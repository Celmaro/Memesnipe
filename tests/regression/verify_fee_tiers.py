"""R3-P0-1 regression: market-cap-scaled pump.fun fee tiers.

Covers:
- bonding_curve_market_cap_lamports math (SDK formula: virtualSol*mintSupply/virtualToken).
- calculate_fee_tier tier pick (first below threshold, highest above).
- total_fee_bps_for_market_cap conservative fallback (empty list -> default).
- decode_fee_config: real Anchor discriminator, u128 thresholds, corrupt/truncated
  payloads fail open to None (caller keeps launch-tier default).
- curve_manager integration: `_resolve_tiered_fee_bps` returns None on no
  fee_config / empty tiers (never understates cost), and returns the active
  tier total when a sane tier list is present. Conservative contract: any
  failure keeps the flat launch-tier default.
"""

import asyncio
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from platforms.pumpfun.fee_tiers import (  # noqa: E402
    FEE_CONFIG_DISCRIMINATOR,
    MAX_FEE_BPS,
    FeeTier,
    bonding_curve_market_cap_lamports,
    calculate_fee_tier,
    decode_fee_config,
    total_fee_bps_for_market_cap,
)
from platforms.pumpfun.curve_manager import PumpFunCurveManager  # noqa: E402

DEFAULT = 100


def _fees(lp: int, protocol: int, creator: int) -> bytes:
    return struct.pack("<QQQ", lp, protocol, creator)


def _tier(threshold: int, lp: int, protocol: int, creator: int) -> bytes:
    return threshold.to_bytes(16, "little") + _fees(lp, protocol, creator)


def _synthetic_fee_config(tiers: list[tuple[int, int, int, int]]) -> bytes:
    body = struct.pack("<B", 1) + b"\x11" * 32 + _fees(75, 25, 0)
    body += struct.pack("<I", len(tiers))
    for t in tiers:
        body += _tier(*t)
    body += struct.pack("<I", 0)  # stable_fee_tiers empty
    body += _fees(75, 25, 0)  # exotic_flat_fees
    return FEE_CONFIG_DISCRIMINATOR + body


def test_market_cap_formula() -> None:
    # Canonical launch: 30 SOL virtual / 1.073B virtual tokens -> 30 SOL lamports MC
    mcap = bonding_curve_market_cap_lamports(
        30_000_000_000, 1_073_000_000_000_000, 1_073_000_000_000_000
    )
    assert mcap == 30_000_000_000
    # MC grows with supply (supply > virtual tokens)
    mcap2 = bonding_curve_market_cap_lamports(
        30_000_000_000, 1_073_000_000_000_000, 2_000_000_000_000_000
    )
    assert mcap2 > mcap
    # Zero-division guard -> 0 (first tier / default)
    assert bonding_curve_market_cap_lamports(1, 0, 1) == 0


def test_tier_selection() -> None:
    tiers = [
        FeeTier(0, 75, 25, 0),  # launch: 100 bps
        FeeTier(30_000_000_000_000, 50, 15, 0),  # 65 bps
        FeeTier(69_000_000_000_000, 25, 10, 0),  # 35 bps
    ]
    assert calculate_fee_tier(tiers, 1_000_000) is not None
    assert calculate_fee_tier(tiers, 1_000_000).total_bps == 100  # type: ignore[union-attr]
    assert calculate_fee_tier(tiers, 40_000_000_000_000) is not None
    assert calculate_fee_tier(tiers, 40_000_000_000_000).total_bps == 65  # type: ignore[union-attr]
    assert calculate_fee_tier(tiers, 100_000_000_000_000) is not None
    assert calculate_fee_tier(tiers, 100_000_000_000_000).total_bps == 35  # type: ignore[union-attr]
    assert calculate_fee_tier(tiers, 29_999_999_999_999) is not None
    assert calculate_fee_tier(tiers, 29_999_999_999_999).total_bps == 100  # type: ignore[union-attr]
    # Empty tier list: no tier to pick -> None; the conservative fallback
    # (total_fee_bps_for_market_cap) handles the DEFAULT. Covered below.
    assert calculate_fee_tier([], 1_000) is None


def test_conservative_fallback() -> None:
    assert total_fee_bps_for_market_cap([], 123) == DEFAULT
    assert total_fee_bps_for_market_cap(
        [FeeTier(0, 200, 0, 0)], 1_000
    ) == 200  # sane total honored
    # absurd total above ceiling -> default (never overstate)
    assert total_fee_bps_for_market_cap([FeeTier(0, 2000, 0, 0)], 1_000) == DEFAULT
    assert 0 < MAX_FEE_BPS < 10_000


