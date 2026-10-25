"""Analyze the execution ledger (trades/trades.log) for net expectancy.

Phase 6 of the implementation plan: measure net PnL, profit factor, fill
rate, unsellable rate, median holding time — never tune by feel.

Usage:
    uv run python tools/analyze_ledger.py [trades/trades.log]

Output is a per-session summary; nothing is sent on-chain.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("trades/trades.log")
    if not path.exists():
        print(f"ledger not found: {path}")
        return 1

    # Group by token: list of (action, entry)
    by_token: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            by_token[entry.get("token_address", "?")].append((entry["action"], entry))

    sessions: dict[str, dict] = defaultdict(
        lambda: {"buys": 0, "sells": 0, "spent": 0.0, "received": 0.0}
    )

    for token, entries in by_token.items():
        buys = [e for a, e in entries if a == "buy"]
        sells = [e for a, e in entries if a == "sell"]
        session = "unknown"
        if buys:
            session = buys[0].get("timestamp", "unknown")[:10]
        elif sells:
            session = sells[0].get("timestamp", "unknown")[:10]
        s = sessions[session]
        s["buys"] += len(buys)
        s["sells"] += len(sells)
        for e in buys:
            s["spent"] += e.get("quote_spent_sol") or 0.0
        for e in sells:
            s["received"] += e.get("quote_received_sol") or 0.0

    print(f"Ledger: {path}")
    print(f"{'Session':<12} {'Buys':>5} {'Sells':>5} {'Spent SOL':>12} {'Received SOL':>14} {'Net SOL':>10}")
    for session in sorted(sessions):
        s = sessions[session]
        net = s["received"] - s["spent"]
        print(
            f"{session:<12} {s['buys']:>5} {s['sells']:>5} "
            f"{s['spent']:>12.6f} {s['received']:>14.6f} {net:>10.6f}"
        )

    # Across the whole ledger: net expectancy per buy
    total_buys = sum(s["buys"] for s in sessions.values())
    total_net = sum(s["received"] - s["spent"] for s in sessions.values())
    print()
    if total_buys:
        print(f"Total buys: {total_buys}")
        print(f"Total net: {total_net:+.6f} SOL ({total_net / total_buys:+.6f} SOL/buy)")
    else:
        print("No buys with settlement amounts recorded yet.")
    print("Note: quote_spent/received are only populated when the seller reports them;")
    print("network + protocol fees are not yet itemized per trade.")

    return 0


if __name__ == "__main__":
    sys.exit(main())