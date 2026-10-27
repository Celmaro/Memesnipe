from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.priority_fee.dynamic_fee import DynamicPriorityFee
from core.priority_fee.fixed_fee import FixedPriorityFee
from utils.logger import get_logger

logger = get_logger(__name__)


class PriorityFeeManager:
    """Manager for priority fee calculation and validation."""

    def __init__(
        self,
        client: SolanaClient,
        enable_dynamic_fee: bool,
        enable_fixed_fee: bool,
        fixed_fee: int,
        extra_fee: float,
        hard_cap: int,
        bid_multiplier: float = 1.0,
        fee_cache_ttl: float = 2.0,
        # R4-P0-3: contention-mode racing. When armed (launch flood),
        # the effective bid multiplier becomes bid_multiplier * 
        # contention_multiplier, and the cap becomes min(hard_cap,
        # contention_cap) when the caller set one — so a fee surge can
        # never eat a whole position.
        contention_multiplier: float = 2.5,
    ):
        """Initialize the priority fee manager.

        Args:
            client: Solana RPC client for dynamic fee calculation.
            enable_dynamic_fee: Whether to enable dynamic fee calculation.
            enable_fixed_fee: Whether to enable fixed fee.
            fixed_fee: Fixed priority fee in microlamports.
            extra_fee: Percentage increase to apply to the base fee.
            hard_cap: Maximum allowed priority fee in microlamports.
            bid_multiplier: Auction bid (T1.4): multiply the sampled base fee
                by this before the extra_fee percentage. >1.0 raises the bid
                above the observed top fee to win the race; 1.0 = match the
                sample exactly. Applied to the dynamic sample; ignored for
                fixed-fee mode.
            fee_cache_ttl: How long the dynamic sampler reuses one RPC sample
                (seconds), so the auction re-prices without a hot-path RPC.
            contention_multiplier: R4-P0-3 — extra multiplier applied on top
                of bid_multiplier while contention is armed (launch flood);
                off until set_contention(True) is called.
        """
        self.client = client
        self.enable_dynamic_fee = enable_dynamic_fee
        self.enable_fixed_fee = enable_fixed_fee
        self.fixed_fee = fixed_fee
        self.extra_fee = extra_fee
        self.hard_cap = hard_cap
        self.bid_multiplier = bid_multiplier
        self.contention_multiplier = contention_multiplier
        # R4-P0-3: contention state (off by default).
        self._contention_armed = False
        self._contention_cap: int | None = None

        # Initialize plugins
        self.dynamic_fee_plugin = DynamicPriorityFee(client, cache_ttl=fee_cache_ttl)
        self.fixed_fee_plugin = FixedPriorityFee(fixed_fee)

    def set_contention(self, armed: bool, cap: int | None = None) -> None:
        """Arm/disarm contention-mode racing (R4-P0-3).

        When armed, ``calculate_priority_fee`` multiplies the bid by
        ``contention_multiplier`` and enforces ``min(hard_cap, cap)`` (the
        cap is the fraction-of-position ceiling the caller derives). Off
        (default) = today's behavior byte-for-byte.
        """
        self._contention_armed = bool(armed)
        self._contention_cap = cap

    @property
    def contention_armed(self) -> bool:
        return self._contention_armed

    async def calculate_priority_fee(
        self, accounts: list[Pubkey] | None = None
    ) -> int | None:
        """Calculate the priority fee based on the configuration.

        Args:
            accounts: List of accounts to consider for dynamic fee calculation.
                     If None, the fee is calculated without specific account constraints.

        Returns:
            Optional[int]: Calculated priority fee in microlamports, or None if no fee should be applied.
        """
        base_fee = await self._get_base_fee(accounts)
        if base_fee is None:
            return None

        if self.enable_dynamic_fee:
            # Auction bid (T1.4): raise the sampled base above the observed
            # top before applying the extra percentage. R4-P0-3: contention
            # racing (armed via set_contention) escalates the bid further
            # during a launch flood.
            multiplier = self.bid_multiplier
            if self._contention_armed:
                multiplier = self.bid_multiplier * self.contention_multiplier
                logger.warning(
                    f"Fee auction CONTENTION: multiplier {multiplier:.2f} "
                    f"(bid {self.bid_multiplier} x contention "
                    f"{self.contention_multiplier})"
                )
            auctioned = int(base_fee * multiplier)
            if multiplier != 1.0:
                logger.debug(
                    f"Fee auction: sampled={base_fee} bid={auctioned} "
                    f"(multiplier {multiplier})"
                )
            base_fee = auctioned

        # Apply extra fee (percentage increase)
        final_fee = int(base_fee * (1 + self.extra_fee))

        # Enforce cap. R4-P0-3: when contention is armed with a
        # fraction-of-position ceiling, the cap is min(hard_cap, ceiling)
        # so a fee surge can never eat the whole position.
        cap = self.hard_cap
        if self._contention_armed and self._contention_cap is not None:
            cap = min(self.hard_cap, self._contention_cap)
        if final_fee > cap:
            logger.warning(
                f"Calculated priority fee {final_fee} exceeds cap {cap}. "
                f"Applying cap."
            )
            final_fee = cap

        return final_fee

    async def _get_base_fee(self, accounts: list[Pubkey] | None = None) -> int | None:
        """Determine the base fee based on the configuration.

        Returns:
            Optional[int]: Base fee in microlamports, or None if no fee should be applied.
        """
        # Prefer dynamic fee if both are enabled
        if self.enable_dynamic_fee:
            dynamic_fee = await self.dynamic_fee_plugin.get_priority_fee(accounts)
            if dynamic_fee is not None:
                return dynamic_fee

        # Fall back to fixed fee if enabled
        if self.enable_fixed_fee:
            return await self.fixed_fee_plugin.get_priority_fee()

        # No priority fee if both are disabled
        return None
