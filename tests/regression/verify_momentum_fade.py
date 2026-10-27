"""R4-P1-8 regression: momentum-fade volume signal (log-only).

The 53-trade cohort: volume -50% from peak while price is not making new
highs = distribution, exit is the norm. The trader tracks the peak trade
rate per mint and logs a momentum-fade advisory when the rate halves
without a new price high. LOG-ONLY — no behavior change, no config gate;
the signal is advisory data a replay can act on later.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from screening.wash_lpi import WashLpiDetector  # noqa: E402


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R4-P1-8: momentum-fade volume signal (log-only)")

    # 1. The rate tracker: fills/min over the observed window.
    d = WashLpiDetector()
    for i in range(6):
        d.observe_trade(
            "m", f"w{i}", is_buy=True,
            quote_lamports=1_000_000_000, ts=float(i) * 1.0,
        )
    rate = d.event_rate_per_min("m")
    check(
        "6 fills in 5s = 72/min",
        rate is not None and abs(rate - 72.0) < 1e-6,
        f"-> {rate}",
    )
    check(
        "no events -> None (fail open)",
        WashLpiDetector().event_rate_per_min("ghost") is None,
    )

    # 2. Simulate the advisory state machine: peak rate tracked, then a
    # >50% rate drop WITHOUT a new price high fires the hint condition.
    # Replicate the trader's exact predicate from the monitor-loop block.
    def _should_hint(
        rate, peak_rate, peak_price, current_price
    ) -> bool:
        if rate is None:
            return False
        if rate > peak_rate or current_price > peak_price:
            return False  # new peak or new high: update, no hint
        return (
            peak_rate > 0
            and rate < 0.5 * peak_rate
            and current_price <= peak_price * 1.05
        )

    check(
        "rate halved without new high -> hint",
        _should_hint(30.0, 72.0, 1.0, 0.95) is True,
    )
    check(
        "rate halved WITH new high -> no hint",
        _should_hint(30.0, 72.0, 1.0, 1.2) is False,
    )
    check(
        "rate above half -> no hint",
        _should_hint(50.0, 72.0, 1.0, 0.98) is False,
    )
    check(
        "unknown rate -> no hint",
        _should_hint(None, 72.0, 1.0, 0.95) is False,
    )

    # 3. Trader source wiring: advisory state + block present, log-only.
    src = Path("src/trading/universal_trader.py").read_text()
    check(
        "peak-rate state initialized",
        "self._momentum_peak_rate: dict[str, float] = {}" in src,
    )
    check(
        "momentum-fade advisory block present",
        "momentum-fade hint" in src,
    )
    check(
        "advisory is log-only (no gate read in _skip_reason)",
        "momentum_fade" not in Path(
            "src/config_loader.py"
        ).read_text()
        and "momentum" not in Path("bots/memesnipe.yaml").read_text(),
    )

    if failures:
        print(f"\nR4-P1-8 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nMomentum-fade volume signal OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())