"""Honeypot / rug gates (T2.5), ported from solana-trading-bot's filters.

Two checks are shipped, both off by default:

- ``freeze_authority``: the mint carries a freeze authority (a freeze
  instruction can lock every holder's balance — an instant honeypot). Decoded
  from the raw mint account: both SPL Token and Token-2022 fixed-layout mints
  store the option byte at offset 45 (1 = authority present, 0 = none).
  Token-2022 extension-driven freeze authorities are not covered; the checks
  default to fail-open (pass) when the layout is not the standard one.

- ``burn_ratio``: the fraction of total supply not held in the bonding curve
  (`1 - real_token_reserves / token_total_supply`). On a fresh pump.fun
  launch that fraction is ~20% by design (the curve mints 1B and keeps ~793M);
  a coin holding far less than half its supply in the curve signals a rug
  structure (developer diverted or burned supply to control the price).

Both gates answer None (pass) on any RPC/parse error — a honeypot check must
never cost a live trade by failing open, but a coin that cannot be read is
not necessarily a honeypot, so this mirrors the reference's per-DEX default.

A reference to the SPL metadata program (Metaplex) is intentionally absent:
pump.fun coins carry no standard metadata account, and the token-2022
embedded-metadata case needs its own decoder. The mutable-metadata check from
the reference is left fail-open here until that decoder lands.
"""

from __future__ import annotations

from solders.pubkey import Pubkey

from core.client import SolanaClient
from interfaces.core import TokenInfo
from utils.logger import get_logger

logger = get_logger(__name__)

#: Offset of the freeze-authority option byte in a standard mint account
#: (SPL Token and Token-2022 fixed header): 1 => authority present.
_FREEZE_AUTHORITY_OPTION_OFFSET = 45


def has_freeze_authority(mint_account_data: bytes) -> bool:
    """True when the mint account carries a freeze authority.

    Args:
        mint_account_data: Raw account data of the mint (base64-decoded bytes)

    Returns:
        True if the option byte at offset 45 is set. Conventions vary by token
        program, so a too-short data buffer returns False (fail-open).
    """
    if len(mint_account_data) <= _FREEZE_AUTHORITY_OPTION_OFFSET:
        return False
    return mint_account_data[_FREEZE_AUTHORITY_OPTION_OFFSET] == 1


def burn_fraction(
    curve_state: dict, token_total_supply: float | None = None
) -> float | None:
    """Fraction of supply not held in the trading curve.

    Args:
        curve_state: Pool state dict from the curve manager
        token_total_supply: Optional supply override (raw units); default reads
            the curve state's own field

    Returns:
        Fraction 0..1, or None when either side is unknown/zero
    """
    supply = token_total_supply
    if supply is None:
        supply = curve_state.get("token_total_supply")
    real_reserves = curve_state.get("real_token_reserves")
    if not supply or not real_reserves:
        return None
    supply = float(supply)
    real = float(real_reserves)
    if supply <= 0:
        return None
    return max(0.0, 1.0 - real / supply)


async def honeypot_reason(
    client: SolanaClient,
    token_info: TokenInfo,
    curve_state: dict,
    *,
    check_freeze_authority: bool,
    min_curve_hold_fraction: float,
) -> str | None:
    """Why the honeypot gates refuse this coin, or None to trade it.

    Args:
        client: Solana RPC client for the mint account read
        token_info: Token being screened
        curve_state: Pool state already read by the curve gate (no extra RPC)
        check_freeze_authority: Run the freeze-authority gate
        min_curve_hold_fraction: Minimum share of total supply the curve must
            hold (0 disables the burn gate)

    Returns:
        A rejection reason string, or None when the coin passes (or the gate
        cannot be evaluated and is configured to fail open).
    """
    if check_freeze_authority:
        try:
            mint_account = await client.get_account_info(token_info.mint)
            if has_freeze_authority(mint_account.data):
                return "mint has a freeze authority (honeypot risk)"
        except Exception:
            logger.exception("freeze-authority check failed (fail-open)")

    if min_curve_hold_fraction > 0:
        frac = burn_fraction(curve_state)
        if frac is not None and (1.0 - frac) < min_curve_hold_fraction:
            return (
                f"curve holds only {(1.0 - frac) * 100:.1f}% of supply "
                f"(< min_curve_hold_fraction {min_curve_hold_fraction * 100:.0f}%)"
            )

    return None