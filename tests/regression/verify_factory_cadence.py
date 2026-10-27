"""R5-P2-2: factory-cadence artifact — one launch is not 60/hour.

The old inline math floored the trailing-window span at 1/60h, so a
creator's first observed launch computed exactly 60 mints/hour and could
trip the factory gate (factory_deploy_rate) or inflate the rug score. The
shared cadence helper now returns None for <2 observations, and only the
≥2-launch rate is ever a number.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from screening.rug_score import cadence_mints_per_hour  # noqa: E402


def main() -> None:
    now = 1000.0
    # 1 observation -> None (artifact gone)
    assert cadence_mints_per_hour([900.0], now) is None
    # 2 launches -> rate over the elapsed window since the first launch
    # (same convention as the old inline math): [900, 930] at now=1000 is a
    # 100s window, so 2 / (100/3600) = 72/hr. The point is the denominator
    # is now real elapsed time, never the 1/60h floor.
    c2 = cadence_mints_per_hour([900.0, 930.0], now)
    assert c2 is not None and abs(c2 - 72.0) < 1e-6, c2
    # 2 launches with a 900s window (first at 100, now 1000) -> 8/hr
    c3 = cadence_mints_per_hour([100.0, 500.0], now)
    assert c3 is not None and abs(c3 - 8.0) < 1e-6, c3
    # empty -> None
    assert cadence_mints_per_hour([], now) is None
    print("R5-P2-2 factory-cadence artifact verifier: PASS")


if __name__ == "__main__":
    main()
