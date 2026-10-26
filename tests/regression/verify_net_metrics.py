"""R3-P1-1 regression: execution-ledger net metrics.

Feeds a small known ledger (sim_open/sim_close pairs) and asserts the audit
metric set: total PnL, avg PnL/trade, profit factor, win rate, max drawdown,
fill rate, median hold, unsellable count, unparseable tolerance, and the
insufficient-data state (no closed trades -> no NaN / no divide-by-zero).
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from research.net_metrics import TradeMetrics, compute_metrics  # noqa: E402


def _write(fh, etype: str, mint: str, **extra) -> None:
    row = {"type": etype, "mint": mint, **extra}
    fh.write(json.dumps(row) + "\n")


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P1-1 net metrics")
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "day.jsonl"
        with ledger.open("w") as fh:
            # Trade 1: +0.10 quote, hold 60s
            _write(fh, "sim_open", "mintA", quote_spent=0.1, ts=1000.0)
            _write(
                fh,
                "sim_close",
                "mintA",
                quote_spent=0.1,
                pnl_quote=0.10,
                hold_seconds=60.0,
                ts=1060.0,
            )
            # Trade 2: -0.05 quote, hold 20s
            _write(fh, "sim_open", "mintB", quote_spent=0.05, ts=1061.0)
            _write(
                fh,
                "sim_close",
                "mintB",
                quote_spent=0.05,
                pnl_quote=-0.05,
                hold_seconds=20.0,
                ts=1081.0,
            )
            # Trade 3: +0.08, hold 90s
            _write(fh, "sim_open", "mintC", quote_spent=0.08, ts=1082.0)
            _write(
                fh,
                "sim_close",
                "mintC",
                quote_spent=0.08,
                pnl_quote=0.08,
                hold_seconds=90.0,
                ts=1172.0,
            )
            # Trade 4: opened but never closed -> unsellable
            _write(fh, "sim_open", "mintD", quote_spent=0.02, ts=1173.0)
            # One unparseable row
            fh.write("{not json}\n")
            # One unknown event type
            _write(fh, "weird_event", "mintE")

        m = compute_metrics([ledger], source="test")
        check("4 opened", m.opened == 4, f"-> {m.opened}")
        check("3 closed", m.closed == 3, f"-> {m.closed}")
        check("1 unsellable", m.unsellable == 1, f"-> {m.unsellable}")
        check(
            "2 unparseable rows (bad json + unknown event type)",
            m.unparseable_rows == 2,
            f"-> {m.unparseable_rows}",
        )
        check(
            "total PnL 0.13",
            abs(m.total_pnl_quote - 0.13) < 1e-9,
            f"-> {m.total_pnl_quote}",
        )
        check(
            "avg PnL/trade 0.0433333",
            abs((m.avg_pnl_quote_per_trade or 0) - 0.13 / 3) < 1e-9,
            f"-> {m.avg_pnl_quote_per_trade}",
        )
        check(
            "profit factor 3.6 (0.18 / 0.05)",
            abs((m.profit_factor or 0) - 3.6) < 1e-9,
            f"-> {m.profit_factor}",
        )
        check("win rate 2/3", abs((m.win_rate or 0) - 2 / 3) < 1e-9, f"-> {m.win_rate}")
        check(
            "max drawdown 0.05 (peak 0.10 -> trough 0.05)",
            abs(m.max_drawdown - 0.05) < 1e-9,
            f"-> {m.max_drawdown}",
        )
        check("fill rate 3/4", abs((m.fill_rate or 0) - 0.75) < 1e-9, f"-> {m.fill_rate}")
        check(
            "median hold 60s (sorted 20/60/90)",
            m.median_hold_seconds == 60.0,
            f"-> {m.median_hold_seconds}",
        )

        # Insufficient data: opened with no closes -> no NaN metrics
        empty_ledger = Path(tmp) / "empty.jsonl"
        with empty_ledger.open("w") as fh:
            _write(fh, "sim_open", "mintX", quote_spent=0.01)
        m2 = compute_metrics([empty_ledger], source="empty")
        check(
            "no closed trades -> fill_rate 0.0 (meaningful: all unsold)",
            m2.fill_rate == 0.0,
            f"-> {m2.fill_rate}",
        )
        check("no closed trades -> profit_factor None", m2.profit_factor is None)
        check("no closed trades -> avg None", m2.avg_pnl_quote_per_trade is None)
        check("no closed trades -> unsellable 1", m2.unsellable == 1)

        # Missing dir -> empty metrics, no raise
        m3 = compute_metrics([], source="none")
        check("no ledger -> opened 0", m3.opened == 0)
        check("no ledger -> total 0", m3.total_pnl_quote == 0.0)

    # TradeMetrics report renders without crashing on zero data
    n = TradeMetrics()
    check("empty report renders", "insufficient" in n.to_report())

    if failures:
        print(f"\nR3-P1-1 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P1-1 net metrics OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())