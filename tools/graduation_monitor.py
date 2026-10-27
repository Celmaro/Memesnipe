"""Graduation (migration) monitor — pump.fun bonding-curve → PumpSwap.

Watches the pumpapi stream's migrate/createPool events and, for every
graduation, tracks the post-grad price path for T minutes to answer:
  1. What % of graduates pump after migrating (continuation edge)?
  2. Median time-to-peak and drawdown profile (fade edge)?
  3. Which graduation characteristics (MC at grad, pool, liquidity)
     predict the winners?

Shadow-only: logs events + price paths to state/pumpapi_grads/YYYY-MM-DD.jsonl.
When the sample is big enough, wire the winner profile into the bot as a
second strategy (buy-the-graduation or fade-the-dump).

Usage:
    uv run tools/graduation_monitor.py            # live watch
    uv run tools/graduation_monitor.py --replay state/replay_cache/2026-10-26-11.jsonl.zst
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WS_URL = "wss://stream.pumpapi.io/"
OUT_DIR = PROJECT_ROOT / "state" / "pumpapi_grads"

TRACK_MINUTES = 30  # post-grad observation window
SAMPLE_INTERVAL_S = 10.0  # price sampling cadence per graduate


class GraduationMonitor:
    def __init__(self):
        self.grads: dict[str, dict] = {}  # mint -> grad record
        self.prices: dict[str, deque] = defaultdict(deque)  # mint -> (ts, price, mc)
        self.done: set[str] = set()
        self.events = 0
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        self.day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def log(self, row: dict):
        try:
            with (OUT_DIR / f"{self.day}.jsonl").open("a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            print(f"[grads] log failed: {e}", file=sys.stderr)

    def on_event(self, e: dict):
        self.events += 1
        action = e.get("action")
        mint = e.get("mint")
        if not mint:
            return
        price = e.get("price") or 0.0
        mc = e.get("marketCapQuote") or 0.0
        ts = time.time()

        if action in ("migrate", "createPool"):
            # Unit guard (2026-10-27 audit): pumpapi's marketCapQuote is
            # ambiguous — SOL for some events, USD for others (verified:
            # mc_at_grad == quote_in_pool in 108/298 rows, grad_price*1e9 in
            # 142/298). Banding on it produced a fake "$100-200k" story; the
            # reproducible edge bands on quote_in_pool (pure SOL): avoid
            # <50 SOL grads (41% nuked), prefer 85-120 SOL (1.83x median).
            mc = e.get("marketCapQuote") or 0.0
            quote_sol = e.get("quoteInPool") or 0.0
            if mc > 0 and quote_sol > 0 and abs(mc - quote_sol) < 0.01:
                mc_units = "sol"
            elif mc > 0 and price > 0 and abs(mc - price * 1e9) / max(mc, 1e-9) < 0.02:
                mc_units = "usd"
            else:
                mc_units = "unknown"
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "type": "graduation",
                "mint": mint,
                "symbol": e.get("symbol"),
                "pool": e.get("pool"),
                "poolId": e.get("poolId"),
                "mc_at_grad": mc,
                "mc_units": mc_units,  # sol | usd | unknown — band on this
                "quote_in_pool": quote_sol,
                "tokens_in_pool": e.get("tokensInPool"),
                "grad_price": price,
                "creator": e.get("txSigner"),  # deployer wallet (platform is poolCreatedBy)
            }
            self.grads[mint] = rec
            self.log(rec)
            print(
                f"[grads] {rec['symbol'] or mint[:8]} graduated via {rec['pool']} "
                f"pool_liq={rec['quote_in_pool']}",
                flush=True,
            )
        elif action in ("buy", "sell") and mint in self.grads:
            self.prices[mint].append((ts, price, mc))
            self._maybe_finish(mint, ts)

    def _maybe_finish(self, mint: str, now: float):
        if mint in self.done:
            return
        rec = self.grads[mint]
        grad_ts = self._grad_ts(rec)
        if grad_ts is None or now - grad_ts < TRACK_MINUTES * 60:
            return
        self.done.add(mint)
        series = [p for p in self.prices[mint] if p[0] >= grad_ts]
        if len(series) < 3:
            return
        prices = [p[1] for p in series if p[1]]
        if not prices:
            return
        grad_price = rec.get("grad_price") or prices[0]
        peak = max(prices)
        trough = min(prices)
        last = prices[-1]
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "grad_result",
            "mint": mint,
            "symbol": rec["symbol"],
            "pool": rec["pool"],
            "creator": rec.get("creator"),
            "mc_at_grad": rec["mc_at_grad"],
            "mc_units": rec.get("mc_units", "unknown"),
            "quote_in_pool": rec["quote_in_pool"],
            "grad_price": grad_price,
            "peak_multiple": round(peak / grad_price, 3) if grad_price else None,
            "last_multiple": round(last / grad_price, 3) if grad_price else None,
            "trough_multiple": round(trough / grad_price, 3) if grad_price else None,
            "minutes_to_peak": round((next(p[0] for p in series if p[1] == peak) - grad_ts) / 60, 1),
            "samples": len(series),
        }
        self.log(row)
        print(
            f"[grads] RESULT {row['symbol']}: peak {row['peak_multiple']}x, "
            f"final {row['last_multiple']}x, trough {row['trough_multiple']}x "
            f"({row['minutes_to_peak']} min to peak)",
            flush=True,
        )
        self.prices.pop(mint, None)

    @staticmethod
    def _grad_ts(rec: dict) -> float | None:
        ts = rec.get("ts")
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts).timestamp()
        except ValueError:
            return None

    def summary(self) -> str:
        grads = len(self.grads)
        return f"[grads] {grads} graduations, {self.events} events seen"


async def live():
    import websockets

    m = GraduationMonitor()
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(WS_URL, open_timeout=10, max_size=None, ping_interval=None) as ws:
                print("[grads] connected", flush=True)
                backoff = 1.0
                await ws.send(json.dumps({"method": "subscribeNewTokenOnly"}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    m.on_event(json.loads(raw))
        except Exception as e:
            print(f"[grads] stream error: {type(e).__name__}: {e} — retry {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def replay(path: str):
    m = GraduationMonitor()
    p = subprocess.Popen(["zstdcat", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert p.stdout
    n_grads = 0
    for raw in p.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            m.on_event(json.loads(line))
        except json.JSONDecodeError:
            continue
    p.wait()
    # finish all tracking
    import datetime as dt

    for mint in list(m.grads):
        rec = m.grads[mint]
        rec["ts"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=TRACK_MINUTES + 1)).isoformat()
        m._maybe_finish(mint, time.time())
    # aggregate
    rows = []
    try:
        with (OUT_DIR / f"{m.day}.jsonl").open() as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        pass
    results = [r for r in rows if r["type"] == "grad_result"]
    print(m.summary())
    if results:
        peaks = [r["peak_multiple"] for r in results if r["peak_multiple"]]
        ups = [p for p in peaks if p > 1.2]
        downs = [p for p in peaks if p <= 1.2]
        print(
            f"grad results: n={len(results)} | pump(>1.2x at peak): {len(ups)} "
            f"({len(ups)/len(results)*100:.0f}%) | fade candidates: {len(downs)}"
        )
        if ups:
            print(f"pumpers median peak: {sorted(ups)[len(ups)//2]}x")
    else:
        print("no completed grad results in window (need --replay of hours containing post-grad trading)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", help="path to a .jsonl.zst archive to analyze instead of live")
    args = ap.parse_args()
    if args.replay:
        asyncio.run(replay(args.replay))
    else:
        asyncio.run(live())
    return 0


if __name__ == "__main__":
    sys.exit(main())
