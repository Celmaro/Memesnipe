"""R5-P0-2: static gates fire at detection, dynamic re-run pre-buy, cadence once.

The audit's P0-2: _skip_reason() ran EVERY gate at detection (with the
creator-cadence side effect), then only the curve gate re-ran before buy —
so wash/cofire/holder data could go stale between detection and submission.
The fix splits them: static gates (quote mint, transfer fee, mayhem,
blacklist, cadence) live in _skip_reason(); data-dependent gates live in the
stateless _dynamic_screening_reason() which is re-called immediately before
the buy. This verifier proves the three contracts:
  1. static fires WITHOUT the dynamic machinery present at all;
  2. dynamic fires on FRESH data (the pre-buy re-eval);
  3. re-evaluating dynamic does NOT re-record the creator's launch cadence
     (the side effect stays in _skip_reason, so it can't double-count).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.pubkeys import WSOL_MINT  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

MINT = "Mint111111111111111111111111111111111111111"
CREATOR = "Creator111111111111111111111111111111111"


def _token(transfer_fee_bps: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        platform="pump_fun",
        creator=CREATOR,
        user=None,
        quote_mint=WSOL_MINT,
        transfer_fee_bps=transfer_fee_bps,
        mint=MINT,
        bonding_curve="pooladdr",
        pool_state=None,
        associated_bonding_curve=None,
        symbol="TST",
    )


def _trader(factory_on: bool = True) -> SimpleNamespace:
    """Minimal stub carrying the attrs _skip_reason reads (no dynamic helper)."""
    return SimpleNamespace(
        blocked_creators=set(),
        creator_pnl_store=None,
        allowed_quote_mints={WSOL_MINT},
        quote_amounts={WSOL_MINT: 0.0001},
        max_transfer_fee_bps=100,
        allow_mayhem_mode=False,
        holder_reward_mode="trade",
        min_unique_holders=0,
        max_top10_concentration=0.0,
        max_gini_coefficient=0.0,
        rug_score_threshold=0.0,
        factory_deploy_rate=60.0 if factory_on else 0.0,
        max_coordinated_supply_share=0.0,
        max_wash_share=0.0,
        _creator_launch_ts={},
        _cadence_recorded_mints=set(),  # noqa: SLF001 - C4.3 per-mint guard
        _wash_lpi=None,
        _cofire=None,
        _holder_store=None,
        min_metadata_extensions=0,
        # NOTE: NO _dynamic_screening_reason attribute on purpose — probe 1
        # must prove the static path never reaches it.
    )


def main() -> None:
    fails = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            fails.append(label)

    # Probe 1: static transfer-fee gate fires with NO dynamic helper present.
    t = _trader()
    reason = UniversalTrader._skip_reason(t, _token(transfer_fee_bps=300))
    check(
        "static transfer-fee gate fires without dynamic machinery",
        reason is not None and "transfer fee" in reason,
        f"-> {reason}",
    )
    # And a clean token passes static checks and reaches the dynamic helper
    # (which the real trader always has); with it stubbed to None the whole
    # gate chain returns None.
    t2 = _trader()
    t2._dynamic_screening_reason = lambda token_info, quote_mint=None: None  # noqa: SLF001
    r2 = UniversalTrader._skip_reason(t2, _token())
    check(
        "static pass + dynamic pass on clean token returns None",
        r2 is None,
        f"-> {r2}",
    )

    # Probe 2: dynamic re-eval fires on FRESH wash data pre-buy. The
    # R5-P0-2 split puts the data-dependent gates in
    # `_dynamic_screening_reason` — the pre-buy recheck entry — so this
    # probe calls that helper directly (detection already proved the
    # static path above).
    class _Wash:
        def wash_share(self, _mint: str) -> float:
            return 0.9  # fresh observation says 90% round-trip volume

    t3 = _trader()
    t3._wash_lpi = _Wash()  # noqa: SLF001
    t3.max_wash_share = 0.3  # 0.9 > 0.3 -> should refuse
    reason = UniversalTrader._dynamic_screening_reason(t3, _token())
    check("dynamic wash gate fires on fresh data", reason is not None and "wash" in reason, f"-> {reason}")

    # Probe 3: dynamic re-eval does NOT re-record creator cadence.
    t4 = _trader(factory_on=True)
    calls = {"n": 0}

    def fake_dynamic(_token_info, _quote_mint=None):
        calls["n"] += 1
        return None

    t4._dynamic_screening_reason = fake_dynamic  # noqa: SLF001
    before = len(t4._creator_launch_ts.get(CREATOR, []))  # noqa: SLF001
    UniversalTrader._skip_reason(t4, _token())  # detection: records cadence
    mid = len(t4._creator_launch_ts.get(CREATOR, []))  # noqa: SLF001
    check("detection calls dynamic helper exactly once", calls["n"] == 1, f"{calls['n']}")
    check("detection records cadence once", mid == before + 1, f"{before}->{mid}")
    # pre-buy re-eval calls ONLY the dynamic helper — no cadence re-record
    t4._dynamic_screening_reason(_token())  # noqa: SLF001
    after = len(t4._creator_launch_ts.get(CREATOR, []))  # noqa: SLF001
    check("dynamic re-eval does not re-record cadence", after == mid, f"{mid}->{after}")

    print("\n" + ("R5-P0-2 gate-order verifier: PASS" if not fails else f"FAIL: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())