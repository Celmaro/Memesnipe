"""Paper-copy engine — shadow-trade the vetted copy watchlist live.

Watches the pumpapi stream for buy/sell events from wallets in
state/wallet_scout/watchlist.json and simulates mirrored trades:

  - entry: mirror their buy (scaled to SIM_POSITION_SOL), at the feed price
    of the NEXT event after their buy (models detection+submit latency)
  - exit: mirror their sell, or our TP/SL if theirs never comes
  - logs every decision to state/paper_copy/YYYY-MM-DD.jsonl

Zero funds at risk. After ~24h, the ledger gives copy-trading EV at real
latency. To go live later, swap the sim_open/sim_close calls for
tools/pumpapi_exec.py or the repo's local pipeline.

Usage:
    uv run tools/paper_copy.py                     # watch the saved watchlist
    uv run tools/paper_copy.py --wallets W1,W2     # override list
    uv run tools/paper_copy.py --replay hour.jsonl.zst   # backtest a window
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
OUT_DIR = PROJECT_ROOT / "state" / "paper_copy"
WATCHLIST = PROJECT_ROOT / "state" / "wallet_scout" / "watchlist.json"

SIM_POSITION_SOL = 0.01
TP_PCT = 0.20
SL_PCT = -0.20
MAX_HOLD_MIN = 240
# model our detection+submit latency: enter at the price of the next event
# on the same token that arrives after the watched wallet's buy
LATENCY_MODEL = "next_event"


class PaperCopy:
    def __init__(self, wallets: set[str]):
        self.wallets = wallets
        self.positions: dict[str, dict] = {}  # mint -> position
        self.pending: dict[str, list] = defaultdict(list)  # mint -> queued events
        self.trades: list[dict] = []
        self.events = 0
        self.copy_buys = 0
        self.copy_sells = 0
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        self.day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def log(self, row: dict):
        try:
            with (OUT_DIR / f"{self.day}.jsonl").open("a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            print(f"[copy] log failed: {e}", file=sys.stderr)

    def on_event(self, e: dict):
        self.events += 1
        action = e.get("action")
        mint = e.get("mint")
        if not mint:
            return
        price = e.get("price") or 0.0

        # process pending entries first (latency model: next event on token)
        if mint in self.pending:
            queue = self.pending[mint]
            while queue:
                pend = queue[0]
                if price and price > 0:
                    queue.pop(0)
                    self._open(mint, e, price, pend)
                else:
                    break

        if action == "buy":
            traders = {bd.get("trader") for bd in e.get("breakdown") or []} | {e.get("txSigner")}
            if traders & self.wallets:
                if mint not in self.positions and len(self.positions) < 20:
                    if price and price > 0 and LATENCY_MODEL == "next_event":
                        self.pending[mint].append(
                            {"lead_wallet": ", ".join(t for t in traders & self.wallets),
                             "lead_ts": time.time(), "lead_price": price}
                        )
                    else:
                        self._open(mint, e, price, {
                            "lead_wallet": ", ".join(t for t in traders & self.wallets),
                            "lead_ts": time.time(), "lead_price": price})
        elif action == "sell":
            traders = {bd.get("trader") for bd in e.get("breakdown") or []} | {e.get("txSigner")}
            pos = self.positions.get(mint)
            if pos and (traders & self.wallets) and pos.get("lead_wallet") in (traders & self.wallets):
                if price:
                    self._close(mint, e, price, "lead_sold")
        elif action in ("buy", "sell"):
            pos = self.positions.get(mint)
            if pos and price:
                pos["last_price"] = price
                pos["last_ts"] = time.time()
                pnl = price / pos["entry_price"] - 1
                if pnl >= TP_PCT:
                    self._close(mint, e, price, "take_profit")
                elif pnl <= SL_PCT:
                    self._close(mint, e, price, "stop_loss")

    def _open(self, mint: str, e: dict, price: float, pend: dict):
        if mint in self.positions or not price or price <= 0:
            if mint in self.pending:
                self.pending[mint].clear()
            return
        entry_price = price
        tokens = SIM_POSITION_SOL / entry_price
        self.positions[mint] = {
            "mint": mint,
            "symbol": e.get("symbol") or "?",
            "lead_wallet": pend["lead_wallet"],
            "entry_price": entry_price,
            "lead_price": pend["lead_price"],
            "tokens": tokens,
            "entry_ts": time.time(),
            "last_price": price,
        }
        self.copy_buys += 1
        lag = entry_price / pend["lead_price"] - 1 if pend.get("lead_price") else 0
        self.log({
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "paper_open",
            "mint": mint, "symbol": self.positions[mint]["symbol"],
            "lead_wallet": pend["lead_wallet"],
            "lead_price": pend["lead_price"], "entry_price": entry_price,
            "entry_lag_pct": round(lag * 100, 2),
            "position_sol": SIM_POSITION_SOL,
        })
        print(f"[copy] OPEN {self.positions[mint]['symbol']} via {pend['lead_wallet'][:8]}… "
              f"entry {entry_price:.3e} (lag {lag*100:+.2f}%)", flush=True)

    def _close(self, mint: str, e: dict, price: float, reason: str):
        pos = self.positions.pop(mint, None)
        if not pos or not price:
            return
        pnl = (price / pos["entry_price"] - 1) * 100
        self.copy_sells += 1
        self.trades.append(pnl)
        self.log({
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "paper_close",
            "mint": mint, "symbol": pos["symbol"],
            "lead_wallet": pos["lead_wallet"],
            "entry_price": pos["entry_price"], "exit_price": price,
            "pnl_pct": round(pnl, 2), "exit_reason": reason,
            "hold_min": round((time.time() - pos["entry_ts"]) / 60, 1),
        })
        print(f"[copy] CLOSE {pos['symbol']} {reason} {pnl:+.1f}%", flush=True)

    def sweep(self):
        """Force-close stale positions at last price (lead never sold)."""
        now = time.time()
        for mint, pos in list(self.positions.items()):
            if now - pos["entry_ts"] > MAX_HOLD_MIN * 60:
                self._close(mint, {}, pos["last_price"], "max_hold")

    def summary(self) -> str:
        if not self.trades:
            return f"[copy] {self.copy_buys} opens, no closes yet ({self.events} events)"
        wins = [p for p in self.trades if p > 0]
        return (f"[copy] {self.copy_buys} opens / {self.copy_sells} closes | "
                f"wr={len(wins)/len(self.trades)*100:.0f}% "
                f"avg={sum(self.trades)/len(self.trades):+.1f}% "
                f"total={sum(self.trades):+.1f}%")


async def live(wallets: set[str]):
    import websockets

    pc = PaperCopy(wallets)
    backoff = 1.0
    last_sweep = time.time()
    while True:
        try:
            async with websockets.connect(WS_URL, open_timeout=10, max_size=None, ping_interval=None) as ws:
                print(f"[copy] watching {len(wallets)} wallets", flush=True)
                backoff = 1.0
                await ws.send(json.dumps({"method": "subscribeNewTokenOnly"}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    pc.on_event(json.loads(raw))
                    if time.time() - last_sweep > 300:
                        pc.sweep()
                        last_sweep = time.time()
                        print(pc.summary(), flush=True)
        except Exception as e:
            print(f"[copy] stream error: {type(e).__name__}: {e} — retry {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def replay(wallets: set[str], path: str):
    pc = PaperCopy(wallets)
    p = subprocess.Popen(["zstdcat", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert p.stdout
    for raw in p.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            pc.on_event(json.loads(line))
        except json.JSONDecodeError:
            continue
    p.wait()
    # close everything at last price
    for mint in list(pc.positions):
        pc._close(mint, {}, pc.positions[mint]["last_price"], "archive_end")
    print(pc.summary())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallets", help="comma-separated override; default = watchlist.json")
    ap.add_argument("--replay", help="backtest against an archive instead of live")
    args = ap.parse_args()
    if args.wallets:
        wallets = {w.strip() for w in args.wallets.split(",") if w.strip()}
    else:
        wallets = set(json.loads(WATCHLIST.read_text()))
    if not wallets:
        print("no wallets to watch", file=sys.stderr)
        return 1
    if args.replay:
        return replay(wallets, args.replay)
    asyncio.run(live(wallets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
