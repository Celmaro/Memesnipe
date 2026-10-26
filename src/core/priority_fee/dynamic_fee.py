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
        self._cache: tuple[float, tuple[str, ...], int | None] | None = None

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

        if self._cache is not None:
            cached_at, cache_key, cached_fee = self._cache
            if key == cache_key and (now - cached_at) < self.cache_ttl:
                return cached_fee

        fee = await self._fetch_fee(accounts)
        self._cache = (now, key, fee)
        return fee

    async def _fetch_fee(self, accounts: list[Pubkey] | None = None) -> int | None:
        try:
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getRecentPrioritizationFees",
                "params": [[str(account) for account in accounts]] if accounts else [],
            }

            response = await self.client.post_rpc(body)
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
