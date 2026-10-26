"""R3-P1-2 regression: staged TP/SL ranges.

Covers the pure range math:
- build_steps: band stepping, single-step (from==to) band, exact far edge.
- due_tp_steps: fires low-to-high, each step once, jump-past fires the step.
- due_sl_steps: fires high-to-low, band bottom is_final.
- ShareRange.from_dict: parse + reject bad step/share.

Plus the trader wiring checks:
- constructor parses ranges and defaults to off (empty).
- YAML default tp_ranges/sl_ranges empty.
- LIST_OF_RANGE_FIELDS registered; config validation accepts a valid range
  list and rejects a missing-key entry.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.staged_exit import (  # noqa: E402
    ShareRange,
    build_steps,
    due_sl_steps,
    due_tp_steps,
)
import config_loader  # noqa: E402


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P1-2 staged TP/SL ranges")

    # --- build_steps ---
    entry = 1.0
    rng = ShareRange(from_pct=0.4, to_pct=1.0, step=0.2, share=0.25)
    steps = build_steps(entry, rng)
    check(
        "TP band 0.4->1.0 step 0.2 yields 4 steps",
        len(steps) == 4,
        f"-> {steps}",
    )
    check(
        "TP steps at 1.4/1.6/1.8/2.0",
        all(
            abs(a - b) < 1e-9
            for a, b in zip(steps, [1.4, 1.6, 1.8, 2.0])
        ),
        f"-> {steps}",
    )
    single = build_steps(entry, ShareRange(0.5, 0.5, 0.1, 0.3))
    check(
        "single-step band (from==to) yields exactly one trigger",
        single == [1.5],
        f"-> {single}",
    )
    far = build_steps(entry, ShareRange(0.0, 0.35, 0.1, 0.5))
    check(
        "far edge included exactly (0.35 not a multiple of 0.1 -> append)",
        len(far) == 5 and abs(far[-1] - 1.35) < 1e-9,
        f"-> {far}",
    )

    # --- due_tp_steps: low-to-high, once, jump-past ---
    fired: set[tuple[int, float]] = set()
    due = due_tp_steps(1.0, 1.55, [rng], fired)
    check(
        "jump to 1.55 fires step 1.4 only",
        [s.trigger_price for s in due] == [1.4],
        f"-> {[s.trigger_price for s in due]}",
    )
    for s in due:
        fired.add((s.band_index, s.trigger_price))
    due2 = due_tp_steps(1.0, 1.55, [rng], fired)
    check(
        "already-fired step does not repeat",
        due2 == [],
        f"-> {due2}",
    )
    due3 = due_tp_steps(1.0, 2.1, [rng], fired)
    check(
        "jump past many levels fires the remaining steps (1.6, 1.8, 2.0)",
        [s.trigger_price for s in due3] == [1.6, 1.8, 2.0],
        f"-> {[s.trigger_price for s in due3]}",
    )

    # --- due_sl_steps: high-to-low, bottom is final ---
    sl = ShareRange(from_pct=-0.1, to_pct=-0.3, step=0.1, share=0.5)
    fired_sl: set[tuple[int, float]] = set()
    due_sl = due_sl_steps(1.0, 0.88, [sl], fired_sl)
    check(
        "SL fall to 0.88 fires top step 0.9 only",
        [s.trigger_price for s in due_sl] == [0.9],
        f"-> {[s.trigger_price for s in due_sl]}",
    )
    for s in due_sl:
        fired_sl.add((s.band_index, s.trigger_price))
    due_sl2 = due_sl_steps(1.0, 0.88, [sl], fired_sl)
    check("SL step does not repeat", due_sl2 == [], f"-> {due_sl2}")
    fired_sl2: set[tuple[int, float]] = set()
    due_sl3 = due_sl_steps(1.0, 0.5, [sl], fired_sl2)
    check(
        "SL crash past bottom fires 0.9 and marks 0.7 final",
        [s.trigger_price for s in due_sl3] == [0.9, 0.8, 0.7]
        and any(s.is_final for s in due_sl3),
        f"-> {[(s.trigger_price, s.is_final) for s in due_sl3]}",
    )

    # --- ShareRange.from_dict validation ---
    ok = ShareRange.from_dict({"from_pct": 0.4, "to_pct": 1.0, "step": 0.2, "share": 0.25})
    check("from_dict parses valid", ok.share == 0.25)
    try:
        ShareRange.from_dict({"from_pct": 0.4, "to_pct": 1.0, "step": 0, "share": 0.25})
        check("zero step rejected", False)
    except ValueError:
        check("zero step rejected", True)
    try:
        ShareRange.from_dict({"from_pct": 0.4, "to_pct": 1.0, "step": 0.2, "share": 1.5})
        check("share > 1 rejected", False)
    except ValueError:
        check("share > 1 rejected", True)

    # --- Trader wiring ---
    from trading.universal_trader import UniversalTrader

    t = object.__new__(UniversalTrader)
    t.tp_ranges = []
    t.sl_ranges = []
    t._tp_fired = set()  # noqa: SLF001
    t._sl_fired = set()  # noqa: SLF001
    check(
        "trader off by default (empty ranges)",
        not t.tp_ranges and not t.sl_ranges,
    )

    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML defaults tp_ranges []",
        cfg["trade"].get("tp_ranges") == [],
        f"-> {cfg['trade'].get('tp_ranges')!r}",
    )
    check(
        "YAML defaults sl_ranges []",
        cfg["trade"].get("sl_ranges") == [],
        f"-> {cfg['trade'].get('sl_ranges')!r}",
    )
    check(
        "LIST_OF_RANGE_FIELDS registered",
        "trade.tp_ranges" in config_loader.LIST_OF_RANGE_FIELDS,
    )

    # Config validation accepts a valid range and rejects a bad entry.
    good = {"trade": {"tp_ranges": [{"from_pct": 0.4, "to_pct": 1.0, "step": 0.2, "share": 0.25}]}}
    import copy

    cfg_good = copy.deepcopy(config_loader.load_bot_config("bots/memesnipe.yaml"))
    cfg_good["trade"]["tp_ranges"] = good["trade"]["tp_ranges"]
    try:
        config_loader.validate_config(cfg_good)
        check("valid tp_ranges accepted", True)
    except ValueError as e:
        check("valid tp_ranges accepted", False, repr(e))
    cfg_bad = copy.deepcopy(cfg_good)
    cfg_bad["trade"]["tp_ranges"] = [{"from_pct": 0.4}]  # missing keys
    try:
        config_loader.validate_config(cfg_bad)
        check("missing-key range rejected", False)
    except ValueError:
        check("missing-key range rejected", True)

    if failures:
        print(f"\nR3-P1-2 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P1-2 staged TP/SL ranges OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())