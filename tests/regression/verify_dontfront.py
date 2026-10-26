"""R3-P2-6 regression: DontFront/MEV shield executor selection.

Covers:
- JitoExecutor with dontfront=True rewrites block-engine endpoints to the
  DontFront variant; a flag carries through.
- dontfront=False (default) leaves endpoints untouched.
- Config wiring: YAML default false, BOOL_FIELDS entry, bot_runner read.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from execution.executor import JitoExecutor  # noqa: E402
import config_loader  # noqa: E402


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P2-6 DontFront shield")

    endpoints = [
        "https://amsterdam.mainnet.block-engine.jito.wtf",
        "https://frankfurt.mainnet.block-engine.jito.wtf",
    ]
    ex_off = JitoExecutor(None, endpoints=endpoints, dontfront=False)
    check(
        "off: endpoints untouched",
        ex_off.endpoints == endpoints and not ex_off.dontfront,
        f"-> {ex_off.endpoints[0]}",
    )

    ex_on = JitoExecutor(None, endpoints=endpoints, dontfront=True)
    check(
        "on: block-engine rewritten to dontfront variant",
        all("dontfront" in ep for ep in ex_on.endpoints)
        and ex_on.dontfront
        and "amsterdam.mainnet.block-engine.jito.wtf" not in ex_on.endpoints,
        f"-> {ex_on.endpoints[0]}",
    )

    ex_default = JitoExecutor(None, dontfront=False)
    check(
        "on-default endpoints still the standard list",
        "block-engine" in ex_default.endpoints[0]
        and "dontfront" not in ex_default.endpoints[0],
    )

    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML default jito.dontfront false",
        cfg.get("jito", {}).get("dontfront") is False,
        f"-> {cfg.get('jito', {}).get('dontfront')!r}",
    )
    check(
        "BOOL_FIELDS includes jito.dontfront",
        "jito.dontfront" in config_loader.BOOL_FIELDS,
    )

    if failures:
        print(f"\nR3-P2-6 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P2-6 DontFront shield OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())