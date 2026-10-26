"""Pump.fun CurveManager: bonding curve reads and pricing, IDL-decoded."""

import time
from typing import Any

from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.pubkeys import (
    TOKEN_DECIMALS,
    cached_quote_units,
    is_sol_paired,
    normalize_quote_mint,
)
from interfaces.core import CurveGraduatedError, CurveManager, Platform
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)

#: Canonical pump.fun total fee on SOL-paired launch-tier coins: 100 bps (1%).
#: The program splits it ~25 bps protocol + 75 bps creator; the tiered
#: FeeConfig only ever reduces fees as market cap grows, so pricing every
#: quote at this launch-tier max is conservative (never understates cost,
#: never overstates output). Custom creator-fee coins derive a higher total
#: below.
DEFAULT_PUMP_FEE_BPS = 100
#: Protocol share of the fee, added on top of a decoded `creator_fee_bps`
#: when the curve carries a custom creator fee.
PROTOCOL_FEE_BPS = 25
#: Ceiling guard so a corrupt/foreign decode can never quote absurd fees.
MAX_FEE_BPS = 1000


def _ceil_div(a: int, b: int) -> int:
    """Ceiling integer division (SDK uses ceilDiv per fee component)."""
    return -(-a // b)


def fee_on_input(amount: int, fee_bps: int) -> int:
    """Fee charged on the input side: ceil(amount * fee_bps / 10000).

    Matches the pump.fun/SDK model: the fee is skimmed from the asset the
    trader provides (quote for buys, tokens for sells) before the
    constant-product swap, so the curve receives ``amount - fee``.
    """
    if amount <= 0 or fee_bps <= 0:
        return 0
    return _ceil_div(amount * fee_bps, 10_000)


def buy_amount_out(
    virtual_token_reserves: int,
    virtual_sol_reserves: int,
    amount_in: int,
    fee_bps: int,
) -> int:
    """Fee-aware pump.fun buy output: tokens received for ``amount_in`` SOL."""
    net_in = amount_in - fee_on_input(amount_in, fee_bps)
    if net_in <= 0:
        return 0
    return (net_in * virtual_token_reserves) // (virtual_sol_reserves + net_in)


def sell_amount_out(
    virtual_token_reserves: int,
    virtual_sol_reserves: int,
    amount_in: int,
    fee_bps: int,
) -> int:
    """Fee-aware pump.fun sell output: SOL received for ``amount_in`` tokens."""
    net_in = amount_in - fee_on_input(amount_in, fee_bps)
    if net_in <= 0:
        return 0
    return (net_in * virtual_sol_reserves) // (virtual_token_reserves + net_in)


def _total_fee_bps(creator_fee_bps: int) -> int:
    """Total pump.fun fee in bps for a curve.

    A decoded custom creator fee (custom pair / creator-fee coin) adds the
    protocol share on top; standard coins carry no per-curve creator fee in
    the decode and use the canonical launch-tier 100 bps total. Clamped so a
    corrupt/foreign decode can never quote absurd fees.
    """
    if creator_fee_bps and creator_fee_bps > 0:
        return min(creator_fee_bps + PROTOCOL_FEE_BPS, MAX_FEE_BPS)
    return DEFAULT_PUMP_FEE_BPS


U64_MAX = 2**64 - 1
U128_MAX = 2**128 - 1


def max_safe_sell_amount(
    token_balance: int, virtual_quote_reserves: int
) -> int:
    """Clamp an oversized sell to the on-chain safe bound (P2-FEE-2).

    pump.fun encodes the sell token amount as u64 and the curve math
    multiplies it by ``virtual_quote_reserves`` in u128; a raw token balance
    near completion (or a high-decimals coin) can overflow both and revert
    with AnchorError 6024 (Overflow). Matching the reference SDK's bound:
    ``min(u64::MAX, floor(0.9 * u128::MAX / virtualQuoteReserves))``.

    Returns the amount that can be sold in one instruction — strictly ≤ the
    requested balance. The caller re-runs per chunk (or per periodic sweep)
    to drain the remainder.
    """
    if token_balance <= 0:
        return 0
    if virtual_quote_reserves > 0:
        # 0.9 * u128::MAX, integer math (no float precision loss).
        bound = ((9 * U128_MAX) // 10) // virtual_quote_reserves
    else:
        bound = U64_MAX
    return min(token_balance, min(bound, U64_MAX))


class PumpFunCurveManager(CurveManager):
    """Pump.Fun implementation of CurveManager interface using IDL-based decoding."""

    #: How long a curve read may be reused. The quality gate reads pool state
    #: right before the buy, and the buy would otherwise read it again
    #: milliseconds later (T2.1: warm read). Exits poll every
    #: price_check_interval (>=1s), so a 1s TTL adds no stale-price exposure
    #: beyond the existing cadence.
    _POOL_STATE_TTL = 1.0

    def __init__(self, client: SolanaClient, idl_parser: IDLParser):
        """Initialize pump.fun curve manager with injected IDL parser.

        Args:
            client: Solana RPC client
            idl_parser: Pre-loaded IDL parser for pump.fun platform
        """
        self.client = client
        self._idl_parser = idl_parser
        # T2.1 warm-read cache: {pool_address: (fetched_at, state)}. Bounded by
        # the TTL; a commitment override still forces a fresh read.
        self._state_cache: dict[str, tuple[float, dict[str, Any]]] = {}

        logger.info("Pump.Fun curve manager initialized with injected IDL parser")

    @property
    def platform(self) -> Platform:
        """Get the platform this manager serves."""
        return Platform.PUMP_FUN

    async def get_pool_state(
        self, pool_address: Pubkey, commitment: str | None = None
    ) -> dict[str, Any]:
        """Get the current state of a pump.fun bonding curve.

        Reads are cached for a short TTL so the pre-buy quality gate and the
        buy itself share one RPC call; exits poll on their own cadence and are
        not materially delayed by the 1s window.

        Args:
            pool_address: Address of the bonding curve
            commitment: Optional commitment override. Pass "processed" right
                after a geyser/logs event so the BC is readable in the same
                slot it was created (default: confirmed, ~1-2 slot lag).

        Returns:
            Dictionary containing bonding curve state data
        """
        key = str(pool_address)
        if commitment is None:
            cached = self._state_cache.get(key)
            if cached is not None:
                fetched_at, state = cached
                if (time.monotonic() - fetched_at) < self._POOL_STATE_TTL:
                    return state

        try:
            account = await self.client.get_account_info(
                pool_address, commitment=commitment
            )
            if not account.data:
                raise ValueError(f"No data in bonding curve account {pool_address}")

            # Decode bonding curve state using injected IDL parser
            curve_state_data = self._decode_curve_state_with_idl(account.data)

            if commitment is None:
                self._state_cache[key] = (time.monotonic(), curve_state_data)

            return curve_state_data

        except Exception as e:
            logger.exception("Failed to get curve state")
            raise ValueError(f"Invalid bonding curve state: {e!s}")

    async def get_pool_state_and_token_program(
        self, pool_address: Pubkey, mint: Pubkey, commitment: str | None = None
    ) -> tuple[dict[str, Any], Pubkey | None]:
        """Read curve state and the mint's owning token program together.

        One getMultipleAccounts round trip, so both values come from the same
        node and slot. A TokenInfo whose token program was guessed (the logs
        CreateEvent path defaults to Token-2022) can be corrected from the mint
        account's owner without a second, possibly inconsistent read.

        Args:
            pool_address: Address of the bonding curve
            mint: Token mint whose owner identifies the token program
            commitment: Optional commitment override (see get_pool_state)

        Returns:
            Tuple of (decoded curve state, token program id or None if the
            mint account was not readable)

        Raises:
            ValueError: If the bonding curve account is missing or undecodable
        """
        try:
            curve_account, mint_account = await self.client.get_multiple_accounts(
                [pool_address, mint], commitment=commitment
            )
        except Exception as e:
            logger.exception("Failed to read curve and mint accounts")
            raise ValueError(f"Invalid bonding curve state: {e!s}") from e  # noqa: TRY003

        if curve_account is None or not curve_account.data:
            raise ValueError(f"No data in bonding curve account {pool_address}")  # noqa: TRY003

        curve_state_data = self._decode_curve_state_with_idl(curve_account.data)
        token_program = mint_account.owner if mint_account is not None else None
        return curve_state_data, token_program

    async def calculate_price(self, pool_address: Pubkey) -> float:
        """Calculate current token price from bonding curve state.

        Args:
            pool_address: Address of the bonding curve

        Returns:
            Current token price denominated in the curve's quote asset
            (SOL for SOL-paired coins, USDC for USDC-paired coins)

        Raises:
            ValueError: If the curve's quote mint has not been resolved, so no
                price can be expressed. Distinct from the 0.0 below: that is a
                curve with nothing in it, this is a curve nobody can price.
        """
        pool_state = await self.get_pool_state(pool_address)

        if pool_state["graduated"]:
            raise CurveGraduatedError(str(pool_address))
        if pool_state["virtual_token_reserves"] <= 0:
            return 0.0

        # _decode_curve_state_with_idl already scales by the quote mint's
        # decimals, so don't re-derive the price with a hardcoded 1e9 here.
        price = pool_state["price_per_token"]
        if price is None:
            raise ValueError(
                f"Cannot price {pool_address}: decimals for its quote mint "
                f"{pool_state['quote_mint']} are unresolved"
            )
        return price

    async def calculate_buy_amount_out(
        self, pool_address: Pubkey, amount_in: int
    ) -> int:
        """Calculate expected tokens received for a buy operation.

        Uses the pump.fun bonding curve formula to calculate token output.

        Args:
            pool_address: Address of the bonding curve
            amount_in: Amount of SOL to spend (in lamports)

        Returns:
            Expected amount of tokens to receive (in raw token units)
        """
        pool_state = await self.get_pool_state(pool_address)

        virtual_token_reserves = pool_state["virtual_token_reserves"]
        virtual_sol_reserves = pool_state["virtual_sol_reserves"]

        return buy_amount_out(
            virtual_token_reserves,
            virtual_sol_reserves,
            amount_in,
            pool_state.get("fee_bps", DEFAULT_PUMP_FEE_BPS),
        )

    async def calculate_sell_amount_out(
        self, pool_address: Pubkey, amount_in: int
    ) -> int:
        """Calculate expected SOL received for a sell operation.

        Uses the pump.fun bonding curve formula to calculate SOL output.

        Args:
            pool_address: Address of the bonding curve
            amount_in: Amount of tokens to sell (in raw token units)

        Returns:
            Expected amount of SOL to receive (in lamports)
        """
        pool_state = await self.get_pool_state(pool_address)

        virtual_token_reserves = pool_state["virtual_token_reserves"]
        virtual_sol_reserves = pool_state["virtual_sol_reserves"]

        return sell_amount_out(
            virtual_token_reserves,
            virtual_sol_reserves,
            amount_in,
            pool_state.get("fee_bps", DEFAULT_PUMP_FEE_BPS),
        )

    async def get_reserves(self, pool_address: Pubkey) -> tuple[int, int]:
        """Get current bonding curve reserves.

        Args:
            pool_address: Address of the bonding curve

        Returns:
            Tuple of (token_reserves, sol_reserves) in raw units
        """
        pool_state = await self.get_pool_state(pool_address)
        return (
            pool_state["virtual_token_reserves"],
            pool_state["virtual_sol_reserves"],
        )

    def _decode_curve_state_with_idl(self, data: bytes) -> dict[str, Any]:
        """Decode bonding curve state data using injected IDL parser.

        Args:
            data: Raw account data

        Returns:
            Dictionary with decoded bonding curve state

        Raises:
            ValueError: If IDL parsing fails
        """
        # Use injected IDL parser to decode BondingCurve account data
        decoded_curve_state = self._idl_parser.decode_account_data(
            data, "BondingCurve", skip_discriminator=True
        )

        if not decoded_curve_state:
            raise ValueError("Failed to decode bonding curve state with IDL parser")

        # BondingCurve renamed its SOL fields to quote fields when non-SOL quote
        # assets arrived, and appended quote_mint. The old names are kept as
        # aliases below so SOL-only callers keep working.
        raw_quote_mint = decoded_curve_state.get("quote_mint")
        quote_mint = normalize_quote_mint(
            Pubkey.from_string(raw_quote_mint)
            if isinstance(raw_quote_mint, str)
            else raw_quote_mint
        )
        # None for a quote mint nobody resolved. Decoding runs against every coin
        # a listener reports, most of which are never traded, so an unknown quote
        # asset leaves the quote-denominated fields unset rather than failing the
        # decode -- the caller's quote gate is what refuses the coin.
        quote_unit = cached_quote_units(quote_mint)

        curve_data = {
            "virtual_token_reserves": decoded_curve_state.get(
                "virtual_token_reserves", 0
            ),
            "virtual_quote_reserves": decoded_curve_state.get(
                "virtual_quote_reserves", 0
            ),
            "real_token_reserves": decoded_curve_state.get("real_token_reserves", 0),
            "real_quote_reserves": decoded_curve_state.get("real_quote_reserves", 0),
            "token_total_supply": decoded_curve_state.get("token_total_supply", 0),
            "complete": decoded_curve_state.get("complete", False),
            # The coin now trades on PumpSwap; the curve's reserves are zero.
            "graduated": decoded_curve_state.get("complete", False),
            "creator": decoded_curve_state.get("creator", ""),
            "is_mayhem_mode": decoded_curve_state.get("is_mayhem_mode", False),
            # Decoded for every coin. create_v2 no longer mints cashback coins
            # (6082 CashbackDeprecated), but this flag still drives the legacy
            # sell path's cashback account branch for coins that have it set.
            "is_cashback_coin": decoded_curve_state.get("is_cashback_coin", False),
            "is_holder_reward": decoded_curve_state.get("is_holder_reward", False),
            "creator_fee_bps": decoded_curve_state.get("creator_fee_bps", 0),
            "quote_mint": quote_mint,
            "is_sol_paired": is_sol_paired(quote_mint),
            # P2-FEE-1: total fee in bps. A decoded custom creator fee (custom
            # pair / configurable) adds the protocol share; standard coins use
            # the canonical launch-tier 100 bps total. Grabbed by the quote
            # methods and exposed as fee_fraction for the platform-aware
            # buyer/seller (which price from the curve, not these helpers).
            "fee_bps": _total_fee_bps(
                decoded_curve_state.get("creator_fee_bps", 0)
            ),
            "fee_fraction": _total_fee_bps(
                decoded_curve_state.get("creator_fee_bps", 0)
            )
            / 10_000,
        }

        # Back-compat aliases for the pre-quote-mint field names.
        curve_data["virtual_sol_reserves"] = curve_data["virtual_quote_reserves"]
        curve_data["real_sol_reserves"] = curve_data["real_quote_reserves"]

        # A graduated curve has handed its reserves to PumpSwap and has no
        # price; reporting that, rather than raising, lets a holder route the
        # sell to the pool.
        if curve_data["graduated"]:
            curve_data["price_per_token"] = None
            return curve_data

        # Validate reserves are positive before calculating price
        if curve_data["virtual_token_reserves"] <= 0:
            raise ValueError(
                f"Invalid virtual_token_reserves: {curve_data['virtual_token_reserves']} - cannot calculate price"
            )
        if curve_data["virtual_quote_reserves"] <= 0:
            raise ValueError(
                f"Invalid virtual_quote_reserves: {curve_data['virtual_quote_reserves']} - cannot calculate price"
            )

        # Price is denominated in the curve's quote asset, so scale by that
        # mint's decimals (1e9 for SOL, 1e6 for USDC) rather than assuming SOL.
        # None when those decimals are unknown: a price is unanswerable without
        # them, and a guessed power of ten would misprice the coin and inflate
        # the slippage cap the same way, compounding instead of cancelling.
        curve_data["price_per_token"] = (
            (
                curve_data["virtual_quote_reserves"]
                / curve_data["virtual_token_reserves"]
            )
            * (10**TOKEN_DECIMALS)
            / quote_unit
            if quote_unit is not None
            else None
        )

        # Add convenience decimal fields
        curve_data["token_reserves_decimal"] = (
            curve_data["virtual_token_reserves"] / 10**TOKEN_DECIMALS
        )
        curve_data["quote_reserves_decimal"] = (
            curve_data["virtual_quote_reserves"] / quote_unit
            if quote_unit is not None
            else None
        )
        curve_data["sol_reserves_decimal"] = curve_data["quote_reserves_decimal"]

        logger.debug(
            f"Decoded curve state: virtual_token_reserves={curve_data['virtual_token_reserves']}, "
            f"virtual_quote_reserves={curve_data['virtual_quote_reserves']}, "
            f"quote_mint={quote_mint}, "
            f"price={curve_data['price_per_token']} quote/token"
        )

        return curve_data

    async def is_curve_complete(self, pool_address: Pubkey) -> bool:
        """Check if the bonding curve is complete (migrated to Raydium).

        Args:
            pool_address: Address of the bonding curve

        Returns:
            True if curve is complete, False otherwise
        """
        pool_state = await self.get_pool_state(pool_address)
        return pool_state.get("complete", False)

    def validate_curve_state_structure(self, pool_address: Pubkey) -> bool:
        """Validate that the curve state structure matches IDL expectations.

        Args:
            pool_address: Address of the bonding curve

        Returns:
            True if structure is valid, False otherwise
        """
        try:
            pool_state = self.get_pool_state(pool_address)

            required_fields = [
                "virtual_token_reserves",
                "virtual_sol_reserves",
                "real_token_reserves",
                "real_sol_reserves",
                "token_total_supply",
                "complete",
            ]

            for field in required_fields:
                if field not in pool_state:
                    logger.error(f"Missing required field: {field}")
                    return False

                if field != "complete" and not isinstance(pool_state[field], int):
                    logger.error(
                        f"Field {field} is not an integer: {type(pool_state[field])}"
                    )
                    return False

            return True

        except Exception:
            logger.exception("Curve state validation failed")
            return False
