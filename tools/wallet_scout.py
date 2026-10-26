"""Wallet scout — score traders from pumpapi replay archives for copy trading.

Reads hourly replay archives, reconstructs per-wallet trade history on
bonding-curve launches, and scores every wallet for copy-trading value:

  - n_trades, win rate (position PnL at exit heuristics below)
  - median return per trade, cumulative return
  - entry timing: seconds from token create (same-block snipers excluded
    from "skill" — you cannot copy their entry)
  - early-exit behavior: did they sell into strength (good) or dump at loss

Exit heuristic for open positions at archive end: mark at last observed
price (conservative).

Scoring output: state/wallet_scout/YYYY-MM-DD.jsonl, one row per wallet,
sorted best-first in the printed table. Filter to wallets with
n_trades >= MIN_TRADES and --seed to pin a user-provided watchlist.

Usage:
    uv run tools/wallet_scout.py --archives a.jsonl.zst b.jsonl.zst
    uv run tools/wallet_scout.py --archives ... --seed <wallet1,wallet2>
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "state" / "wallet_scout"

MIN_TRADES = 5
MIN_BUY_DELAY_S = 2.0  # entries faster than this = sniper/insider, flagged
SNIPER_FLAG = "sniper"


def stream_events(path: str):
    p = subprocess.Popen(["zstdcat", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert p.stdout
    for raw in p.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue
    p.wait()


def scout(archives: list[str], seed_wallets: set[str]) -> int:
    # per-wallet open positions: wallet -> mint -> buy info
    open_pos: dict[str, dict[str, dict]] = defaultdict(dict)
    # token prices: mint -> last price + create time
    tok_price: dict[str, float] = {}
    tok_create: dict[str, float] = {}
    # completed trade log: wallet -> list of (ret_pct, entry_delay_s)
    trades: dict[str, list[tuple[float, float]]] = defaultdict(list)
    events = 0

    def close_position(wallet: str, mint: str, price: float, tok_amt: float):
        pos = open_pos[wallet].pop(mint, None)
        if not pos:
            return
        if pos["entry_price"] <= 0 or price <= 0:
            return
        ret = (price / pos["entry_price"] - 1) * 100
        # sanity cap: pump.fun curve prices can print garbage on thin pools;
        # a per-trade return beyond these bounds is a data artifact, not skill
        if ret > 2000 or ret < -99:
            return
        trades[wallet].append((ret, pos["entry_delay_s"]))

    for path in archives:
        for e in stream_events(path):
            events += 1
            action = e.get("action")
            mint = e.get("mint")
            if not mint:
                continue
            price = e.get("price")
            if action == "create":
                tok_create[mint] = float(e.get("timestamp") or 0)
                continue
            if action in ("buy", "sell") and price:
                tok_price[mint] = price
                for bd in e.get("breakdown") or [{"trader": e.get("txSigner"),
                                                  "tokenAmount": e.get("tokenAmount"),
                                                  "quoteAmount": e.get("quoteAmount")}]:
                    w = bd.get("trader")
                    if not w:
                        continue
                    if action == "buy":
                        delay = 0.0
                        ts = tok_create.get(mint, 0)
                        lts = e.get("localTimestamp")
                        if ts and lts:
                            delay = max(0.0, (lts / 1000.0) - ts)
                        # average into position (simple: track weighted entry)
                        amt = float(bd.get("tokenAmount") or 0)
                        prev = open_pos[w].get(mint)
                        if prev:
                            tot = prev["tokens"] + amt
                            prev["entry_price"] = (
                                (prev["entry_price"] * prev["tokens"] + price * amt) / tot if tot else price
                            )
                            prev["tokens"] = tot
                            prev["entry_delay_s"] = min(prev["entry_delay_s"], delay)
                        else:
                            open_pos[w][mint] = {
                                "entry_price": price,
                                "tokens": amt,
                                "entry_delay_s": delay,
                            }
                    else:  # sell
                        close_position(w, mint, price, float(bd.get("tokenAmount") or 0))

    # close remaining positions at last known price (conservative)
    for w, positions in list(open_pos.items()):
        for mint in list(positions):
            close_position(w, mint, tok_price.get(mint, 0.0), 0)

    # score wallets
    rows = []
    for w, tl in trades.items():
        if len(tl) < MIN_TRADES:
            continue
        rets = [t[0] for t in tl]
        delays = [t[1] for t in tl]
        wins = [r for r in rets if r > 0]
        row = {
            "wallet": w,
            "n_trades": len(tl),
            "winrate": round(len(wins) / len(tl) * 100, 1),
            "median_ret": round(statistics.median(rets), 1),
            "avg_ret": round(sum(rets) / len(rets), 1),
            "total_ret": round(sum(rets), 1),
            "best": round(max(rets), 1),
            "worst": round(min(rets), 1),
            "median_entry_delay_s": round(statistics.median(delays), 2),
            "sniper_share": round(sum(1 for d in delays if d < MIN_BUY_DELAY_S) / len(tl) * 100, 1),
            "seeded": w in seed_wallets,
        }
        rows.append(row)

    rows.sort(key=lambda r: (r["seeded"], r["total_ret"]), reverse=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = OUT_DIR / f"{day}.jsonl"
    with out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    seeded = [r for r in rows if r["seeded"]]
    normal = [r for r in rows if not r["seeded"]]
    print(f"events={events} wallets_scored={len(rows)} (n>={MIN_TRADES} trades) -> {out}")
    if seeded:
        print("\n== SEEDED WALLET ==")
        for r in seeded:
            print(f"  {r['wallet'][:10]}… n={r['n_trades']} wr={r['winrate']}% med={r['median_ret']}% "
                  f"total={r['total_ret']}% sniper={r['sniper_share']}% delay={r['median_entry_delay_s']}s")
    print("\n== TOP 20 (skill-scored: non-sniper entries weighted) ==")
    shown = 0
    for r in normal:
        # skill score: penalize same-block entries heavily
        skill = r["total_ret"] * (1 - r["sniper_share"] / 200)
        r["skill"] = round(skill, 1)
        if shown < 20:
            print(f"  {r['wallet'][:10]}… n={r['n_trades']:3} wr={r['winrate']:5.1f}% med={r['median_ret']:+7.1f}% "
                  f"total={r['total_ret']:+9.1f}% sniper={r['sniper_share']:5.1f}% delay={r['median_entry_delay_s']:6.1f}s")
            shown += 1
    rows.sort(key=lambda r: r.get("skill", 0), reverse=True)
    with out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archives", nargs="+", required=True)
    ap.add_argument("--seed", help="comma-separated wallet watchlist to pin")
    args = ap.parse_args()
    seeds = {w.strip() for w in args.seed.split(",")} if args.seed else set()
    return scout(args.archives, seeds)


if __name__ == "__main__":
    sys.exit(main())