def test_decode_fee_config() -> None:
    payload = _synthetic_fee_config(
        [
            (0, 75, 25, 0),
            (30_000_000_000_000, 50, 15, 0),
            (69_000_000_000_000, 25, 10, 0),
        ]
    )
    cfg = decode_fee_config(payload)
    assert cfg is not None
    assert len(cfg.fee_tiers) == 3
    assert [t.total_bps for t in cfg.fee_tiers] == [100, 65, 35]
    assert cfg.fee_tiers[0].lp_fee_bps == 75 and cfg.fee_tiers[0].protocol_fee_bps == 25
    # corrupt / truncated / foreign payloads -> None (conservative default)
    assert decode_fee_config(b"") is None
    assert decode_fee_config(b"\x00" * 10) is None
    assert decode_fee_config(FEE_CONFIG_DISCRIMINATOR + b"AAAA") is None


class _FakeClient:
    """Returns truthy curve data for pool reads; fee_config payload for the
    fee_config PDA; None (or corrupt bytes) otherwise — matching the
    conservative-fallback cases."""

    def __init__(self, fee_config_data: bytes | None):
        self._fee_config_data = fee_config_data
        self._curve_data = b"\x01"  # any truthy bytes; fake parser ignores it
        from platforms.pumpfun.address_provider import PumpFunAddresses

        self._fee_config_addr = PumpFunAddresses.find_fee_config()

    async def get_account_info(self, address, commitment=None):
        class _Acc:
            data = None
        acc = _Acc()
        if address == self._fee_config_addr:
            acc.data = self._fee_config_data
        else:
            acc.data = self._curve_data
        return acc

    async def get_multiple_accounts(self, addresses, commitment=None):
        # Used by get_pool_state_and_token_program: [pool, mint]
        class _Acc:
            data = None
            owner = None
        return [_Acc(), _Acc()]


class _FakeIdlParser:
    def decode_account_data(self, *args, **kwargs):
        return {
            "virtual_quote_reserves": 40_000_000_000,
            "virtual_sol_reserves": 40_000_000_000,
            "virtual_token_reserves": 1_073_000_000_000_000,
            "token_total_supply": 1_200_000_000_000_000,
            "is_sol_paired": True,
            "graduated": False,
        }


async def _test_curve_manager_integration() -> None:
    from solders.pubkey import Pubkey

    mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
    pool = Pubkey.from_string("So11111111111111111111111111111111111111112")
    # Tier list where 40B SOL lamports MC (40 SOL) -> 65 bps tier
    # (30B <= 40B < 69B lamports)
    good_payload = _synthetic_fee_config(
        [
            (0, 75, 25, 0),
            (30_000_000_000, 50, 15, 0),
            (69_000_000_000, 25, 10, 0),
        ]
    )
    cm = PumpFunCurveManager(_FakeClient(good_payload), _FakeIdlParser())
    # get_pool_state_and_token_program uses get_multiple_accounts; but our
    # FakeClient only returns data via get_account_info. Drive the tier
    # resolution through get_pool_state instead (single-account path).
    curve = await cm.get_pool_state(pool)
    assert curve["fee_bps"] == 65, curve.get("fee_bps")
    assert abs(curve["fee_fraction"] - 65 / 10_000) < 1e-9

    # No fee_config -> conservative default (100 bps), no crash
    cm2 = PumpFunCurveManager(_FakeClient(None), _FakeIdlParser())
    curve2 = await cm2.get_pool_state(pool)
    assert curve2["fee_bps"] == DEFAULT, curve2.get("fee_bps")

    # Corrupt fee_config -> conservative default
    cm3 = PumpFunCurveManager(_FakeClient(b"\x00" * 16), _FakeIdlParser())
    curve3 = await cm3.get_pool_state(pool)
    assert curve3["fee_bps"] == DEFAULT, curve3.get("fee_bps")


def main() -> None:
    test_market_cap_formula()
    test_tier_selection()
    test_conservative_fallback()
    test_decode_fee_config()
    asyncio.run(_test_curve_manager_integration())
    print("R3-P0-1 fee-tier verification OK")


if __name__ == "__main__":
    main()
