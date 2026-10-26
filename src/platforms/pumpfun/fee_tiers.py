"""Pump.fun market-cap-scaled fee tiers (R3-P0-1).

The Sept-2026 fee program (see pump-fun-sdk docs/pump-official/FEE_PROGRAM_README.md)
replaced the flat launch-tier fee with a **market-cap-scaled FeeConfig**:

    computeFeesBps(global, feeConfig, mintSupply, virtualSolReserves,
                   virtualTokenReserves) -> calculateFeeTier(feeTiers,
                   bondingCurveMarketCap)

where ``bondingCurveMarketCap = virtualSolReserves * mintSupply / virtualTokenReserves``
(SOL lamports). Fees only ever *decrease* as market cap grows, so pricing
every quote at the launch-tier maximum is conservative but leaves edge on the
table and mis-states cost at higher MC. This module resolves the live tier.

Conservative-fallback contract (never understates cost / never overstates
output): any decode failure, missing fee_config account, empty tier list, or
non-SOL pairing returns the launch-tier default (DEFAULT_PUMP_FEE_BPS) —
identical to the pre-tier behavior. The tier path is therefore a strict
improvement only when the fee_config account is readable and sane.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Canonical pump.fun total fee on SOL-paired launch-tier coins: 100 bps (1%).
#: Matches curve_manager.DEFAULT_PUMP_FEE_BPS.
DEFAULT_PUMP_FEE_BPS = 100
#: Protocol share of the fee, added on top of a decoded `creator_fee_bps`
#: when the curve carries a custom creator fee (curve_manager.PROTOCOL_FEE_BPS).
PROTOCOL_FEE_BPS = 25
#: Ceiling guard so a corrupt/foreign decode can never quote absurd fees.
MAX_FEE_BPS = 1000
#: FeeConfig account discriminator — first 8 bytes of SHA256("account:FeeConfig").
FEE_CONFIG_DISCRIMINATOR = b"\x8f\x34\x92\xbb\xdb\x7b\x4c\x9b"


@dataclass
class FeeTier:
    """One market-cap threshold band (mirrors the FeeTier IDL struct)."""

    market_cap_lamports_threshold: int = 0
    lp_fee_bps: int = 0
    protocol_fee_bps: int = 0
    creator_fee_bps: int = 0

    @property
    def total_bps(self) -> int:
        # Raw sum; ceiling enforcement belongs to the conservative guards in
        # total_fee_bps_for_market_cap / _resolve_tiered_fee_bps (they fall
        # back to DEFAULT when the total exceeds MAX_FEE_BPS — clamping here
        # would mask an absurd tier and defeat the fallback).
        return self.lp_fee_bps + self.protocol_fee_bps + self.creator_fee_bps


@dataclass
class FeeConfigState:
    """Decoded FeeConfig account (SOL tier list + stable tier list)."""

    bump: int = 0
    admin: str = ""
    flat_fees: FeeTier = field(
        default_factory=lambda: FeeTier(
            lp_fee_bps=75, protocol_fee_bps=25, creator_fee_bps=0
        )
    )
    fee_tiers: list[FeeTier] = field(default_factory=list)
    stable_fee_tiers: list[FeeTier] = field(default_factory=list)
    exotic_flat_fees: FeeTier = field(
        default_factory=lambda: FeeTier(
            lp_fee_bps=75, protocol_fee_bps=25, creator_fee_bps=0
        )
    )


def bonding_curve_market_cap_lamports(
    virtual_sol_reserves: int, virtual_token_reserves: int, mint_supply: int
) -> int:
    """Market cap in lamports: virtualSol * mintSupply / virtualToken.

    Matches the SDK's ``bondingCurveMarketCap``. Zero-division guarded: a
    corrupt curve returns 0 (falls into the first tier / default).
    """
    if virtual_token_reserves <= 0:
        return 0
    return (virtual_sol_reserves * mint_supply) // virtual_token_reserves


def calculate_fee_tier(fee_tiers: list[FeeTier], market_cap_lamports: int) -> FeeTier | None:
    """Pick the fee tier whose threshold the market cap meets.

    SDK semantics: the FIRST tier applies below its threshold; otherwise the
    highest threshold the cap is >= becomes active (walk tiers reversed and
    take the last threshold the cap clears).
    """
    if not fee_tiers:
        return None
    first = fee_tiers[0]
    if market_cap_lamports < first.market_cap_lamports_threshold:
        return first
    active: FeeTier | None = None
    for tier in fee_tiers:
        if market_cap_lamports >= tier.market_cap_lamports_threshold:
            active = tier
    return active or first


def total_fee_bps_for_market_cap(
    fee_tiers: list[FeeTier],
    market_cap_lamports: int,
    *,
    default_bps: int = DEFAULT_PUMP_FEE_BPS,
) -> int:
    """Total fee bps for a market cap under a tier list (fallback on empty)."""
    tier = calculate_fee_tier(fee_tiers, market_cap_lamports)
    if tier is None:
        return default_bps
    total = tier.total_bps
    return total if 0 < total <= MAX_FEE_BPS else default_bps


def decode_fee_config(data: bytes) -> FeeConfigState | None:
    """Decode a FeeConfig account payload (after the 8-byte discriminator).

    Returns None when the payload is not a recognizable FeeConfig (foreign
    program, truncated data, etc.) — the caller then falls back to the
    conservative launch-tier default.
    """
    if not data or len(data) < 8:
        return None
    if data[:8] != FEE_CONFIG_DISCRIMINATOR:
        # Not a FeeConfig account; do not guess.
        return None
    body = data[8:]
    # Minimum full layout: bump(1) + admin(32) + flat_fees(24) +
    # n_tiers(4) + n_stable(4) + exotic_flat_fees(24) = 89 bytes.
    # A shorter body is a truncated/corrupt account, not a valid empty
    # config (empty vecs still carry their 4-byte lengths).
    if len(body) < 89:
        return None
    # Layout (all little-endian):
    #   bump: u8
    #   admin: 32-byte pubkey
    #   flat_fees: Fees { lp u64, protocol u64, creator u64 }
    #   fee_tiers: vec length u32 + N * (threshold u128 + Fees)
    #   stable_fee_tiers: vec length u32 + N * (threshold u128 + Fees)
    #   exotic_flat_fees: Fees
    off = 0
    try:
        bump = body[off]
        off += 1
        admin = body[off : off + 32].hex()
        off += 32
        flat = _read_fees(body, off)
        off += 24
        n_tiers = int.from_bytes(body[off : off + 4], "little")
        off += 4
        tiers: list[FeeTier] = []
        for _ in range(n_tiers):
            threshold = int.from_bytes(body[off : off + 16], "little")
            off += 16
            fees = _read_fees(body, off)
            off += 24
            tiers.append(
                FeeTier(
                    market_cap_lamports_threshold=threshold,
                    lp_fee_bps=fees[0],
                    protocol_fee_bps=fees[1],
                    creator_fee_bps=fees[2],
                )
            )
        n_stable = int.from_bytes(body[off : off + 4], "little")
        off += 4
        stable: list[FeeTier] = []
        for _ in range(n_stable):
            threshold = int.from_bytes(body[off : off + 16], "little")
            off += 16
            fees = _read_fees(body, off)
            off += 24
            stable.append(
                FeeTier(
                    market_cap_lamports_threshold=threshold,
                    lp_fee_bps=fees[0],
                    protocol_fee_bps=fees[1],
                    creator_fee_bps=fees[2],
                )
            )
        exotic = _read_fees(body, off)
    except (IndexError, ValueError):
        # Truncated / corrupt payload: conservative default.
        return None
    return FeeConfigState(
        bump=bump,
        admin=admin,
        flat_fees=FeeTier(lp_fee_bps=flat[0], protocol_fee_bps=flat[1], creator_fee_bps=flat[2]),
        fee_tiers=tiers,
        stable_fee_tiers=stable,
        exotic_flat_fees=FeeTier(lp_fee_bps=exotic[0], protocol_fee_bps=exotic[1], creator_fee_bps=exotic[2]),
    )


def _read_fees(body: bytes, off: int) -> tuple[int, int, int]:
    return (
        int.from_bytes(body[off : off + 8], "little"),
        int.from_bytes(body[off + 8 : off + 16], "little"),
        int.from_bytes(body[off + 16 : off + 24], "little"),
    )
