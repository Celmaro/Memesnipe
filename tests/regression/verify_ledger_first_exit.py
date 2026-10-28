"""R5-P0-1: ledger records each strategy's OWN first exit + net PnL.

Feed a synthetic position through StrategiesLedger: two marks — strategy A
fires at mark 1, strategy B at mark 2 — then close. Assert:

- decide rows carry first_exit_* fields pinned to the strategy's own trigger
- close rows carry first_exit_pnl_quote (strategy A's own first-exit PnL, not
  the live close price) and net_pnl_quote (fee-adjusted)
- a non-firing strategy (hold) has first_exit_price=None
"""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from research.cost_model import net_round_trip
from research.strategies_ledger import StrategiesLedger


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        spec = [
            {"name": "staged", "kind": "staged_trail", "config": {"initial_tp_pct": 0.20, "initial_tp_share": 0.4, "trail_pct": 0.08, "hard_stop_pct": 0.20}},
            {"name": "hold", "kind": "hold", "config": {}},
        ]
        ledger = StrategiesLedger(dir_path=d, enabled=True, strategies=spec)
        entry = 1.0
        ledger.open_position("mintA", "SYM", "pump_fun", entry, amount_tokens=0.01, quote_spent=0.01)

        # mark 1: +30% → staged fires its initial TP (first_exit pinned here)
        ledger.decide("mintA", current_price=1.30, hold_seconds=60.0)
        # mark 2: +50% → staged doesn't re-fire (first stays at 1.30); hold never fires
        ledger.decide("mintA", current_price=1.50, hold_seconds=120.0)
        ledger.close_position("mintA", close_price=1.50, exit_reason="live", hold_seconds=180.0)

        rows = [json.loads(l) for l in (d / f"{datetime.now(UTC):%Y-%m-%d}.jsonl").open()]
        decides = [r for r in rows if r["type"] == "strategy_decide"]
        closes = [r for r in rows if r["type"] == "strategy_close"]

        assert len(decides) == 1, f"want 1 decide row (staged fires once), got {len(decides)}"
        staged_decides = [r for r in decides if r["strategy"] == "staged"]
        assert staged_decides, "no staged decide row"
        # first_exit pinned to the FIRST fire (1.30), not the later mark (1.50)
        assert all(r["first_exit_price"] == 1.30 for r in staged_decides), staged_decides
        assert all(r["first_exit_reason"] == "initial_tp" for r in staged_decides), staged_decides

        staged_close = next(r for r in closes if r["strategy"] == "staged")
        assert staged_close["first_exit_price"] == 1.30, staged_close
        # staged's own first-exit pnl = +30% * amount (0.003)
        assert abs(staged_close["first_exit_pnl_quote"] - 0.003) < 1e-9, staged_close
        # C8.2: net pnl reflects the strategy's OWN blended exit path, not a
        # repricing of the whole position at the live close. The staged cell
        # exits 40% at its first TP (+30% @ 1.30) and the 60% remainder at
        # the live close (+50% @ 1.50): blended gross = 0.4*0.30 + 0.6*0.50
        # = 0.42, then fee/network-adjusted per the cost model.
        blended_gross = 0.4 * (1.30 - 1.0) + 0.6 * (1.50 - 1.0)
        assert abs(staged_close["realized_gross_pnl_pct"] - blended_gross) < 1e-9, staged_close
        assert (
            abs(staged_close["realized_gross_pnl_quote"] - blended_gross * 0.01) < 1e-12
        ), staged_close
        expected_net = net_round_trip(blended_gross, 0.01) * 0.01
        assert abs(staged_close["net_pnl_quote"] - expected_net) < 1e-9, staged_close
        # The old (defective) contract repriced the whole position at the
        # live close: 0.50 gross. The blended net must differ from it —
        # this is what makes C8.2 observable.
        old_net = net_round_trip(0.50, 0.01) * 0.01
        assert abs(expected_net - old_net) > 1e-6, "blended net must differ from live-close repricing"

        hold_close = next(r for r in closes if r["strategy"] == "hold")
        assert hold_close["first_exit_price"] is None, hold_close

        print("R5-P0-1 ledger first-exit verifier: PASS")


if __name__ == "__main__":
    main()
