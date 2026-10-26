"""R3-P1-6 regression: anti-bundler extreme-fast calibration (docs + validator).

The plan classifies this item as PARTIAL/documentation: ship the calibrated
pairs table in bots/README and a config validator asserting the pair is
internally consistent. No trading code changes are needed while
extreme_fast_mode stays off.

Covers:
- bots/README.md exists and carries the calibration table + the 6024 pitfall.
- The validator accepts the shipped config (extreme_fast_mode off).
- The validator REJECTS extreme_fast_mode on with an amount below the
  dev-bundle revert threshold (2,620,000 tokens).
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import config_loader  # noqa: E402


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P1-6 anti-bundler calibration")

    readme = Path("bots/README.md")
    check("bots/README.md exists", readme.exists())
    if readme.exists():
        text = readme.read_text()
        check(
            "README carries calibration table",
            "2_620_000" in text and "extreme_fast_token_amount" in text,
        )
        check(
            "README documents 6024 dust pitfall",
            "6024" in text and "Overflow" in text,
        )

    # The shipped config (extreme_fast off) must validate.
    base = copy.deepcopy(config_loader.load_bot_config("bots/memesnipe.yaml"))
    try:
        config_loader.validate_config(base)
        check("shipped config validates (mode off)", True)
    except ValueError as e:
        check("shipped config validates (mode off)", False, repr(e))

    # Mode on with a below-threshold amount must be rejected.
    bad = copy.deepcopy(base)
    bad["trade"]["extreme_fast_mode"] = True
    bad["trade"]["extreme_fast_token_amount"] = 2_000_000
    try:
        config_loader.validate_config(bad)
        check("below-threshold amount rejected", False)
    except ValueError as e:
        check(
            "below-threshold amount rejected",
            "2_620_000" in str(e),
            f"-> {e!s}",
        )

    # Mode on with an at-threshold amount must validate.
    ok = copy.deepcopy(base)
    ok["trade"]["extreme_fast_mode"] = True
    ok["trade"]["extreme_fast_token_amount"] = 3_560_000
    try:
        config_loader.validate_config(ok)
        check("at-threshold amount accepted", True)
    except ValueError as e:
        check("at-threshold amount accepted", False, repr(e))

    if failures:
        print(f"\nR3-P1-6 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P1-6 anti-bundler calibration OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())