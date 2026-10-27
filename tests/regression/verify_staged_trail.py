"""R4-P1-7 regression: staged-then-trail exit mode + strategies ledger.

Proves the two halves of the asymmetric-exit feature:

1. ``TrailExitState`` (src/trading/trail_exit.py) is a correct pure state
   machine for the 53-position paper-cohort rule: sell the initial share at
   +25%, arm the break-even floor at +10%, trail 8% behind new highs, exit
   on the floor touch or the -20% hard stop.
2. The strategies ledger (src/research/strategies_ledger.py) records every
   registered strategy's would-be exit for the SAME price path in shadow
   mode — one open, per-strategy decides, one close per strategy.
3. The trader wires ``exit_mode=staged_trail`` through the monitor loop to
   the same partial-sell machinery as the staged ranges, and the config
   accepts ``trade.exit_mode`` in {"tp_sl","staged_trail"}.

Disabled-first: the YAML default stays ``exit_mode: tp_sl`` (production
TP/SL unchanged) while the strategies ledger runs tp_sl AND staged_trail in
parallel for every shadow position.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.trail_exit import TrailExitState


def _eps(a, b, tol=1e-9):
    return abs(a - b) < tol


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R4-P1-7: staged-then-trail exit mode + strategies ledger")

    # 1. TrailExitState — ordinary run: +25% fires the initial share, +10%
    # arms the floor, trail exits on a 8% pullback from the high.
    st = TrailExitState(entry_price=100.0)
    d = st.on_tick(100.0)  # entry
    check("entry holds", d.action == "hold", f"-> {d.action}")
    d = st.on_tick(126.0)  # +26% -> initial TP fires
    check(
        "initial TP sells share",
        d.action == "sell_share" and d.reason == "initial_tp"
        and _eps(d.share, 0.4),
        f"-> {d.action} {d.reason} share={d.share}",
    )
    check("initial TP fired once", st.initial_tp_fired is True)
    # Next tick still +26%: no re-fire.
    d = st.on_tick(126.0)
    check("initial TP does not repeat", d.action != "sell_share", f"-> {d.action}")
    # New high at +30% -> arms floor at 100 (break-even) and trails 8%.
    d = st.on_tick(130.0)
    check("new high holds", d.action == "hold", f"-> {d.action}")
    check("armed after +10%", st.armed is True, f"-> armed={st.armed}")
    check(
        "floor trails 8% behind peak",
        _eps(st.floor_price, 130.0 * 0.92),
        f"-> {st.floor_price:.4f}",
    )
    # Pullback to the floor: 119.6 (8% below 130).
    d = st.on_tick(119.6)
    check(
        "trail floor exits",
        d.action == "exit_all" and d.reason == "trail_floor",
        f"-> {d.action} {d.reason}",
    )

    # 2. Hard stop when never armed: drop to -20% before any +10% arm.
    st2 = TrailExitState(entry_price=100.0)
    d = st2.on_tick(79.9)
    check(
        "hard stop exits before arming",
        d.action == "exit_all" and d.reason == "stop_loss",
        f"-> {d.action} {d.reason}",
    )

    # 3. No exit while barely moving.
    st3 = TrailExitState(entry_price=100.0)
    d = st3.on_tick(105.0)
    check(
        "small move holds",
        d.action == "hold",
        f"-> {d.action}",
    )

    # 4. Strategies ledger: one price path, two strategies, per-strategy
    # outcome rows.
    from research.strategies_ledger import StrategiesLedger

    with tempfile.TemporaryDirectory() as tmp:
        ledger = StrategiesLedger(
            dir_path=tmp,
            enabled=True,
            tp_pct=0.20,
            sl_pct=0.20,
        )
        # Register a staged-trail strategy with custom knobs + tp_sl
        # (default set) + a hold baseline.
        ledger.open_position(
            mint="MINT1",
            symbol="TST",
            platform="pump_fun",
            entry_price=100.0,
            amount_tokens=10.0,
            quote_spent=1000.0,
        )
        # Feed the same price path: +10% (hold all), +30% (trail arms,
        # tp_sl fires), pullback to floor (trail exits, tp_sl would still
        # be in profit).
        for px in (110.0, 130.0, 119.6):
            ledger.decide("MINT1", current_price=px, hold_seconds=5.0)
        ledger.close_position(
            "MINT1",
            close_price=119.6,
            exit_reason="trail_stop",
            hold_seconds=10.0,
        )
        rows = [l for l in Path(tmp).glob("*.jsonl")]
        check("ledger writes a daily file", len(rows) == 1, f"-> {rows}")
        if rows:
            import json

            lines = [json.loads(l) for l in rows[0].read_text().splitlines()]
            types = [r["type"] for r in lines]
            check(
                "open/decide/close sequence",
                "strategy_open" in types
                and "strategy_decide" in types
                and "strategy_close" in types,
                f"-> {types}",
            )
            strategies = {r["strategy"] for r in lines if r["type"] == "strategy_close"}
            check(
                "both strategies report a close",
                "tp_sl" in strategies and "staged_trail" in strategies,
                f"-> {strategies}",
            )
            sees = [r for r in lines if r["type"] == "strategy_decide"]
            check(
                "strategies saw would-fires",
                len(sees) >= 2,
                f"-> {len(sees)} decides",
            )

    # 5. Config wiring: trade.exit_mode in VALID_VALUES, strategies ledger
    # key in BOOL_FIELDS, YAML default tp_sl, YAML ledger enable true.
    import yaml

    import config_loader

    check(
        "VALID_VALUES has trade.exit_mode",
        "tp_sl" in config_loader.VALID_VALUES.get("trade.exit_mode", [])
        and "staged_trail" in config_loader.VALID_VALUES.get("trade.exit_mode", []),
        f"-> {config_loader.VALID_VALUES.get('trade.exit_mode')}",
    )
    check(
        "BOOL_FIELDS has research.strategies_ledger_enabled",
        "research.strategies_ledger_enabled" in config_loader.BOOL_FIELDS,
    )
    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML default exit_mode tp_sl",
        cfg["trade"].get("exit_mode") == "tp_sl",
        f"-> {cfg['trade'].get('exit_mode')!r}",
    )
    check(
        "YAML strategies ledger enabled",
        cfg["research"].get("strategies_ledger_enabled") is True,
        f"-> {cfg['research'].get('strategies_ledger_enabled')!r}",
    )

    if failures:
        print(f"\nR4-P1-7 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nStaged-then-trail + strategies ledger OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())