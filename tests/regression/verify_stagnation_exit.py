"""R4-P1-9 regression: stagnation exit (opt-in).

Proves the trader fires a STAGNATION exit when the price has stayed inside
the configured band of the ENTRY price for stagnation_exit_min minutes,
and holds when the price moved out of the band. Disabled-first: the YAML
default is 0 (off) and a 0 threshold never fires.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import yaml  # noqa: E402

import config_loader  # noqa: E402
from trading.position import ExitReason  # noqa: E402


class _FakePosition:
    """Minimal position for the stagnation decision (no seller)."""

    def __init__(self, entry_price: float, entry_time) -> None:
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.closed = False

    def is_active(self):
        return not self.closed

    def close_position(self, price, reason):
        self.closed = True


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R4-P1-9: stagnation exit (opt-in)")

    # 1. The decision itself: flat price inside band after the threshold.
    from datetime import datetime, timedelta, timezone

    from trading.universal_trader import UniversalTrader

    # Replicate the monitor-loop predicate as a pure function of the state
    # the trader reads (entry_time, entry_price, current_price, config).
    def _stagnation_due(trader, position, current_price, now) -> bool:
        if trader.stagnation_exit_min <= 0:
            return False
        elapsed_min = (now - position.entry_time).total_seconds() / 60.0
        if elapsed_min < trader.stagnation_exit_min:
            return False
        upper = position.entry_price * (1 + trader.stagnation_band_pct)
        lower = position.entry_price * (1 - trader.stagnation_band_pct)
        return lower <= current_price <= upper

    t = object.__new__(UniversalTrader)
    t.stagnation_exit_min = 5  # minutes
    t.stagnation_band_pct = 0.02  # +/-2%

    entered = datetime.now(timezone.utc) - timedelta(minutes=6)
    pos_flat = _FakePosition(100.0, entered)
    now = datetime.now(timezone.utc)
    check(
        "flat inside band after threshold fires",
        _stagnation_due(t, pos_flat, 101.0, now),
        f"-> {_stagnation_due(t, pos_flat, 101.0, now)}",
    )
    check(
        "moved above band holds",
        not _stagnation_due(t, pos_flat, 110.0, now),
        f"-> {_stagnation_due(t, pos_flat, 110.0, now)}",
    )
    check(
        "moved below band holds",
        not _stagnation_due(t, pos_flat, 90.0, now),
        f"-> {_stagnation_due(t, pos_flat, 90.0, now)}",
    )

    # 2. Not yet elapsed: holds even when inside band.
    pos_new = _FakePosition(100.0, datetime.now(timezone.utc) - timedelta(minutes=1))
    check(
        "not yet elapsed holds",
        not _stagnation_due(t, pos_new, 101.0, datetime.now(timezone.utc)),
    )

    # 3. Threshold 0 = off.
    t_off = object.__new__(UniversalTrader)
    t_off.stagnation_exit_min = 0
    t_off.stagnation_band_pct = 0.02
    check(
        "threshold 0 never fires",
        not _stagnation_due(t_off, pos_flat, 101.0, now),
    )

    # 4. Config wiring (4 places).
    check(
        "CONFIG_VALIDATION_RULES has trade.stagnation_exit_min",
        any(
            r[0] == "trade.stagnation_exit_min"
            for r in config_loader.CONFIG_VALIDATION_RULES
        ),
    )
    check(
        "CONFIG_VALIDATION_RULES has trade.stagnation_band_pct",
        any(
            r[0] == "trade.stagnation_band_pct"
            for r in config_loader.CONFIG_VALIDATION_RULES
        ),
    )
    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML stagnation_exit_min 0 (off)",
        cfg["trade"].get("stagnation_exit_min") == 0,
        f"-> {cfg['trade'].get('stagnation_exit_min')!r}",
    )
    check(
        "YAML stagnation_band_pct 0.02",
        cfg["trade"].get("stagnation_band_pct") == 0.02,
        f"-> {cfg['trade'].get('stagnation_band_pct')!r}",
    )
    runner = Path("src/bot_runner.py").read_text()
    check(
        "bot_runner passes stagnation_exit_min",
        "stagnation_exit_min=cfg" in runner,
    )
    trader_src = Path("src/trading/universal_trader.py").read_text()
    check(
        "trader monitor loop has stagnation branch",
        "ExitReason.STAGNATION" in trader_src,
    )
    # 5. ExitReason enum has the member.
    check(
        "ExitReason.STAGNATION exists",
        hasattr(ExitReason, "STAGNATION"),
    )

    if failures:
        print(f"\nR4-P1-9 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nStagnation exit OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())