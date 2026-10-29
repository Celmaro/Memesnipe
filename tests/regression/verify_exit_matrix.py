"""R5-P1-1/2: exit matrix parses + time-control evaluator behaves.

- Every strategy spec in the yaml grid constructs an evaluator (no silent
  sim-drop — the bug that once hid trail10).
- time_exit: hold_120s fires exit_all/max_hold at 120s, not before; the
  stagnation variant only fires inside the band.

The time_exit cells were pruned from the live grid (2026-10-28 strategy
consolidation), so their behavior is verified with a FIXED fixture grid
here, not the production yaml — otherwise a grid change would break the
time-exit tests. The vocabulary per yaml: kind: staged_trail | tp_sl |
time_exit | hold.
"""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from research.strategies_ledger import StrategiesLedger

PROJECT = Path(__file__).resolve().parent.parent.parent
YAML = PROJECT / "bots" / "memesnipe.yaml"

# Fixed fixture grid: time_exit cells no longer ship in the live yaml, so
# exercise the evaluator directly plus one price-sensitive strategy that must
# stay silent on a flat path (matrix completeness is proven via the yaml open
# row below).
TIME_SPECS = [
    {"name": "hold_120s", "kind": "time_exit", "config": {"max_hold_s": 120}},
    {"name": "hold_300s", "kind": "time_exit", "config": {"max_hold_s": 300}},
    {"name": "stagn_600s", "kind": "time_exit", "config": {"max_hold_s": 600, "stagnation_band_pct": 0.02}},
    {"name": "staged", "kind": "staged_trail", "config": {"initial_tp_pct": 0.20, "initial_tp_share": 0.4, "trail_pct": 0.08, "hard_stop_pct": 0.25}},
]


def _rows(d: Path) -> list[dict]:
    return [
        json.loads(l)
        for l in (d / f"{datetime.now(UTC):%Y-%m-%d}.jsonl").open()
    ]


def check_time_control() -> None:
    """Time_exit evaluator: fires exactly at its max_hold_s, not before."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        ledger = StrategiesLedger(dir_path=d, enabled=True, strategies=TIME_SPECS)
        entry = 1.0
        ledger.open_position("mintT", "T", "pump_fun", entry, amount_tokens=0.01, quote_spent=0.01)
        # 110s: nothing fired; 130s: hold_120s exits, hold_300s still open
        ledger.decide("mintT", current_price=1.05, hold_seconds=110.0)
        ledger.decide("mintT", current_price=1.05, hold_seconds=130.0)
        ledger.close_position("mintT", close_price=1.05, exit_reason="live", hold_seconds=140.0)

        rows = _rows(d)
        decides = [r for r in rows if r["type"] == "strategy_decide"]
        h120 = [r for r in decides if r["strategy"] == "hold_120s"]
        h300 = [r for r in decides if r["strategy"] == "hold_300s"]
        stagn = [r for r in decides if r["strategy"] == "stagn_600s"]
        assert h120, "hold_120s never decided"
        assert all(r["reason"] == "max_hold" for r in h120), h120
        assert all(r["hold_seconds"] == 130.0 for r in h120), h120  # first (only) fire at the timeout
        assert not h300, f"hold_300s fired too early: {h300}"
        assert not stagn, f"stagn_600s fired outside band: {stagn}"
        # price-sensitive strategy stays silent on a flat path
        assert not any(r["strategy"] == "staged" for r in decides), decides


def check_yaml_grid_complete() -> None:
    """Every strategy spec shipped in the yaml constructs an evaluator."""
    cfg = yaml.safe_load(YAML.read_text())
    specs = cfg["research"]["strategies"]
    assert specs, "yaml grid is empty"
    # A staged_trail runner-riding cell must exist (the live exit shape);
    # no time_exit cells ship anymore. tp_sl baseline was removed from the
    # grid 2026-10-29 per the user's runner-riding-only directive — the
    # fixed +20/-20 caps upside and cannot ride a runner.
    names = {s["name"] for s in specs}
    assert {"staged_trail"}.issubset(names), names
    assert not any(s["kind"] == "time_exit" for s in specs), names

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        ledger = StrategiesLedger(dir_path=d, enabled=True, strategies=specs)
        ledger.open_position("mintG", "G", "pump_fun", 1.0, amount_tokens=0.01, quote_spent=0.01)
        ledger.decide("mintG", current_price=1.05, hold_seconds=5.0)
        ledger.close_position("mintG", close_price=1.05, exit_reason="live", hold_seconds=10.0)
        opens = [r for r in _rows(d) if r["type"] == "strategy_open"]
        assert opens and set(opens[0]["strategies"]) == names, (
            opens[0]["strategies"] if opens else "no open row",
            names,
        )


def main() -> None:
    check_time_control()
    check_yaml_grid_complete()
    print("R5-P1-1/2 exit matrix + time control verifier: PASS")


if __name__ == "__main__":
    main()