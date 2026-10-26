"""R3-P2-10 regression: session risk guards (daily-loss/budget/concurrency).

Covers the RiskTracker gates themselves (the trader wiring is covered by
verify_mirror_risk_gate and the 74-verifier suite):
- max_concurrent_positions caps open positions; record_close frees a slot.
- daily_loss_limit_sol blocks NEW buys after realized losses pass it, while
  record_close still runs (never traps money in a falling position).
- budget_sol caps session spend (record_buy + record_spend both accrue).
- 0 = off: no guard fires.
- record_spend counts toward budget WITHOUT opening a position (mirror buys).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.risk_guard import RiskTracker  # noqa: E402
import config_loader  # noqa: E402


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P2-10 session risk guards")

    # Concurrency cap: buy 2 (limit 2) then a 3rd is blocked; a close frees.
    rt = RiskTracker(max_concurrent_positions=2)
    check("buy 1 allowed", rt.record_buy(0.1) and rt.open_positions == 1)
    check("buy 2 allowed", rt.record_buy(0.1) and rt.open_positions == 2)
    check(
        "buy 3 blocked by max_concurrent_positions",
        not rt.record_buy(0.1) and "max_concurrent_positions" in (rt.blocked_reason or ""),
        f"-> {rt.blocked_reason}",
    )
    rt.record_close(0.0)
    check("close frees a slot", rt.open_positions == 1 and rt.record_buy(0.1))

    # Loss limit: once realized PnL passes the floor, NEW buys are blocked;
    # sells (record_close) still run.
    rt2 = RiskTracker(daily_loss_limit_sol=0.5)
    check("loss-limit buy allowed before limit", rt2.record_buy(0.1))
    rt2.record_close(-0.7)  # realized -0.7 <= -0.5 -> blocked
    check(
        "loss limit blocks new buy",
        not rt2.record_buy(0.1) and "daily loss limit" in (rt2.blocked_reason or ""),
        f"-> {rt2.blocked_reason}",
    )
    rt2.record_close(-0.3)  # sell path unaffected
    check(
        "sells still run under loss limit",
        rt2.realized_pnl_sol < -0.9,
        f"-> realized {rt2.realized_pnl_sol:.3f}",
    )

    # Budget: spend cap counts record_buy AND record_spend. A buy that CROSSES
    # the cap is allowed (you can't know final cost pre-slippage); the NEXT
    # buy is blocked — same crossing-then-block semantics as the loss limit.
    rt3 = RiskTracker(budget_sol=1.0)
    check("budget buy 1", rt3.record_buy(0.4))
    rt3.record_spend(0.4)  # mirror spend counts toward budget
    check(
        "mirror spend did not open a position",
        rt3.open_positions == 1,
        f"-> open={rt3.open_positions}",
    )
    check("crossing buy allowed", rt3.record_buy(0.3))  # spend -> 1.1
    check(
        "next buy blocked by budget",
        not rt3.record_buy(0.01) and "budget" in (rt3.blocked_reason or ""),
        f"-> {rt3.blocked_reason}",
    )

    # 0 = off: no guard fires.
    rt0 = RiskTracker()
    check("0 = off: buys never blocked", rt0.record_buy(5.0) and rt0.record_buy(5.0))

    # Config wiring: YAML default + validation rules.
    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML risk.max_concurrent_positions present",
        "max_concurrent_positions" in cfg.get("risk", {}),
        f"-> {cfg.get('risk', {}).get('max_concurrent_positions')!r}",
    )
    check(
        "YAML risk.budget_sol present (0 = off)",
        "budget_sol" in cfg.get("risk", {}),
    )
    check(
        "CONFIG_VALIDATION_RULES has risk keys",
        any(r[0] == "risk.max_concurrent_positions" for r in config_loader.CONFIG_VALIDATION_RULES)
        and any(r[0] == "risk.budget_sol" for r in config_loader.CONFIG_VALIDATION_RULES),
    )

    if failures:
        print(f"\nR3-P2-10 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P2-10 session risk guards OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())