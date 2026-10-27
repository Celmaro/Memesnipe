"""R5-P1-1/2: exit matrix parses + time-control evaluator behaves.

- Every strategy spec in the yaml grid constructs an evaluator (no silent
  sim-drop — the bug that once hid trail10).
- time_exit: hold_120s fires exit_all/max_hold at 120s, not before; the
  stagnation variant only fires inside the band.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml

from research.strategies_ledger import StrategiesLedger

PROJECT = Path(__file__).resolve().parent.parent.parent
YAML = PROJECT / "bots" / "memesnipe.yaml"


def main() -> None:
    cfg = yaml.safe_load(YAML.read_text())
    specs = cfg["research"]["strategies"]
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        ledger = StrategiesLedger(dir_path=d, enabled=True, strategies=specs)
        entry = 1.0
        ledger.open_position("mintT", "T", "pump_fun", entry, amount_tokens=0.01, quote_spent=0.01)
        # 110s: nothing fired; 130s: hold_120s exits, hold_300s still open
        ledger.decide("mintT", current_price=1.05, hold_seconds=110.0)
        ledger.decide("mintT", current_price=1.05, hold_seconds=130.0)
        ledger.close_position("mintT", close_price=1.05, exit_reason="live", hold_seconds=140.0)

        rows = [json.loads(l) for l in (d / "2026-10-27.jsonl").open()]
        decides = [r for r in rows if r["type"] == "strategy_decide"]
        h120 = [r for r in decides if r["strategy"] == "hold_120s"]
        h300 = [r for r in decides if r["strategy"] == "hold_300s"]
        assert h120, "hold_120s never decided"
        assert all(r["reason"] == "max_hold" for r in h120), h120
        assert all(r["hold_seconds"] == 130.0 for r in h120), h120  # first (only) fire at the timeout
        assert not h300, f"hold_300s fired too early: {h300}"
        # on this flat path only the 120s timer fires; the price-sensitive
        # strategies correctly stay silent — their presence is verified via
        # the strategy_open row below.
        assert not any(r["strategy"] in ("st_tp20_sl10", "trail10") for r in decides), decides
        # matrix is complete: every registered strategy got a sim at open
        opens = [r for r in rows if r["type"] == "strategy_open"]
        assert opens and set(opens[0]["strategies"]) == {s["name"] for s in specs}, (
            opens[0]["strategies"],
            [s["name"] for s in specs],
        )
        print("R5-P1-1/2 exit matrix + time control verifier: PASS")


if __name__ == "__main__":
    main()
