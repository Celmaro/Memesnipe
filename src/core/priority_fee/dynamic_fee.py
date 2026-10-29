import statistics
import time

from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.priority_fee import PriorityFeePlugin
from utils.logger import get_logger

logger = get_logger(__name__)


class DynamicPriorityFee(PriorityFeePlugin):
    """Dynamic priority fee plugin using getRecentPrioritizationFees.

    The RPC call is cached for `cache_ttl` seconds, keyed on the accounts
    list, so an in-place auction (T1.4) can re-price inside the race window
    without paying a 60-250ms RPC round-trip per buy: the first buy samples,
    subsequent buys within the TTL reuse the sample.
    """

    def __init__(self, client: SolanaClient, cache_ttl: float = 2.0):
        """Initialize the dynamic fee plugin.

        Args:
            client: Solana RPC client for network requests.
            cache_ttl: Seconds to reuse a sampled fee before re-sampling.
        """
        self.client = client
        self.cache_ttl = cache_ttl
        # Keyed on the accounts tuple: buy and sell pass different account
        # lists, so a single-entry cache would miss on every alternate send.
        self._cache: dict[tuple[str, ...], tuple[float, int | None]] = {}

    async def get_priority_fee(
        self, accounts: list[Pubkey] | None = None
    ) -> int | None:
        """Fetch the recent priority fee using getRecentPrioritizationFees.

        Args:
            accounts: List of accounts to consider for the fee calculation.
                     If None, the fee is calculated without specific account constraints.

        Returns:
            Optional[int]: Median priority fee in microlamports, or None if the request fails.
        """
        key: tuple[str, ...] = tuple(
            str(account) for account in accounts
        ) if accounts else ()
        now = time.monotonic()

        cached = self._cache.get(key)
        if cached is not None and (now - cached[0]) < self.cache_ttl:
            return cached[1]

        fee = await self._fetch_fee(accounts)
        self._cache[key] = (now, fee)
        if len(self._cache) > 16:  # bounded: at most a few account shapes
            self._cache = {key: (now, fee)}
        return fee

    async def _fetch_fee(self, accounts: list[Pubkey] | None = None) -> int | None:
        try:
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getRecentPrioritizationFees",
                "params": [[str(account) for account in accounts]] if accounts else [],
            }

            # A stalled endpoint must not hold up the buy: bound the sample at
            # 1.5s. On timeout the caller falls back to the fixed fee.
            response = await self.client.post_rpc(body, deadline_seconds=1.5)
            if not response or "result" not in response:
                logger.error(
                    "Failed to fetch recent prioritization fees: invalid response"
                )
                return None

            fees = [fee["prioritizationFee"] for fee in response["result"]]
            if not fees:
                logger.warning("No prioritization fees found in the response")
                return None

            # Get the 70th percentile of fees for faster processing
            # It means you're paying a fee that's higher than 70% of other transactions
            # Higher percentile = faster transactions but more expensive
            # Lower percentile = cheaper but slower transactions
            prior_fee = int(statistics.quantiles(fees, n=10)[-3])  # 70th percentile

            return prior_fee

        except Exception:
            logger.exception("Failed to fetch recent priority fee")
            return None
