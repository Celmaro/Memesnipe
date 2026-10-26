"""pumpapi.io sidecar: latency benchmark, data ingester, momentum provider.

Connects to the public pumpapi.io WebSocket (no key needed) and, per event:

1. Latency benchmark — every event carries the Solana `block` it landed in and
   our local receive time. The sidecar periodically polls the RPC for block
   times and reports the stream lag distribution (stream latency = recv_ts -
   block_time). It also joins against the bot's shadow ledger by mint: when the
   bot detects a token this feed already saw, the delta is the bot's detection
   latency relative to this feed (not absolute on-chain latency).
2. Data ingester — appends every event to
   ``state/pumpapi_events/YYYY-MM-DD.jsonl`` (gitignored), the raw replay
   corpus: creates, buys, sells, transfers, leaders, migrations.
3. Momentum provider — maintains an in-memory momentum table per mint
   (buys/sells over rolling 30s/60s windows, net quote flow, buy pressure
   ratio, unique traders) and appends a snapshot line to
   ``state/pumpapi_momentum/YYYY-MM-DD.jsonl`` every 10s for mints whose
   momentum crosses the configured thresholds. This is the input a future
   momentum-based entry filter or exit trigger consumes.
4. Token monitoring — every ``create`` event is compared against the bot's
   shadow ``detect`` rows: tokens this feed saw but the bot did not detect are
   the bot's detection gaps (router-CPI creates, listener outages), logged to
   ``state/pumpapi_gaps/YYYY-MM-DD.jsonl``.

Best-effort by design: any failure logs and keeps the loop alive. The bot's
trade path never reads this process — it is a pure research/monitor sidecar.

Usage:
    uv run tools/pumpapi_sidecar.py [--state state/pumpapi_sidecar.json]

Run under a watchdog/background process; it never exits on its own.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import json
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path

import websockets

WS_URL = "wss://stream.pumpapi.io/"
RECVdedupe_WINDOW = 120  # seconds of signature dedupe memory
MOMENTUM_SNAPSHOT_INTERVAL = 10.0
MOMENTUM_MIN_BUYS_60S = 5  # minimum buys in 60s to report a mint
MOMENTUM_MIN_NET_FLOW_SOL = 0.5  # minimum net quote flow (buys - sells) in SOL
BLOCK_TIME_POLL_INTERVAL = 30.0
_BOT_LEDGER_RELOAD_SECONDS = 60.0
# pumpapi_events size discipline: 3-4 GB/day raw; recoverable from
# replay.pumpapi.io so the live file may be truncated when huge.
_EVENTS_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB live cap


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _day_dir(base: Path) -> Path:
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    d = base / day
    d.mkdir(parents=True, exist_ok=True)
    return d


class MomentumTracker:
    """Rolling-window buy/sell momentum per mint."""

    def __init__(self, window: float = 60.0):
        self.window = window
        # mint -> deque[(recv_ts, side, quote_amount_sol, trader)]
        self.events: dict[str, deque] = defaultdict(lambda: deque(maxlen=5000))

    def record(self, mint: str, side: str, quote_sol: float, trader: str, ts: float):
        self.events[mint].append((ts, side, quote_sol, trader))

    def snapshot(self, mint: str) -> dict | None:
        dq = self.events.get(mint)
        if not dq:
            return None
        now = time.time()
        while dq and now - dq[0][0] > self.window:
            dq.popleft()
        if not dq:
            return None
        buys = [(ts, q, tr) for ts, _side, q, tr in dq if _side == "buy"]
        sells = [(ts, q, tr) for ts, _side, q, tr in dq if _side == "sell"]
        net = sum(q for _, q, _ in buys) - sum(q for _, q, _ in sells)
        traders = {tr for _, _, _, tr in dq if tr}
        return {
            "mint": mint,
            "buys_60s": len(buys),
            "sells_60s": len(sells),
            "buy_vol_sol_60s": round(sum(q for _, q, _ in buys), 4),
            "sell_vol_sol_60s": round(sum(q for _, q, _ in sells), 4),
            "net_flow_sol_60s": round(net, 4),
            "unique_traders_60s": len(traders),
        }


class SidecarState:
    def __init__(self, base_dir: Path):
        self.base = base_dir
        self.events_dir = base_dir / "pumpapi_events"
        self.momentum_dir = base_dir / "pumpapi_momentum"
        self.gaps_dir = base_dir / "pumpapi_gaps"
        self.latency_dir = base_dir / "pumpapi_latency"
        for d in (self.events_dir, self.momentum_dir, self.gaps_dir, self.latency_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.seen_sigs: deque = deque(maxlen=20000)
        self.seen_set: set[str] = set()
        self.momentum = MomentumTracker()
        # bot shadow detects: mint -> iso ts (loaded periodically from ledger)
        self.bot_detects: dict[str, str] = {}
        self.bot_ledger_path = base_dir / "shadow_data"
        self._bot_ledger_loaded_at = 0.0
        # latency samples: (recv_ts, block_slot)
        self.block_refs: deque = deque(maxlen=4000)
        self._pending_blocks: dict[int, float] = {}
        self._last_rotate_day = ""
        self.stats = {
            "events": 0,
            "creates": 0,
            "gaps": 0,
            "momentum_alerts": 0,
            "latency_samples": 0,
            "started": _now(),
        }

    def append(self, d: Path, row: dict):
        try:
            with (d / f"{datetime.now(UTC).strftime('%Y-%m-%d')}.jsonl").open("a") as f:
                f.write(json.dumps(row) + "\n")
        except Exception as e:
            print(f"[sidecar] append failed {d}: {e}", flush=True)

    def maybe_rotate_events(self):
        """Rotate + compress yesterday's pumpapi_events archive (daily).

        The raw event feed grows ~3-4 GB/day uncompressed. On the first
        ingest call after UTC midnight, compress the previous day's file
        to .jsonl.zst and delete the plain text (zstd -1 keeps analysis
        easy: `zstdcat state/pumpapi_events/<day>.jsonl.zst`). Also
        hard-caps today's live file: if it exceeds _EVENTS_MAX_BYTES the
        oldest half is dropped (feed is recoverable from replay.pumpapi.io).
        """
        try:
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            if self._last_rotate_day == today:
                # mid-day size cap check
                live = self.events_dir / f"{today}.jsonl"
                if live.exists() and live.stat().st_size > _EVENTS_MAX_BYTES:
                    self._truncate_half(live)
                return
            self._last_rotate_day = today
            # compress any plain .jsonl that is NOT today
            import subprocess

            for p in self.events_dir.glob("*.jsonl"):
                if p.stem == today:
                    continue
                zst = p.with_suffix(".jsonl.zst")
                if zst.exists():
                    p.unlink()
                    continue
                subprocess.run(["zstd", "-1", "-f", str(p)], check=True, timeout=600)
                p.unlink()
                print(f"[sidecar] rotated {p.name} -> {zst.name}", flush=True)
        except Exception as e:
            print(f"[sidecar] rotation failed: {e}", flush=True)

    def _truncate_half(self, p: Path):
        """Keep the newest half of an oversized live events file."""
        try:
            with p.open("rb") as f:
                f.seek(p.stat().st_size // 2)
                f.readline()  # skip partial line
                keep = f.read()
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(keep)
            tmp.replace(p)
            print(f"[sidecar] size cap: truncated {p.name} to newest half", flush=True)
        except Exception as e:
            print(f"[sidecar] truncate failed: {e}", flush=True)

    def load_bot_detects(self):
        """Refresh bot-detect knowledge from the shadow ledger every 60s."""
        if time.time() - self._bot_ledger_loaded_at < _BOT_LEDGER_RELOAD_SECONDS:
            return
        self._bot_ledger_loaded_at = time.time()
        try:
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            p = self.bot_ledger_path / f"{day}.jsonl"
            if not p.exists():
                return
            with p.open() as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("type") == "detect" and row.get("mint"):
                        self.bot_detects[row["mint"]] = row.get("ts", "")
        except Exception as e:
            print(f"[sidecar] bot-ledger read failed: {e}", flush=True)


async def poll_block_times(state: SidecarState, rpc_url: str):
    """Resolve pending block slots to wall times via getBlockTime for latency."""
    import urllib.request

    from dotenv import load_dotenv

    load_dotenv(state.base.parent / ".env")
    rpc_url = os.environ.get("SOLANA_NODE_RPC_ENDPOINT", rpc_url)

    while True:
        await asyncio.sleep(BLOCK_TIME_POLL_INTERVAL)
        try:
            pending = list(state._pending_blocks.items())[:10]
            for slot, recv_ts in pending:
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getBlockTime",
                        "params": [slot],
                    }
                ).encode()
                req = urllib.request.Request(
                    rpc_url,
                    data=body,
                    headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    out = json.load(resp)
                chain_ts = out.get("result")
                if isinstance(chain_ts, (int, float)):
                    lag = recv_ts - chain_ts
                    state.append(
                        state.latency_dir,
                        {
                            "ts": _now(),
                            "slot": slot,
                            "recv_ts": recv_ts,
                            "chain_ts": chain_ts,
                            "lag_seconds": round(lag, 3),
                        },
                    )
                    state.stats["latency_samples"] += 1
                state._pending_blocks.pop(slot, None)
        except Exception as e:
            print(f"[sidecar] block-time poll failed: {e}", flush=True)


async def momentum_loop(state: SidecarState):
    """Emit momentum snapshots for hot mints every 10s."""
    while True:
        await asyncio.sleep(MOMENTUM_SNAPSHOT_INTERVAL)
        try:
            hot = [
                (m, s)
                for m in list(state.momentum.events.keys())
                if (s := state.momentum.snapshot(m))
                and s["buys_60s"] >= MOMENTUM_MIN_BUYS_60S
                and s["net_flow_sol_60s"] >= MOMENTUM_MIN_NET_FLOW_SOL
            ]
            hot.sort(key=lambda x: -x[1]["net_flow_sol_60s"])
            for _, snap in hot[:20]:
                snap["ts"] = _now()
                state.append(state.momentum_dir, snap)
                state.stats["momentum_alerts"] += 1
        except Exception as e:
            print(f"[sidecar] momentum loop failed: {e}", flush=True)


def handle_event(state: SidecarState, e: dict):
    sig = e.get("signature")
    if sig:
        if sig in state.seen_set:
            return
        state.seen_set.add(sig)
        state.seen_sigs.append(sig)
    recv_ts = time.time()
    e["_recv_ts"] = recv_ts
    e["_recv_iso"] = _now()
    state.stats["events"] += 1
    state.maybe_rotate_events()
    state.append(state.events_dir, e)

    slot = e.get("block")
    if isinstance(slot, int) and slot > 0 and slot not in state._pending_blocks:
        state._pending_blocks[slot] = recv_ts

    action = e.get("action")
    mint = e.get("mint")

    if action == "create" and mint:
        state.stats["creates"] += 1
        state.load_bot_detects()
        if mint not in state.bot_detects:
            state.stats["gaps"] += 1
            state.append(
                state.gaps_dir,
                {
                    "ts": _now(),
                    "mint": mint,
                    "symbol": e.get("symbol"),
                    "name": e.get("name"),
                    "txSigner": e.get("txSigner"),
                    "initialBuy": e.get("initialBuy"),
                    "note": "feed saw create; bot ledger has no detect for it (yet)",
                },
            )
        # a create is also the first momentum datapoint (initial buy)
        ib = e.get("initialBuy") or 0
        qa = e.get("quoteAmount") or 0
        if ib and qa:
            state.momentum.record(mint, "buy", qa, e.get("txSigner", ""), recv_ts)
    elif action in ("buy", "sell") and mint:
        quote = e.get("quoteAmount")
        if quote is None:
            quote = 0.0
        state.momentum.record(
            mint, action, float(quote), e.get("txSigner", ""), recv_ts
        )
        # enrich a hot-mint price trail into momentum file occasionally is
        # handled by momentum_loop; here nothing further.


async def stream_loop(state: SidecarState):
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(
                WS_URL, open_timeout=10, max_size=None, ping_interval=None
            ) as ws:
                print(f"[sidecar] connected {WS_URL}", flush=True)
                backoff = 1.0
                await ws.send(json.dumps({"method": "subscribeNewTokenOnly"}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    e = json.loads(raw)
                    handle_event(state, e)
        except Exception as e:
            print(
                f"[sidecar] stream error: {type(e).__name__}: {e} — retry in {backoff:.0f}s",
                flush=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def stats_loop(state: SidecarState):
    while True:
        await asyncio.sleep(60)
        s = state.stats
        print(
            f"[sidecar] events={s['events']} creates={s['creates']} gaps={s['gaps']} "
            f"momentum_alerts={s['momentum_alerts']} latency_samples={s['latency_samples']}",
            flush=True,
        )


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default="https://solana-rpc.publicnode.com")
    args = ap.parse_args()
    base = Path(__file__).resolve().parent.parent
    state = SidecarState(base / "state")
    await asyncio.gather(
        stream_loop(state),
        momentum_loop(state),
        stats_loop(state),
        poll_block_times(state, args.rpc),
    )


if __name__ == "__main__":
    asyncio.run(main())
