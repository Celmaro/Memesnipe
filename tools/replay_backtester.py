"""Backtest exit strategies against pumpapi Historical Replay archives.

Downloads hourly event archives (https://replay.pumpapi.io/Y/M/D/H.jsonl.zst),
replays pump.fun bonding-curve activity, and simulates the bot's entry filters
plus candidate exit strategies (TP/SL grids, trailing stops) on every launch.

Event model (matches the live stream format parsed by the sidecar):
  create:  bonding curve deployed, mint/symbol/initialBuy
  buy:     price, marketCapQuote (SOL), solAmount, tokenAmount, user
  sell:    same fields
Prices are bonding-curve AMM results implied by the feed's own price fields —
the same series the live momentum provider sees, so backtests inherit real
microstructure (as fast as the feed recorded it).

Usage:
    uv run tools/replay_backtester.py --hours 2026/10/26/09 2026/10/26/10 ...
    uv run tools/replay_backtester.py --last 6            # last N whole hours
    uv run tools/replay_backtester.py --last 6 --max-open 40 --workers 4

Output: state/replay/<stamp>/trades.jsonl + summary.json printed to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import gzip
import io
import json
import statistics
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPLAY_BASE = "https://replay.pumpapi.io/{dt:%Y/%m/%d/%H}.jsonl.zst"
OUT_DIR = PROJECT_ROOT / "state" / "replay"

sys.path.insert(0, str(PROJECT_ROOT / "src"))
from research.cost_model import net_round_trip  # noqa: E402

# ---- entry filter: mirror memesnipe.yaml (shadow mode of 2026-10-26) ----
ENTRY = {
    "min_curve_progress_sol": 0.25,  # skip the first pixels of the curve
    "position_sol": 0.01,
}

# ---- exit strategies under test (grid) ----
EXIT_GRID = [
    {"name": "base_tp20_sl20", "tp": 0.20, "sl": -0.20},
    {"name": "tp30_sl20", "tp": 0.30, "sl": -0.20},
    {"name": "tp20_sl10", "tp": 0.20, "sl": -0.10},
    {"name": "tp50_sl25", "tp": 0.50, "sl": -0.25},
    {"name": "trail20", "trail_activation": 0.15, "trail_distance": 0.20, "sl": -0.20},
    {"name": "trail10", "trail_activation": 0.10, "trail_distance": 0.10, "sl": -0.20},
    # staged_trail (R4-P1-7): sell 40% at +25%, arm BE floor at +10%,
    # trail the rest 8% below peak, -20% hard stop. pnl_pct reported is the
    # size-weighted blend of the partial sell and the final exit.
    {
        "name": "staged_trail",
        "staged": True,
        "tp_share": 0.40,
        "tp_pnl": 0.20,
        "floor_arm": 0.10,
        "floor_pct": 0.10,
        "trail_distance": 0.08,
        "sl": -0.20,
    },
]

MAX_OPEN_POSITIONS = 40  # cap concurrent positions per strategy (bot realism)
TOKEN_MAX_AGE_S = 1.0  # entries only within 1s of create (bot freshness gate)
# C8.1 fix: the live bot's freshness gate is the age in seconds compared
# directly; the old `* 60` multiplied the 1s constant into a 60s window,
# admitting launches that were a minute old. Keep the constant as seconds.
MAX_TOKEN_LIFETIME_S = 3600  # stop tracking a token after 1h


@dataclass
class Position:
    mint: str
    symbol: str
    strategy: str
    entry_price: float
    entry_time: float
    tokens: float
    peak_price: float = 0.0
    trailing_stop: float | None = None
    # staged_trail state
    tp_fired: bool = False
    remaining_share: float = 1.0
    realized_pnl: float = 0.0  # size-weighted pnl of legs already sold
    floor_price: float | None = None


@dataclass
class StrategyState:
    cfg: dict
    open: dict[str, Position] = field(default_factory=dict)
    trades: list = field(default_factory=list)


class ReplayBacktester:
    def __init__(
        self,
        strategies: list[dict],
        out_dir: Path,
        max_open_positions: int = MAX_OPEN_POSITIONS,
    ):
        self.strategies = {s["name"]: StrategyState(s) for s in strategies}
        self.out_dir = out_dir
        # C8.1 fix: thread the CLI --max-open cap down (was a module global
        # that ignored the parsed argument).
        self.max_open_positions = max_open_positions
        # per-token curve state
        self.tokens: dict[str, dict] = {}
        self.trades_out: list = []
        self.events_seen = 0

    # ---------- event processing ----------

    def on_event(self, ev: dict) -> None:
        self.events_seen += 1
        action = ev.get("action")
        if action == "create":
            mint = ev.get("mint")
            if not mint:
                return
            price = ev.get("price") or 0.0
            mc = ev.get("marketCapQuote") or 0.0
            self.tokens[mint] = {
                "symbol": ev.get("symbol") or "?",
                "price": price,
                "mc_sol": mc,
                # C8.1: real curve progress = SOL actually in the pool.
                # pumpapi create events carry quoteInPool (SOL already);
                # buy/sell events update it below.
                "real_quote_sol": ev.get("quoteInPool") or 0.0,
                "created_ts": _ev_ts(ev),
                "buys_1m": deque(),
                "seen_events": 0,
            }
            return
        if action in ("buy", "sell"):
            mint = ev.get("mint")
            tok = self.tokens.get(mint)
            if tok is None:
                return  # pre-existing token, not a replayed launch
            price = ev.get("price")
            if not price:
                return
            ts = _ev_ts(ev)
            sol_amount = ev.get("solAmount") or ev.get("quoteAmount") or 0.0
            tok["price"] = price
            tok["mc_sol"] = ev.get("marketCapQuote") or tok["mc_sol"]
            # C8.1: keep real curve progress (SOL in pool) current.
            if ev.get("quoteInPool") is not None:
                tok["real_quote_sol"] = ev["quoteInPool"]
            tok["seen_events"] += 1
            if action == "buy":
                tok["buys_1m"].append((ts, sol_amount))
                while tok["buys_1m"] and ts - tok["buys_1m"][0][0] > 60:
                    tok["buys_1m"].popleft()
                # entry: every fresh token gets bought by every strategy
                # (one entry per token per strategy — entry filters are
                # identical across strategies so they cancel out in A/B)
                tok["_buys_seen"] = tok.get("_buys_seen", 0) + 1
                age = ts - tok["created_ts"]
                wait = ENTRY.get("min_entry_age_s", 0.0)
                confirm = ENTRY.get("confirm_buys", 1)
                if (
                    tok["_buys_seen"] == confirm  # R5-P1-4: gate-wait/confirm
                    and age <= TOKEN_MAX_AGE_S * 60
                    and age >= wait
                ):
                    self._try_entries(mint, tok, price, ts, sol_amount)
            self._check_exits(mint, tok, price, ts)
        # migrate/createPool: position exits on curve? keep simple: treat as
        # force-close at last curve price (bot wouldn't hold through migration)
        elif action in ("migrate", "createPool"):
            mint = ev.get("mint")
            tok = self.tokens.get(mint)
            if tok:
                ts = _ev_ts(ev)
                self._force_close_all(mint, tok, tok["price"], ts, "migrate")

    def _try_entries(
        self, mint: str, tok: dict, price: float, ts: float, sol_amount: float
    ) -> None:
        # C8.1 fix: the LIVE curve gate (universal_trader._curve_gate_reason)
        # measures curve progress as the SOL actually paid into the curve
        # (real_quote_reserves / 1e9), not market cap. pumpapi's quoteInPool
        # is that real SOL figure directly; fall back to MC only for
        # archives that predate the field.
        curve_progress = tok.get("real_quote_sol", 0.0) or 0.0
        if curve_progress <= 0:
            curve_progress = tok["mc_sol"]  # legacy fallback (MC in SOL)
        if curve_progress < ENTRY["min_curve_progress_sol"]:
            return
        for name, st in self.strategies.items():
            if len(st.open) >= self.max_open_positions:
                continue
            tokens_out = ENTRY["position_sol"] / price if price else 0.0
            st.open[mint] = Position(
                mint=mint,
                symbol=tok["symbol"],
                strategy=name,
                entry_price=price,
                entry_time=ts,
                tokens=tokens_out,
                peak_price=price,
            )

    def _check_exits(self, mint: str, tok: dict, price: float, ts: float) -> None:
        for name, st in self.strategies.items():
            pos = st.open.get(mint)
            if pos is None:
                continue
            cfg = st.cfg
            pos.peak_price = max(pos.peak_price, price)
            pnl = price / pos.entry_price - 1.0

            # trailing stop arming
            trail_act = cfg.get("trail_activation")
            if trail_act is not None:
                peak_pnl = pos.peak_price / pos.entry_price - 1.0
                if peak_pnl >= trail_act and pos.trailing_stop is None:
                    pos.trailing_stop = pos.peak_price * (1 - cfg["trail_distance"])
                elif pos.trailing_stop is not None:
                    pos.trailing_stop = max(
                        pos.trailing_stop, pos.peak_price * (1 - cfg["trail_distance"])
                    )

            reason = None
            if cfg.get("staged"):
                reason = self._staged_exit(cfg, pos, price, pnl)
            elif cfg.get("tp") is not None and pnl >= cfg["tp"]:
                reason = "take_profit"
            elif cfg.get("sl") is not None and pnl <= cfg["sl"]:
                reason = "stop_loss"
            elif pos.trailing_stop is not None and price <= pos.trailing_stop:
                reason = "trailing_stop"

            if reason:
                self._close(st, pos, price, ts, reason)

    def _staged_exit(self, cfg: dict, pos: Position, price: float, pnl: float) -> str | None:
        """R4-P1-7 state machine: partial TP -> BE floor -> trail -> hard stop.

        Mirrors src/trading/trail_exit.py. Returns exit reason or None; on the
        partial leg, blends realized pnl into pos.realized_pnl and returns None.
        """
        peak_pnl = pos.peak_price / pos.entry_price - 1.0

        # hard stop always live
        if pnl <= cfg["sl"]:
            pos.realized_pnl += pos.remaining_share * pnl * 100
            pos.remaining_share = 0.0
            return "stop_loss"

        # leg 1: partial take profit
        if not pos.tp_fired and pnl >= cfg["tp_pnl"]:
            pos.tp_fired = True
            share = cfg["tp_share"]
            pos.realized_pnl += share * pnl * 100
            pos.remaining_share -= share

        # BE floor arms after the TP leg (floor = +floor_pct above entry)
        if pos.tp_fired and pos.floor_price is None and peak_pnl >= cfg["floor_arm"]:
            pos.floor_price = pos.entry_price * (1 + cfg["floor_pct"])

        # trail the remainder
        if pos.tp_fired:
            trail = pos.peak_price * (1 - cfg["trail_distance"])
            pos.floor_price = max(pos.floor_price or 0.0, trail)

        if pos.tp_fired and pos.floor_price is not None and price <= pos.floor_price:
            pos.realized_pnl += pos.remaining_share * pnl * 100
            pos.remaining_share = 0.0
            return "trail_floor"

        return None

    def _close(self, st: StrategyState, pos: Position, price: float, ts: float, reason: str) -> None:
        st.open.pop(pos.mint, None)
        pnl_pct = (price / pos.entry_price - 1.0) * 100
        if pos.tp_fired:
            # staged: blend realized partial-leg pnl with the remaining share's
            # exit pnl (realized_pnl already size-weighted in %-of-position)
            pnl_pct = pos.realized_pnl + pos.remaining_share * pnl_pct
        # R5-P1-3: net of modeled fees (pump.fun 1.25%/side + network). The
        # audit's P1-3 — a +20% price move is not +20% realized. Computed on
        # the blended gross, at the shadow position size.
        net_pnl_pct = net_round_trip(pnl_pct / 100.0, ENTRY["position_sol"]) * 100
        self.trades_out.append(
            {
                "strategy": pos.strategy,
                "mint": pos.mint,
                "symbol": pos.symbol,
                "entry_time": pos.entry_time,
                "exit_time": ts,
                "hold_s": round(ts - pos.entry_time, 1),
                "pnl_pct": round(pnl_pct, 2),
                "net_pnl_pct": round(net_pnl_pct, 2),  # R5-P1-3
                "exit_reason": reason,
                "peak_pnl_pct": round((pos.peak_price / pos.entry_price - 1) * 100, 2),
            }
        )

    def _force_close_all(self, mint: str, tok: dict, price: float, ts: float, reason: str) -> None:
        for st in self.strategies.values():
            pos = st.open.pop(mint, None)
            if pos:
                self._close(st, pos, price, ts, reason)

    # ---------- hourly housekeeping ----------

    def prune(self, now_ts: float) -> None:
        # tokens idle > MAX_TOKEN_LIFETIME_S: force-close any survivors
        stale = [
            m for m, t in self.tokens.items() if now_ts - t["created_ts"] > MAX_TOKEN_LIFETIME_S
        ]
        for m in stale:
            tok = self.tokens.pop(m)
            self._force_close_all(m, tok, tok["price"], now_ts, "eol")
        # cap memory on the token dict
        if len(self.tokens) > 20_000:
            cutoff = sorted(self.tokens.items(), key=lambda kv: kv[1]["created_ts"])
            for m, _ in cutoff[: len(self.tokens) - 10_000]:
                tok = self.tokens.pop(m)
                self._force_close_all(m, tok, tok["price"], now_ts, "eol")

    # ---------- output ----------

    def summary(self) -> dict:
        out = {}
        for name, st in self.strategies.items():
            trades = [t for t in self.trades_out if t["strategy"] == name]
            if not trades:
                out[name] = {"trades": 0}
                continue
            pnls = [t["pnl_pct"] for t in trades]
            net_pnls = [t.get("net_pnl_pct", t["pnl_pct"]) for t in trades]
            wins = [p for p in pnls if p > 0]
            total = sum(pnls)
            out[name] = {
                "trades": len(trades),
                "winrate": round(len(wins) / len(trades) * 100, 1),
                "avg_pnl_pct": round(total / len(trades), 2),
                "total_pnl_pct": round(total, 1),
                "avg_net_pct": round(sum(net_pnls) / len(net_pnls), 2),  # R5-P1-3
                "total_net_pct": round(sum(net_pnls), 1),  # R5-P1-3
                "median_hold_s": round(statistics.median(t["hold_s"] for t in trades), 1),
                "exit_mix": _tally(t["exit_reason"] for t in trades),
                "best": max(pnls),
                "worst": min(pnls),
            }
        return out

    def flush(self, stamp: str) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        d = self.out_dir / stamp
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "trades.jsonl", "w") as f:
            for t in self.trades_out:
                f.write(json.dumps(t) + "\n")
        with open(d / "summary.json", "w") as f:
            json.dump(self.summary(), f, indent=2)
        return d


def _ev_ts(ev: dict) -> float:
    ts = ev.get("timestamp")
    if ts is None:
        ts = ev.get("blockTime", 0)
    if isinstance(ts, str):
        with contextlib.suppress(ValueError):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    return float(ts or 0)


def _tally(it):
    d = defaultdict(int)
    for x in it:
        d[x] += 1
    return dict(d)


def parse_hour_str(s: str) -> datetime:
    # accept 2026/10/26/09 or 2026-10-26T09
    for fmt in ("%Y/%m/%d/%H", "%Y-%m-%dT%H"):
        with contextlib.suppress(ValueError):
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
    raise argparse.ArgumentTypeError(f"bad hour spec: {s}")


async def download_hour(session: "aiohttp.ClientSession", dt: datetime) -> list[dict]:
    url = REPLAY_BASE.format(dt=dt)
    import logging

    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    timeout = aiohttp.ClientTimeout(total=None, sock_read=120, connect=30)
    async with session.get(url, timeout=timeout) as r:
        if r.status == 404:
            print(f"[replay] missing (404): {url}", file=sys.stderr)
            return []
        r.raise_for_status()
        raw = await r.read()
    text = gzip.decompress(raw).decode() if raw[:2] == b"\x1f\x8b" else _zstd_decompress(raw)
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            with contextlib.suppress(json.JSONDecodeError):
                events.append(json.loads(line))
    print(f"[replay] {url}: {len(events)} events", file=sys.stderr)
    return events


def _zstd_decompress(raw: bytes) -> str:
    import subprocess

    p = subprocess.run(
        ["zstd", "-d", "--stdout"], input=raw, capture_output=True, check=True
    )
    return p.stdout.decode(errors="replace")


def load_local(path: Path):
    """Stream a cached hourly archive line-by-line (zstd pipe, low memory)."""
    import subprocess

    p = subprocess.Popen(
        ["zstdcat", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    n = 0
    assert p.stdout is not None
    for raw in p.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            yield json.loads(line)
            n += 1
            if n % 2_000_000 == 0:
                print(f"[replay] {path.name}: {n} events...", file=sys.stderr)
    p.wait()
    print(f"[replay] {path.name}: {n} events", file=sys.stderr)


def _print_summary(bt, s, stamp, t0, out):
    print(f"\n=== backtest {stamp} | {bt.events_seen} events | {time.time()-t0:.0f}s ===")
    print(f"tokens tracked: {len(bt.tokens)} trades: {len(bt.trades_out)} -> {out}")
    for name, r in s.items():
        if r.get("trades"):
            print(
                f"{name:16} n={r['trades']:5} wr={r['winrate']:5.1f}% "
                f"avg={r['avg_pnl_pct']:+6.2f}% total={r['total_pnl_pct']:+9.1f}% "
                f"hold={r['median_hold_s']:7.0f}s {r['exit_mix']}"
            )
        else:
            print(f"{name:16} no trades")


async def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--hours", nargs="+", help="e.g. 2026/10/26/09 2026/10/26/10")
    g.add_argument("--last", type=int, help="replay last N complete hours")
    g.add_argument("--local", help="path to a cached .jsonl.zst archive")
    ap.add_argument("--max-open", type=int, default=MAX_OPEN_POSITIONS)
    args = ap.parse_args()

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    if args.local:
        stamp = Path(args.local).stem
        bt = ReplayBacktester(EXIT_GRID, OUT_DIR, max_open_positions=args.max_open)
        t0 = time.time()
        for ev in load_local(Path(args.local)):
            bt.on_event(ev)
            if bt.events_seen % 500_000 == 0:
                bt.prune(_ev_ts(ev))
        s = bt.summary()
        out = bt.flush(stamp)
        _print_summary(bt, s, stamp, t0, out)
        return 0
    if args.hours:
        hours = [parse_hour_str(h) for h in args.hours]
    else:
        hours = [now - timedelta(hours=i + 1) for i in range(args.last)]
    hours.sort()

    if aiohttp is None:
        print("aiohttp required: uv run (has it)", file=sys.stderr)
        return 1

    bt = ReplayBacktester(EXIT_GRID, OUT_DIR, max_open_positions=args.max_open)
    stamp = f"{hours[0]:%Y%m%d_%H}-{hours[-1]:%H}"
    t0 = time.time()
    async with aiohttp.ClientSession() as session:
        for dt in hours:
            events = await download_hour(session, dt)
            for ev in events:
                bt.on_event(ev)
            bt.prune(_ev_ts({"timestamp": f"{dt:%Y-%m-%dT%H:00:00Z}"}))

    s = bt.summary()
    out = bt.flush(stamp)
    print(f"\n=== backtest {stamp} | {bt.events_seen} events | {time.time()-t0:.0f}s ===")
    print(f"tokens tracked: {len(bt.tokens)} trades: {len(bt.trades_out)} -> {out}")
    for name, r in s.items():
        if r.get("trades"):
            print(
                f"{name:16} n={r['trades']:5} wr={r['winrate']:5.1f}% "
                f"avg={r['avg_pnl_pct']:+6.2f}% total={r['total_pnl_pct']:+9.1f}% "
                f"hold={r['median_hold_s']:7.0f}s {r['exit_mix']}"
            )
        else:
            print(f"{name:16} no trades")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
