"""R6-P1: two-tier staged trail — the recommended baseline (Profile B).

External recommendation (2026-10-29): TP1 +20% sell 40%, TP2 +40% sell
30% of the ORIGINAL position, keep the final 30% as the runner, hard
stop -18%, trail the runner 18% behind the peak once price reaches +25%.
After TP1 fills the protective stop moves to at least entry (never give
a banked TP back to red).

Pins the pure state machine (src/trading/trail_exit.py) for that profile:
the two TP tiers fire at +20% and +40%, the runner share is what remains
after both, the post-TP1 floor protects the position, and the -18% hard
stop holds before any TP.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.trail_exit import TrailExitState  # noqa: E402


def _eps(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) < tol


def main() -> int:
    failures: list[str] = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("Profile B two-tier staged trail (TP1 +20/40%, TP2 +40/30%, sl20)")

    # Profile B knobs exactly as shipped in bots/memesnipe.yaml.
    def make() -> TrailExitState:
        return TrailExitState(
            entry_price=100.0,
            initial_tp_share=0.4,
            initial_tp_pct=0.20,
            second_tp_share=0.3,
            second_tp_pct=0.40,
            arm_pct=0.25,
            trail_pct=0.18,
            hard_stop_pct=0.20,
        )

    # --- hard stop before any TP ---
    st = make()
    check("entry holds", st.on_tick(100.0).action == "hold")
    d = st.on_tick(79.9)
    check(
        "-20% hard stop exits pre-TP",
        d.action == "exit_all" and d.reason == "stop_loss",
        f"-> {d.action} {d.reason}",
    )

    # --- TP1 at +20% sells 40% of the original position ---
    st = make()
    d = st.on_tick(120.0)
    check(
        "TP1 @ +20% sells 40%",
        d.action == "sell_share" and d.reason == "initial_tp" and _eps(d.share, 0.40),
        f"-> {d.action} {d.reason} share={d.share}",
    )
    check("TP1 marks fired", st.initial_tp_fired and not st.second_tp_fired)
    # Post-TP1 the protective floor moves to entry (banked TP not given back).
    d = st.on_tick(101.0)
    check("post-TP1 floor ~entry", _eps(st.floor_price, 100.0), f"-> {st.floor_price:.4f}")

    # --- TP2 at +40% sells the second share (30% of original = 50% of remaining) ---
    d = st.on_tick(140.0)
    check(
        "TP2 @ +40% sells 30% of original (50% of remaining)",
        d.action == "sell_share" and d.reason == "second_tp" and _eps(d.share, 0.50),
        f"-> {d.action} {d.reason} share={d.share}",
    )
    check("TP2 marks fired", st.second_tp_fired)

    # --- runner: final 30% trails 18% behind the peak, armed at +25% ---
    d = st.on_tick(160.0)
    check("runner new high holds", d.action == "hold", f"-> {d.action}")
    check("runner floor trails 18%", _eps(st.floor_price, 160.0 * 0.82), f"-> {st.floor_price:.4f}")
    d = st.on_tick(131.0)  # 160 * 0.8187 -> below 18% floor
    check(
        "runner trail floor exits",
        d.action == "exit_all" and d.reason == "trail_floor",
        f"-> {d.action} {d.reason}",
    )

    # --- no double fire / idempotent tiers ---
    st = make()
    st.on_tick(120.0)
    d = st.on_tick(120.0)
    check("TP1 does not re-fire", d.action != "sell_share", f"-> {d.action}")
    st.on_tick(140.0)
    d = st.on_tick(140.0)
    check("TP2 does not re-fire", d.action != "sell_share", f"-> {d.action}")

    # --- second TP disabled when share is 0 (backward compat) ---
    st = TrailExitState(
        entry_price=100.0,
        initial_tp_share=0.4,
        initial_tp_pct=0.20,
        second_tp_share=0.0,
    )
    st.on_tick(120.0)
    d = st.on_tick(150.0)
    check(
        "second_tp_share=0 => no TP2 fire",
        d.action != "sell_share",
        f"-> {d.action}",
    )

    if failures:
        print(f"\nProfile B two-tier trail FAILED ({len(failures)}): {failures}")
        return 1
    print("\nProfile B two-tier staged trail OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())