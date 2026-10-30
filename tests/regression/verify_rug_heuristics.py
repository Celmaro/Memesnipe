"""P2-EXIT-4 regression: slot-delta rug heuristics (hardcap.rs:62-118 port).

Reference behavior (solana-trading-bot hardcap.rs): force-sell when fewer
than 2 unique buyers are observed N slots after our buy, or when the pool is
drained right after entry. Guards:
1. The pure decision function fires on early drain and on too-few-buyers.
2. It fails open on missing data (unknown slot, no baseline, clock skew).
3. The trader wires the decision into the monitor loop only when enabled
   (ExitReason.RUG exists and the config keys are registered).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from screening.rug_heuristics import RugContext, rug_exit_reason

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    LAMPORTS_PER_SOL = 1_000_000_000

    # 1. Baseline: entry slot 1000, 5 buyers, 1 SOL depth.
    ctx = RugContext(entry_slot=1000, unique_buyers=5, entry_real_quote_lamports=1 * LAMPORTS_PER_SOL)

    # Healthy: 1 SOL now, 30 slots later, >2 buyers.
    r = rug_exit_reason(ctx, 1030, 0.9 * LAMPORTS_PER_SOL)
    check("healthy coin no exit", r is None, f"got {r!r}")

    # 2. Early drain: half the pool gone at +3 slots -> fires.
    r2 = rug_exit_reason(ctx, 1003, 0.4 * LAMPORTS_PER_SOL)
    check("early drain fires", r2 is not None and "drain" in r2, f"got {r2!r}")

    # 3. Too-few-buyers: 1 buyer at +12 slots -> fires.
    ctx1 = RugContext(entry_slot=1000, unique_buyers=1, entry_real_quote_lamports=1 * LAMPORTS_PER_SOL)
    r3 = rug_exit_reason(ctx1, 1012, 0.9 * LAMPORTS_PER_SOL)
    check("too-few-buyers fires", r3 is not None and "buyer" in r3, f"got {r3!r}")

    # 4. Too-few-buyers before the window: 1 buyer at +5 slots -> no fire.
    r4 = rug_exit_reason(ctx1, 1005, 0.9 * LAMPORTS_PER_SOL)
    check("too-few-buyers waits for window", r4 is None, f"got {r4!r}")

    # 5. Fails open: entry slot unknown.
    r5 = rug_exit_reason(RugContext(None, 1, 0), 1005, 0)
    check("unknown entry slot fails open", r5 is None)

    # 6. Fails open: current slot unknown.
    r6 = rug_exit_reason(ctx, None, 0)
    check("unknown current slot fails open", r6 is None)

    # 7. Fails open: clock skew (current < entry).
    r7 = rug_exit_reason(ctx, 999, 0)
    check("clock skew fails open", r7 is None)

    # 8. Drain needs a real baseline (entry depth 0 = unknown -> no fire).
    r8 = rug_exit_reason(RugContext(1000, 5, 0), 1002, 0)
    check("no depth baseline fails open", r8 is None)

    # 9. ExitReason.RUG exists.
    from trading.position import ExitReason

    check(
        "ExitReason.RUG exists",
        ExitReason.RUG.value == "rug_heuristics",
    )

    # 10. Config wiring.
    import yaml

    import config_loader

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML declares rug_heuristics_enabled",
        "rug_heuristics_enabled" in cfg.get("filters", {}),
    )
    check(
        "YAML rug_heuristics_enabled armed (fixing loop)",
        cfg["filters"].get("rug_heuristics_enabled", False) is True,
    )
    check(
        "BOOL_FIELDS includes it",
        "filters.rug_heuristics_enabled" in config_loader.BOOL_FIELDS,
    )
    check(
        "rug_check_slots validation exists",
        any(
            r[0] == "filters.rug_check_slots"
            for r in config_loader.CONFIG_VALIDATION_RULES
        ),
    )

    print()
    if FAILURES:
        print(f"P2-EXIT-4 FAILED: {len(FAILURES)} check(s) — {FAILURES}")
        return 1
    print("P2-EXIT-4 PASSED: slot-delta rug heuristics verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())