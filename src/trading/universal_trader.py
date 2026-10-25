"""Universal trading coordinator that works with any platform."""

import asyncio
import json
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from time import monotonic

from solders.pubkey import Pubkey

from cleanup.modes import (
    handle_cleanup_after_failure,
    handle_cleanup_after_sell,
    handle_cleanup_post_session,
)
from core.client import SolanaClient
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import (
    WSOL_MINT,
    normalize_quote_mint,
    quote_decimals,
    quote_symbol,
    resolve_quote_amounts,
    resolve_quote_mint,
    resolve_quote_token_program,
)
from core.wallet import Wallet
from interfaces.core import (
    ConfirmationStatus,
    CurveGraduatedError,
    Platform,
    TokenInfo,
    TradeFailureReason,
)
from monitoring.listener_factory import ListenerFactory
from platforms import get_platform_implementations
from trading.base import TradeResult
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller
from trading.position import ExitReason, Position
from utils.logger import get_logger

# Try to use uvloop on Unix or winloop on Windows for better performance
# Fall back to standard asyncio if not available
try:
    if sys.platform == "win32":
        import winloop

        asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
    else:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    # Standard asyncio is fine, just slightly slower
    pass

logger = get_logger(__name__)

# Default for trade.max_exit_sell_attempts. The seller's own max_retries covers
# submission only, so a revert is retried in the monitor loop, where the price is
# re-read first. Bounded so a token that keeps reverting cannot pin the bot.
DEFAULT_MAX_EXIT_SELL_ATTEMPTS = 3


class ExitSellVerdict(Enum):
    """What to do after an exit sell came back unsuccessful.

    An exit sell is not idempotent, so "it did not succeed" is not enough to act
    on: a reverted sell changed nothing and should be retried, while one whose
    confirmation never arrived may already have emptied the position.

    `_classify_failed_exit_sell` only returns the three failure verdicts; SOLD is
    reported by the attempt itself.
    """

    SOLD = "sold"  # confirmed on this attempt, the ordinary happy path
    LATE_SUCCESS = "late_success"  # it landed after all — stop, do not resell
    RETRY = "retry"  # provably nothing happened on chain
    STOP = "stop"  # still unknown — a blind resell is the worse risk


def _exit_sell_verdict_for(reason: TradeFailureReason | None) -> ExitSellVerdict | None:
    """Map a failure reason to a verdict, where one can be decided offline.

    Args:
        reason: The reported failure reason, or None if the seller did not set
            one — which means "unknown", never "reverted".

    Returns:
        The verdict, or None when the signature has to be re-checked first.
    """
    if reason is TradeFailureReason.REVERTED:
        # The program errored and the tokens are still held.
        return ExitSellVerdict.RETRY
    if reason is TradeFailureReason.SUBMIT_FAILED:
        # Nothing was confirmed as sent and there is no signature to re-check.
        # Stranding the position is the worse of the two risks.
        return ExitSellVerdict.RETRY
    return None


def _resolve_quote_config(
    buy_amount: float,
    quote_amounts: dict[str, float] | None,
    allowed_quote_mints: list[str] | None,
) -> tuple[dict[Pubkey, float], set[Pubkey] | None]:
    """Resolve quote-asset configuration into per-mint amounts and an allowlist.

    Keys may be mint addresses or the aliases "sol"/"usdc". SOL always falls
    back to trade.buy_amount, so a config that never mentions quote assets
    keeps its existing SOL-only behaviour.

    Args:
        buy_amount: SOL amount per buy from trade.buy_amount
        quote_amounts: Optional map of quote mint -> amount in whole units
        allowed_quote_mints: Optional list of quote mints permitted to trade

    Returns:
        Tuple of (amount per quote mint, allowed quote mints or None for any)
    """
    amounts = {WSOL_MINT: buy_amount, **resolve_quote_amounts(quote_amounts)}
    allowed = (
        {resolve_quote_mint(mint) for mint in allowed_quote_mints}
        if allowed_quote_mints
        else None
    )
    return amounts, allowed


class UniversalTrader:
    """Universal trading coordinator that works with any supported platform."""

    #: Seconds shutdown cleanup may take before the process exits regardless.
    #: Cleanup settles for 15s per account before reading it, so this has to
    #: cover a session's worth of traded coins, not one.
    _CLEANUP_SHUTDOWN_BUDGET = 120.0

    def __init__(
        self,
        rpc_endpoint: str,
        wss_endpoint: str,
        private_key: str,
        buy_amount: float,
        buy_slippage: float,
        sell_slippage: float,
        # Platform configuration
        platform: Platform | str = Platform.PUMP_FUN,
        # Listener configuration
        listener_type: str = "logs",
        geyser_endpoint: str | None = None,
        geyser_api_token: str | None = None,
        geyser_auth_type: str = "x-token",
        # Trading configuration
        extreme_fast_mode: bool = False,
        extreme_fast_token_amount: int = 30,
        curve_refresh_budget: float = 2.0,
        *,
        trust_create_event: bool = True,
        # Quote asset configuration (pump.fun non-SOL pairs)
        quote_amounts: dict[str, float] | None = None,
        allowed_quote_mints: list[str] | None = None,
        max_transfer_fee_bps: int | None = None,
        # Exit strategy configuration
        exit_strategy: str = "time_based",
        take_profit_percentage: float | None = None,
        stop_loss_percentage: float | None = None,
        max_hold_time: int | None = None,
        price_check_interval: int = 10,
        max_exit_sell_attempts: int = DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
        # Persistent exit monitoring: after the bounded retries are spent, an
        # unsold position is watched and retried until sold or this TTL (secs).
        persistent_exit_ttl: int = 600,
        persistent_exit_interval: int = 15,
        # Priority fee configuration
        enable_dynamic_priority_fee: bool = False,
        enable_fixed_priority_fee: bool = True,
        fixed_priority_fee: int = 200_000,
        extra_priority_fee: float = 0.0,
        hard_cap_prior_fee: int = 200_000,
        # Retry and timeout settings
        max_retries: int = 3,
        wait_time_after_creation: int = 15,
        wait_time_after_buy: int = 15,
        wait_time_before_new_token: int = 15,
        max_token_age: int | float = 0.001,
        token_wait_timeout: int = 30,
        # Cleanup settings
        cleanup_mode: str = "disabled",
        cleanup_force_close_with_burn: bool = False,
        cleanup_with_priority_fee: bool = False,
        # Trading filters
        match_string: str | None = None,
        bro_address: str | None = None,
        blocked_creators: list[str] | None = None,
        max_market_cap_sol: float | None = None,
        min_curve_progress_sol: float | None = None,
        min_market_cap_sol: float | None = None,
        max_tracked_tokens: int = 500,
        # Split buy/sell priority fees: overrides for priority_fees.fixed_amount
        buy_priority_fee: int | None = None,
        sell_priority_fee: int | None = None,
        # Stuck-position wallet sweeper: seconds between scans for orphaned
        # tokens (0 = off).
        stuck_sweep_interval: int = 0,
        # Shadow/paper mode (#8): record simulated decisions and outcomes
        # without sending transactions or moving funds.
        shadow_mode: bool = False,
        marry_mode: bool = False,
        yolo_mode: bool = False,
        # Compute unit configuration
        compute_units: dict | None = None,
        # Node provider configuration
        max_rps: float = 25.0,
    ):
        """Initialize the universal trader."""
        # Core components
        self.solana_client = SolanaClient(rpc_endpoint, max_rps=max_rps)
        self.wallet = Wallet(private_key)
        self.priority_fee_manager = PriorityFeeManager(
            client=self.solana_client,
            enable_dynamic_fee=enable_dynamic_priority_fee,
            enable_fixed_fee=enable_fixed_priority_fee,
            fixed_fee=buy_priority_fee or fixed_priority_fee,
            extra_fee=extra_priority_fee,
            hard_cap=hard_cap_prior_fee,
        )
        # Improvement #6: split buy/sell fees. The buyer keeps the manager
        # above (buy fee); the seller gets its own manager with the sell fee
        # so sells never overpay with the urgent buy price.
        self.sell_priority_fee_manager = PriorityFeeManager(
            client=self.solana_client,
            enable_dynamic_fee=enable_dynamic_priority_fee,
            enable_fixed_fee=enable_fixed_priority_fee,
            fixed_fee=sell_priority_fee or fixed_priority_fee,
            extra_fee=extra_priority_fee,
            hard_cap=hard_cap_prior_fee,
        )

        # Platform setup
        if isinstance(platform, str):
            self.platform = Platform(platform)
        else:
            self.platform = platform

        logger.info(f"Initialized Universal Trader for platform: {self.platform.value}")

        # Validate platform support
        try:
            from platforms import platform_factory

            if not platform_factory.registry.is_platform_supported(self.platform):
                raise ValueError(f"Platform {self.platform.value} is not supported")
        except Exception:
            logger.exception("Platform validation failed")
            raise

        # Get platform-specific implementations
        self.platform_implementations = get_platform_implementations(
            self.platform, self.solana_client
        )

        # Store compute unit and quote-asset configuration
        self.compute_units = compute_units or {}
        self.quote_amounts, self.allowed_quote_mints = _resolve_quote_config(
            buy_amount, quote_amounts, allowed_quote_mints
        )
        # None takes every coin; a LaunchLab reward coin's transfer fee is paid
        # on the buy and again on the sell.
        self.max_transfer_fee_bps = max_transfer_fee_bps

        # Create platform-aware traders
        self.buyer, self.seller = (
            PlatformAwareBuyer(
                self.solana_client,
                self.wallet,
                self.priority_fee_manager,
                buy_amount,
                buy_slippage,
                max_retries,
                extreme_fast_token_amount,
                extreme_fast_mode,
                compute_units=self.compute_units,
                quote_amounts=self.quote_amounts,
                curve_refresh_budget=curve_refresh_budget,
                trust_create_event=trust_create_event,
            ),
            PlatformAwareSeller(
                self.solana_client,
                self.wallet,
                self.sell_priority_fee_manager,
                sell_slippage,
                max_retries,
                compute_units=self.compute_units,
            ),
        )

        # Initialize the appropriate listener with platform filtering
        self.token_listener = ListenerFactory.create_listener(
            listener_type=listener_type,
            wss_endpoint=wss_endpoint,
            geyser_endpoint=geyser_endpoint,
            geyser_api_token=geyser_api_token,
            geyser_auth_type=geyser_auth_type,
            platforms=[self.platform],  # Only listen for our platform
        )

        # Trading parameters
        self.buy_amount = buy_amount
        self.buy_slippage = buy_slippage
        self.sell_slippage = sell_slippage
        self.max_retries = max_retries
        self.extreme_fast_mode = extreme_fast_mode
        self.extreme_fast_token_amount = extreme_fast_token_amount

        # Exit strategy parameters
        self.exit_strategy = exit_strategy.lower()
        self.take_profit_percentage = take_profit_percentage
        self.stop_loss_percentage = stop_loss_percentage
        self.max_hold_time = max_hold_time
        # The attempt cap is clamped: below 1 would mean "never try to sell".
        self.price_check_interval, self.max_exit_sell_attempts = (
            price_check_interval,
            max(1, max_exit_sell_attempts),
        )
        self.persistent_exit_ttl = persistent_exit_ttl
        self.persistent_exit_interval = persistent_exit_interval

        # Timing parameters
        self.wait_time_after_creation = wait_time_after_creation
        self.wait_time_after_buy = wait_time_after_buy
        self.wait_time_before_new_token = wait_time_before_new_token
        self.max_token_age = max_token_age
        self.token_wait_timeout = token_wait_timeout

        # Cleanup parameters
        self.cleanup_mode = cleanup_mode
        self.cleanup_force_close_with_burn = cleanup_force_close_with_burn
        self.cleanup_with_priority_fee = cleanup_with_priority_fee

        # Trading filters/modes
        self.match_string = match_string
        self.bro_address = bro_address
        self.blocked_creators = {
            str(a) for a in (blocked_creators or [])
        }
        self.max_market_cap_sol = max_market_cap_sol
        self.min_curve_progress_sol = min_curve_progress_sol
        self.min_market_cap_sol = min_market_cap_sol
        self.max_tracked_tokens = max_tracked_tokens
        self.buy_priority_fee = buy_priority_fee
        self.sell_priority_fee = sell_priority_fee
        self.stuck_sweep_interval = stuck_sweep_interval
        self.shadow_mode = shadow_mode
        self.marry_mode = marry_mode
        self.yolo_mode = yolo_mode

        # State tracking
        self.traded_mints: set[Pubkey] = set()
        self.traded_token_programs: dict[
            str, Pubkey
        ] = {}  # Maps mint (as string) to token_program_id
        self.token_queue: asyncio.Queue = asyncio.Queue()
        self.processing: bool = False
        self.processed_tokens: set[str] = set()
        # Coins whose graduation has been logged, so it is said once.
        self._graduated_mints: set[Pubkey] = set()
        self.token_timestamps: dict[str, float] = {}
        self.token_launch_timestamps: dict[str, float] = {}
        # Positions whose bounded exit retries are spent: mint -> (token_info,
        # position). A background watcher keeps re-pricing and selling them
        # until they clear or the TTL expires, instead of abandoning them
        # ("no longer monitored" from the pre-persistent code).
        self._unsold_positions: dict[str, tuple[TokenInfo, Position]] = {}
        self._unsold_watcher_task: asyncio.Task | None = None
        # Orphaned-token sweeper task (improvement #3), started when
        # stuck_sweep_interval > 0.
        self._stuck_sweeper_task: asyncio.Task | None = None

    async def _resolve_quote_token_programs(self) -> None:
        """Warm the quote-mint token-program and decimals caches at startup.

        `extreme_fast_mode` submits a buy with zero RPC calls between detection
        and submission, so a quote mint's token program -- and its decimals,
        which size `max_sol_cost`/`min_sol_output` -- must already be known. The
        set of quote mints is fixed at startup, so both facts are cached here
        from the one mint-account fetch `resolve_quote_token_program` makes; the
        hot path only reads those caches.

        Raises:
            ValueError: If a configured quote mint's owner is neither SPL Token
                nor Token-2022, or its decimals cannot be read. Left uncaught
                deliberately: trading a quote mint the bot cannot size is worse
                than refusing to start.
        """
        for quote_mint in self.quote_amounts:
            token_program = await resolve_quote_token_program(
                quote_mint, self.solana_client.get_account_info
            )
            logger.info(
                f"Resolved quote mint {quote_mint} -> token program "
                f"{token_program}, decimals {quote_decimals(quote_mint)}"
            )

    async def _stuck_token_sweeper(self) -> None:
        """Periodically scan the wallet for orphaned tokens and sell them.

        Improvement #3: finds tokens the bot owns but is not tracking —
        leftovers from restarts, late-confirmed buys, or pre-fix abandonments
        (e.g. the DICE dust). They are not in `_unsold_positions` and have no
        active exit path, so they would sit forever otherwise. Built as a
        bounded-scan loop: list token accounts, skip anything the bot already
        handles, sell the rest through the normal seller at the current price.
        """
        from core.pubkeys import WSOL_MINT

        while True:
            await asyncio.sleep(self.stuck_sweep_interval)
            try:
                accounts = await self.solana_client.get_token_accounts_by_owner_all_programs(
                    self.wallet.pubkey
                )
            except Exception:
                logger.exception("Stuck-token sweep: wallet scan failed")
                continue
            for acct in accounts:
                try:
                    mint = acct.get("mint")
                    balance = acct.get("token_amount", {}).get("amount", "0")
                    if not mint or int(balance or 0) <= 0:
                        continue
                    key = str(mint)
                    if (
                        key in self.processed_tokens
                        or key in self._unsold_positions
                        or key in {str(m) for m in self.traded_mints}
                    ):
                        continue
                    # Orphaned: not tracked anywhere. Build a minimal TokenInfo
                    # and hand it to the persistent watcher (which re-prices
                    # and sells). The watcher's TTL bounds repeated attempts.
                    token_info = TokenInfo(
                        name=key[:8],
                        symbol=key[:6],
                        uri="",
                        mint=mint,
                        platform=self.platform,
                        quote_mint=WSOL_MINT,
                    )
                    position = Position.create_from_buy_result(
                        mint=mint,
                        symbol=token_info.symbol,
                        entry_price=0.0,
                        quantity=float(balance) / 1e6,
                    )
                    logger.warning(
                        f"Stuck-token sweep found orphan {token_info.symbol} "
                        f"({key[:16]}...); handing to persistent exit watcher"
                    )
                    await self._register_unsold_position(token_info, position)
                except Exception:
                    logger.exception("Stuck-token sweep: item failed")

    async def start(self) -> None:
        """Start the trading bot and listen for new tokens."""
        logger.info(f"Starting Universal Trader for {self.platform.value}")
        logger.info(
            f"Match filter: {self.match_string if self.match_string else 'None'}"
        )
        logger.info(
            f"Creator filter: {self.bro_address if self.bro_address else 'None'}"
        )
        logger.info(f"Marry mode: {self.marry_mode}")
        logger.info(f"YOLO mode: {self.yolo_mode}")
        logger.info(f"Exit strategy: {self.exit_strategy}")

        if self.exit_strategy == "tp_sl":
            logger.info(
                f"Take profit: {self.take_profit_percentage * 100 if self.take_profit_percentage else 'None'}%"
            )
            logger.info(
                f"Stop loss: {self.stop_loss_percentage * 100 if self.stop_loss_percentage else 'None'}%"
            )
            logger.info(
                f"Max hold time: {self.max_hold_time if self.max_hold_time else 'None'} seconds"
            )
            logger.info(f"Max exit sell attempts: {self.max_exit_sell_attempts}")

        logger.info(f"Max token age: {self.max_token_age} seconds")

        try:
            health_resp = await self.solana_client.get_health()
            logger.info(f"RPC warm-up successful (getHealth passed: {health_resp})")
        except Exception as e:
            logger.warning(f"RPC warm-up failed: {e!s}")

        # Outside the try/except above: an RPC health check is best-effort, but a
        # quote mint that cannot be resolved to a token program derives a
        # non-existent ATA and reverts every trade -- fail startup instead.
        await self._resolve_quote_token_programs()

        try:
            # Improvement #3: start the orphaned-token sweeper if enabled.
            if self.stuck_sweep_interval > 0:
                self._stuck_sweeper_task = asyncio.create_task(
                    self._stuck_token_sweeper()
                )
                logger.info(
                    f"Stuck-token sweeper started (interval {self.stuck_sweep_interval}s)"
                )

            # Choose operating mode based on yolo_mode
            if not self.yolo_mode:
                # Single token mode: process one token and exit
                logger.info(
                    "Running in single token mode - will process one token and exit"
                )
                token_info = await self._wait_for_token()
                if token_info:
                    await self._handle_token(token_info)
                    logger.info("Finished processing single token. Exiting...")
                else:
                    logger.info(
                        f"No suitable token found within timeout period ({self.token_wait_timeout}s). Exiting..."
                    )
            else:
                # Continuous mode: process tokens until interrupted
                logger.info(
                    "Running in continuous mode - will process tokens until interrupted"
                )
                processor_task = asyncio.create_task(self._process_token_queue())

                try:
                    await self.token_listener.listen_for_tokens(
                        lambda token: self._queue_token(token),
                        self.match_string,
                        self.bro_address,
                    )
                except Exception:
                    logger.exception("Token listening stopped due to error")
                finally:
                    processor_task.cancel()
                    try:
                        await processor_task
                    except asyncio.CancelledError:
                        pass

        except Exception:
            logger.exception("Trading stopped due to error")

        finally:
            if self._stuck_sweeper_task is not None:
                self._stuck_sweeper_task.cancel()
            await self._cleanup_without_interruption()
            logger.info("Universal Trader has shut down")

    async def _wait_for_token(self) -> TokenInfo | None:
        """Wait for a single token to be detected."""
        # Create a one-time event to signal when a token is found
        token_found = asyncio.Event()
        found_token = None

        async def token_callback(token: TokenInfo) -> None:
            nonlocal found_token
            token_key = str(token.mint)

            if token_key in self.processed_tokens:
                return
            self.processed_tokens.add(token_key)
            skip_reason = self._skip_reason(token)
            if skip_reason:
                logger.info(f"Skipping {token.symbol} - {skip_reason}; still waiting")
                return
            # Record when the token was discovered
            self.token_timestamps[token_key] = monotonic()
            found_token = token
            token_found.set()

        listener_task = asyncio.create_task(
            self.token_listener.listen_for_tokens(
                token_callback,
                self.match_string,
                self.bro_address,
            )
        )

        # Wait for a token with a timeout
        try:
            logger.info(
                f"Waiting for a suitable token (timeout: {self.token_wait_timeout}s)..."
            )
            await asyncio.wait_for(token_found.wait(), timeout=self.token_wait_timeout)
            logger.info(f"Found token: {found_token.symbol} ({found_token.mint})")
            return found_token
        except TimeoutError:
            logger.info(
                f"Timed out after waiting {self.token_wait_timeout}s for a token"
            )
            return None
        finally:
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass

    async def _cleanup_without_interruption(self) -> None:
        """Run cleanup to completion even though shutdown arrives as a cancellation.

        Shutdown reaches this coroutine as a cancellation, and `_cleanup_resources`
        awaits once per account — a settle wait, then an RPC read. Awaiting it
        directly meant the first of those re-raised `CancelledError` and every
        account after it kept its rent, silently: the run ends without even
        logging that it shut down. Running it as its own task and absorbing the
        cancellation lets it finish.

        Bounded by `_CLEANUP_SHUTDOWN_BUDGET` so a stuck RPC cannot hold the
        process open. Rent left behind when the budget expires is not lost — it
        is reclaimed by `tools/cleanup_accounts.py <MINT>` — so the warning names
        that rather than retrying here.
        """
        task = asyncio.create_task(self._cleanup_resources())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._CLEANUP_SHUTDOWN_BUDGET

        while not task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                task.cancel()
                logger.warning(
                    f"Cleanup did not finish within {self._CLEANUP_SHUTDOWN_BUDGET}s. "
                    f"Accounts still open keep their rent; reclaim it with "
                    f"tools/cleanup_accounts.py <MINT>."
                )
                break
            try:
                # asyncio.wait neither cancels the task on timeout nor re-raises
                # what it failed with, so the loop owns both outcomes.
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError:
                # The cancellation that started this shutdown, arriving while
                # cleanup runs. Absorbing it is the point: this is already the
                # terminal path, and re-raising would strand the rent that the
                # remaining accounts hold.
                continue

        if task.done() and not task.cancelled() and task.exception() is not None:
            logger.error("Cleanup failed", exc_info=task.exception())

    async def _cleanup_resources(self) -> None:
        """Perform cleanup operations before shutting down."""
        if self.traded_mints:
            try:
                logger.info(f"Cleaning up {len(self.traded_mints)} traded token(s)...")
                # Build parallel lists of mints and token_program_ids
                mints_list = list(self.traded_mints)
                token_program_ids = [
                    self.traded_token_programs.get(str(mint)) for mint in mints_list
                ]
                await handle_cleanup_post_session(
                    self.solana_client,
                    self.wallet,
                    mints_list,
                    token_program_ids,
                    self.priority_fee_manager,
                    self.cleanup_mode,
                    self.cleanup_with_priority_fee,
                    self.cleanup_force_close_with_burn,
                )
            except Exception:
                logger.exception("Error during cleanup")

        old_keys = {k for k in self.token_timestamps if k not in self.processed_tokens}
        for key in old_keys:
            self.token_timestamps.pop(key, None)

        await self.solana_client.close()

    async def _queue_token(self, token_info: TokenInfo) -> None:
        """Queue a token for processing if not already processed."""
        token_key = str(token_info.mint)

        if token_key in self.processed_tokens:
            logger.debug(f"Token {token_info.symbol} already processed. Skipping...")
            return

        # Improvement #5: cap the tracked set. Under a launch flood the
        # listener can queue far more than the processor can drain; drop the
        # oldest tracked token so the newest launches are never evicted by an
        # unbounded backlog. The dropped entry was never processed, so it can
        # still be re-detected on a later notification.
        if len(self.token_timestamps) >= self.max_tracked_tokens:
            oldest_key = min(
                self.token_timestamps,
                key=lambda k: self.token_timestamps[k],
            )
            del self.token_timestamps[oldest_key]
            self.token_launch_timestamps.pop(oldest_key, None)
            logger.debug(
                f"Tracked-token cap ({self.max_tracked_tokens}) reached; "
                f"dropping oldest {oldest_key[:12]}..."
            )

        # Record timestamp when token was discovered
        self.token_timestamps[token_key] = monotonic()
        # Record the launch timestamp the listener decoded (CreateEvent), when
        # present. Units are the listener's (pump.fun clock); kept raw so a
        # future clock-aware gate can use it. None means the listener did not
        # decode one, and freshness falls back to discovery time.
        if token_info.creation_timestamp is not None:
            self.token_launch_timestamps[token_key] = token_info.creation_timestamp

        await self.token_queue.put(token_info)
        logger.info(
            f"Queued new token: {token_info.symbol} ({token_info.mint}) on {token_info.platform.value}"
        )

    async def _process_token_queue(self) -> None:
        """Continuously process tokens from the queue, only if they're fresh.

        `task_done()` is balanced against `get()`: cancelling this task is how
        yolo mode shuts down, and the cancellation usually lands while parked on
        `get()`, with no item taken. Calling `task_done()` on that path raises
        ValueError, which `start()` does not catch, so a clean shutdown ends up
        logged as `Trading stopped due to error`.
        """
        while True:
            took_item = False
            try:
                token_info = await self.token_queue.get()
                took_item = True
                token_key = str(token_info.mint)

                # Check if token is still "fresh". The freshness gate uses the
                # local discovery time (self.token_timestamps), not the
                # on-chain launch timestamp, so a backed-up queue cannot evict
                # a coin that is genuinely new on-chain. The launch timestamp
                # is recorded in self.token_launch_timestamps and surfaced in
                # the skip log for diagnostics (its units are listener-owned).
                current_time = monotonic()
                token_age = current_time - self.token_timestamps.get(
                    token_key, current_time
                )

                launch_age = self.token_launch_timestamps.get(token_key)
                age_note = (
                    f", launch_ts={launch_age:.3f}" if launch_age is not None else ""
                )

                if token_age > self.max_token_age:
                    logger.info(
                        f"Skipping token {token_info.symbol} - too old "
                        f"({token_age:.1f}s queue wait{age_note} > {self.max_token_age}s)"
                    )
                    continue

                self.processed_tokens.add(token_key)

                logger.info(
                    f"Processing fresh token: {token_info.symbol} (age: {token_age:.1f}s)"
                )
                await self._handle_token(token_info)

            except asyncio.CancelledError:
                logger.info("Token queue processor was cancelled")
                break
            except Exception:
                logger.exception("Error in token queue processor")
            finally:
                # The stale-token `continue` above took an item, so it still
                # needs this; a cancellation parked on `get()` did not.
                if took_item:
                    self.token_queue.task_done()

    def _skip_reason(self, token_info: TokenInfo) -> str | None:
        """Why the configured filters refuse this coin, or None to trade it.

        Checked on detection as well as before the buy: single-token mode would
        otherwise spend its one token on a coin it was never going to buy.
        """
        quote_mint = normalize_quote_mint(token_info.quote_mint)
        if (
            self.blocked_creators
            and (
                str(token_info.creator) in self.blocked_creators
                or str(token_info.user) in self.blocked_creators
            )
        ):
            return f"creator {token_info.creator or token_info.user} is blocked"
        if self.allowed_quote_mints is not None and quote_mint not in self.allowed_quote_mints:
            return f"quote mint {quote_mint} not in allowed_quote_mints"
        if quote_mint not in self.quote_amounts:
            return f"no buy amount configured for quote mint {quote_mint}"
        if (
            self.max_transfer_fee_bps is not None
            and token_info.transfer_fee_bps > self.max_transfer_fee_bps
        ):
            return (
                f"transfer fee {token_info.transfer_fee_bps} bps exceeds "
                f"max_transfer_fee_bps {self.max_transfer_fee_bps}"
            )
        return None

    async def _curve_gate_reason(self, token_info: TokenInfo) -> str | None:
        """Why the curve refuses this coin, or None to trade it.

        Applies the optional `max_market_cap_sol`, `min_market_cap_sol` and
        `min_curve_progress_sol` filters. Market cap is price * total supply
        in SOL; curve progress is the SOL actually paid into the curve by
        buyers (real_quote_reserves) — a launch nobody touched sits at ~0.
        Non-SOL-paired coins are left alone because the gates are denominated
        in SOL.
        """
        if (
            self.max_market_cap_sol is None
            and self.min_curve_progress_sol is None
            and self.min_market_cap_sol is None
        ):
            return None
        try:
            implementations = get_platform_implementations(
                token_info.platform, self.solana_client
            )
            curve_manager = implementations.curve_manager
            pool_address = (
                token_info.bonding_curve
                or token_info.pool_state
                or token_info.associated_bonding_curve
            )
            if pool_address is None:
                return None
            state = await curve_manager.get_pool_state(pool_address)
            if not state.get("is_sol_paired", False):
                return None
            if state.get("graduated"):
                return "curve already graduated (on PumpSwap)"
            price = state.get("price_per_token")
            total_supply = state.get("token_total_supply", 0)
            if price is None or total_supply <= 0:
                return None
            mc_sol = price * (total_supply / 1e6)
            # Improvement #4: breakout mode — buy only after the market cap
            # has risen THROUGH min_market_cap_sol (momentum confirmation),
            # complementing the max cap that rejects already-pumped coins.
            if (
                self.min_market_cap_sol is not None
                and mc_sol < self.min_market_cap_sol
            ):
                return (
                    f"market cap {mc_sol:.2f} SOL below "
                    f"min_market_cap_sol {self.min_market_cap_sol} (breakout not triggered)"
                )
            if (
                self.max_market_cap_sol is not None
                and mc_sol > self.max_market_cap_sol
            ):
                return (
                    f"market cap {mc_sol:.2f} SOL exceeds "
                    f"max_market_cap_sol {self.max_market_cap_sol}"
                )
            real_quote = state.get("real_quote_reserves", 0)
            progress_sol = real_quote / 1e9
            if (
                self.min_curve_progress_sol is not None
                and progress_sol < self.min_curve_progress_sol
            ):
                return (
                    f"curve progress {progress_sol:.6f} SOL below "
                    f"min_curve_progress_sol {self.min_curve_progress_sol}"
                )
        except Exception:
            logger.exception("Curve gate check failed")
            return None
        return None

    async def _handle_token(self, token_info: TokenInfo) -> None:
        """Handle a new token creation event."""
        try:
            # Validate that token is for our platform
            if token_info.platform != self.platform:
                logger.warning(
                    f"Token platform mismatch: expected {self.platform.value}, got {token_info.platform.value}"
                )
                return

            skip_reason = self._skip_reason(token_info)
            if skip_reason:
                logger.info(f"Skipping {token_info.symbol} - {skip_reason}")
                return
            token_quote_mint = normalize_quote_mint(token_info.quote_mint)

            # Wait for pool/curve to stabilize (unless in extreme fast mode)
            if not self.extreme_fast_mode:
                await self._save_token_info(token_info)
                logger.info(
                    f"Waiting for {self.wait_time_after_creation} seconds for the pool/curve to stabilize..."
                )
                await asyncio.sleep(self.wait_time_after_creation)

            # Curve quality gates (min progress / max MC) — only on the non-extreme
            # path. Block-0 extreme-fast buys have no demand data yet (the
            # curve is seconds old), so min_curve_progress_sol would reject
            # every coin; the anti-bundler shield there is the calibrated
            # extreme_fast_token_amount + slippage cap. The demand-checked
            # path waits, then reads the curve and applies the gates, so a
            # dead or already-pumped coin never costs a tx fee.
            if not self.extreme_fast_mode:
                curve_gate_reason = await self._curve_gate_reason(token_info)
                if curve_gate_reason:
                    logger.info(f"Skipping {token_info.symbol} - {curve_gate_reason}")
                    return

            # Buy token
            logger.info(
                f"Buying {self.quote_amounts[token_quote_mint]:.6f} of quote "
                f"{token_quote_mint} worth of {token_info.symbol} "
                f"on {token_info.platform.value}..."
            )
            if self.shadow_mode:
                # Shadow/paper mode (#8): simulate the fill at the current
                # curve price (like the normal path would price it) and run
                # the exit machinery against the simulated position. No
                # transaction is sent and nothing is logged as spent.
                sim_price = await self._read_price(token_info)
                sim_amount = (
                    self.quote_amounts[token_quote_mint] / sim_price
                    if sim_price > 0
                    else 0.0
                )
                logger.info(
                    f"[SHADOW] would buy {sim_amount:.4f} {token_info.symbol} "
                    f"at {sim_price:.3e} SOL/token (no tx sent)"
                )
                buy_result = TradeResult(
                    success=True,
                    platform=token_info.platform,
                    tx_signature=None,
                    amount=sim_amount,
                    price=sim_price,
                )
            else:
                buy_result: TradeResult = await self.buyer.execute(token_info)

            if buy_result.success:
                await self._handle_successful_buy(token_info, buy_result)
            else:
                await self._handle_failed_buy(token_info, buy_result)

            # Only wait for next token in yolo mode
            if self.yolo_mode:
                logger.info(
                    f"YOLO mode enabled. Waiting {self.wait_time_before_new_token} seconds before looking for next token..."
                )
                await asyncio.sleep(self.wait_time_before_new_token)

        except Exception:
            logger.exception(f"Error handling token {token_info.symbol}")

    async def _handle_successful_buy(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Handle successful token purchase."""
        logger.info(
            f"Successfully bought {token_info.symbol} on {token_info.platform.value}"
        )
        self._log_trade(
            "buy",
            token_info,
            buy_result.price,
            buy_result.amount,
            buy_result.tx_signature,
            # SOL settlement (SOL-paired coins spend SOL; non-SOL quotes are
            # logged as None until a quote->SOL conversion exists).
            quote_spent_sol=(
                buy_result.price * buy_result.amount
                if buy_result.price and buy_result.amount and (
                    normalize_quote_mint(token_info.quote_mint) == WSOL_MINT
                )
                else None
            ),
        )
        self.traded_mints.add(token_info.mint)
        # Track token program for cleanup
        mint_str = str(token_info.mint)
        if token_info.token_program_id:
            self.traded_token_programs[mint_str] = token_info.token_program_id

        # Choose exit strategy
        if not self.marry_mode:
            if self.exit_strategy == "tp_sl":
                await self._handle_tp_sl_exit(token_info, buy_result)
            elif self.exit_strategy == "time_based":
                await self._handle_time_based_exit(token_info, buy_result)
            elif self.exit_strategy == "manual":
                logger.info("Manual exit strategy - position will remain open")
        else:
            logger.info("Marry mode enabled. Skipping sell operation.")

    async def _handle_failed_buy(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Handle failed token purchase."""
        logger.error(f"Failed to buy {token_info.symbol}: {buy_result.error_message}")
        self._log_attempt(
            "buy",
            token_info,
            1,
            buy_result.tx_signature,
            buy_result.error_message,
        )
        # Close ATA if enabled
        await handle_cleanup_after_failure(
            self.solana_client,
            self.wallet,
            token_info.mint,
            token_info.token_program_id,
            self.priority_fee_manager,
            self.cleanup_mode,
            self.cleanup_with_priority_fee,
            self.cleanup_force_close_with_burn,
        )

    async def _handle_tp_sl_exit(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Handle take profit/stop loss exit strategy."""
        position = Position.create_from_buy_result(
            mint=token_info.mint,
            symbol=token_info.symbol,
            entry_price=buy_result.price,
            quantity=buy_result.amount,
            take_profit_percentage=self.take_profit_percentage,
            stop_loss_percentage=self.stop_loss_percentage,
            max_hold_time=self.max_hold_time,
        )

        logger.info(f"Created position: {position}")
        quote_label = quote_symbol(normalize_quote_mint(token_info.quote_mint))
        if position.take_profit_price:
            logger.info(
                f"Take profit target: {position.take_profit_price:.8f} {quote_label}"
            )
        if position.stop_loss_price:
            logger.info(
                f"Stop loss target: {position.stop_loss_price:.8f} {quote_label}"
            )

        # Monitor position until exit condition is met
        await self._monitor_position_until_exit(token_info, position)

    async def _handle_time_based_exit(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Handle legacy time-based exit strategy.

        Args:
            buy_result: Result from the buy operation (contains token amount)
        """
        logger.info(f"Waiting for {self.wait_time_after_buy} seconds before selling...")
        await asyncio.sleep(self.wait_time_after_buy)

        logger.info(f"Selling {token_info.symbol}...")
        # The first attempt prices off the buy: no price has been read since,
        # and reading one would cost an RPC call on the happy path.
        token_price = buy_result.price

        for attempt in range(1, self.max_exit_sell_attempts + 1):
            if self.shadow_mode:
                # Shadow mode: simulate the sell at the current read price.
                price = await self._read_price(token_info)
                sell_result = TradeResult(
                    success=price > 0,
                    platform=token_info.platform,
                    tx_signature=None,
                    amount=buy_result.amount,
                    price=price if price > 0 else 0.0,
                )
                logger.info(
                    f"[SHADOW] would sell {token_info.symbol} at "
                    f"{price:.3e} SOL/token (no tx sent)"
                )
            else:
                sell_result = await self.seller.execute(
                    token_info,
                    token_amount=buy_result.amount or 0.0,
                    token_price=token_price,
                )  # type: ignore[arg-type]  # token_price is built from buy_result.price

            if sell_result.success:
                logger.info(f"Successfully sold {token_info.symbol}")
                self._log_trade(
                    "sell",
                    token_info,
                    sell_result.price,
                    sell_result.amount,
                    sell_result.tx_signature,
                    quote_received_sol=(
                        sell_result.price * sell_result.amount
                        if sell_result.price and sell_result.amount and (
                            normalize_quote_mint(token_info.quote_mint) == WSOL_MINT
                        )
                        else None
                    ),
                )
                # Close ATA if enabled
                await self._cleanup_after_exit(token_info)
                return

            logger.error(
                f"Failed to sell {token_info.symbol} (attempt "
                f"{attempt}/{self.max_exit_sell_attempts}): "
                f"{sell_result.error_message}"
            )
            self._log_attempt(
                "sell",
                token_info,
                attempt,
                sell_result.tx_signature,
                sell_result.error_message,
            )

            verdict = await self._classify_failed_exit_sell(token_info, sell_result)
            if verdict is ExitSellVerdict.LATE_SUCCESS:
                # The sell landed; only its confirmation was late. Run the same
                # cleanup a first-try success would, minus the trade log: the
                # amounts were never read back.
                logger.info(f"Sell of {token_info.symbol} confirmed late")
                await self._cleanup_after_exit(token_info)
                return
            if verdict is ExitSellVerdict.STOP:
                logger.error(
                    f"Stopping exit attempts for {token_info.symbol} after "
                    f"{attempt} attempt(s) - the last sell could not be "
                    f"resolved. Handing the position to the persistent "
                    f"exit watcher."
                )
                await self._register_unsold_position(
                    token_info,
                    Position.create_from_buy_result(
                        mint=token_info.mint,
                        symbol=token_info.symbol,
                        entry_price=buy_result.price or 0.0,
                        quantity=buy_result.amount or 0.0,
                    ),
                )
                return

            if attempt >= self.max_exit_sell_attempts:
                break

            # The seller turns token_price into the slippage floor, and a revert
            # usually means that floor no longer matches the market, so re-read
            # the price rather than repeating the same unpayable ask.
            token_price = await self._current_price_or(token_info, token_price)

        logger.error(
            f"Giving up on selling {token_info.symbol} after "
            f"{self.max_exit_sell_attempts} attempts. Handing the position to "
            f"the persistent exit watcher - tokens are still held."
        )
        await self._register_unsold_position(
            token_info,
            Position.create_from_buy_result(
                mint=token_info.mint,
                symbol=token_info.symbol,
                entry_price=buy_result.price or 0.0,
                quantity=buy_result.amount or 0.0,
            ),
        )

    async def _classify_failed_exit_sell(
        self, token_info: TokenInfo, sell_result: TradeResult
    ) -> ExitSellVerdict:
        """Decide whether another exit sell is safe after one came back failed.

        A reverted sell changed nothing; one whose confirmation never arrived may
        already have closed the position, and resending spends a fee to sell a
        balance that is no longer there.

        A merely unconfirmed signature is re-checked first: `getTransaction`
        keeps answering long after signature statuses age out of the RPC's recent
        history, so a sell that turns out to have landed is reported as a late
        success rather than retried.

        Args:
            token_info: Token information, for logging
            sell_result: The unsuccessful result from the seller

        Returns:
            LATE_SUCCESS, RETRY, or STOP
        """
        verdict = _exit_sell_verdict_for(sell_result.failure_reason)
        if verdict is not None:
            return verdict

        signature = sell_result.tx_signature
        if not signature:
            # No signature to re-check and no reason recorded: treat as unknown.
            logger.error(
                f"Exit sell for {token_info.symbol} failed without a signature "
                f"to re-check; not sending another one"
            )
            return ExitSellVerdict.STOP

        logger.warning(
            f"Exit sell {signature[:16]}... for {token_info.symbol} was not "
            f"confirmed; re-checking before deciding whether to sell again"
        )
        try:
            status = await self.solana_client.verify_transaction_status(signature)
        except Exception:
            # The re-check is all that stands between an unresolved sell and a
            # second one, so it must not throw past the decision: in the monitor
            # loop an escape lands in the outer handler, which can call straight
            # back into another exit attempt.
            logger.exception(
                f"Could not verify exit sell {signature[:16]}...; "
                f"leaving it unresolved rather than selling again"
            )
            return ExitSellVerdict.STOP

        if status is ConfirmationStatus.SUCCESS:
            logger.info(
                f"Exit sell {signature[:16]}... did land after all — "
                f"position is closed, not retrying"
            )
            return ExitSellVerdict.LATE_SUCCESS
        if status is ConfirmationStatus.REVERTED:
            logger.warning(
                f"Exit sell {signature[:16]}... reverted on chain; "
                f"tokens are still held, retrying"
            )
            return ExitSellVerdict.RETRY

        logger.error(
            f"Exit sell {signature[:16]}... is still unconfirmed. Not sending "
            f"another sell: it may already have closed the position. Verify "
            f"this signature before acting on {token_info.symbol}."
        )
        return ExitSellVerdict.STOP

    async def _read_price(self, token_info: TokenInfo) -> float:
        """The coin's price where it trades now: its curve, or after graduation its AMM pool.

        Returns:
            Whole quote units per whole token; 0.0 for a pool with nothing in it

        Raises:
            ValueError: If a graduated coin's pool cannot be priced yet — it
                lands a moment after graduation
        """
        implementations = self.platform_implementations
        try:
            return await implementations.curve_manager.calculate_price(
                self._get_pool_address(token_info)
            )
        except CurveGraduatedError:
            market = implementations.graduated_market
            if market is None:
                raise ValueError(  # noqa: TRY003
                    f"{token_info.symbol} has graduated and "
                    f"{self.platform.value} has no post-graduation market"
                ) from None
            if token_info.mint not in self._graduated_mints:
                self._graduated_mints.add(token_info.mint)
                logger.info(f"{token_info.symbol} has graduated; pricing off its pool")
            return (await market.get_market_state(token_info))["price_per_token"]

    async def _current_price_or(self, token_info: TokenInfo, fallback: float) -> float:
        """Read the current price, falling back to the last known one.

        Args:
            token_info: Token information used to locate the pool/curve
            fallback: Price to keep if the read fails

        Returns:
            The freshly read price, or `fallback` if it could not be read
        """
        try:
            price = await self._read_price(token_info)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Could not re-read price for {token_info.symbol} ({e}); "
                f"retrying against {fallback:.8f}"
            )
            return fallback
        if price and price > 0:
            return price
        logger.warning(
            f"Re-read price for {token_info.symbol} was {price}; "
            f"retrying against {fallback:.8f}"
        )
        return fallback

    async def _monitor_position_until_exit(
        self, token_info: TokenInfo, position: Position
    ) -> None:
        """Monitor a position until exit conditions are met."""
        logger.info(
            f"Starting position monitoring (check interval: {self.price_check_interval}s)"
        )

        exit_sell_attempts = 0
        # Every price below is denominated in the coin's quote asset, which is
        # not always SOL.
        quote_label = quote_symbol(normalize_quote_mint(token_info.quote_mint))

        # The last price actually read, always positive. A max_hold_time exit
        # firing with the price feed down still needs a slippage floor, and
        # before the first successful read the entry price is all there is.
        last_known_price = position.entry_price

        while position.is_active:
            try:
                current_price = await self._read_price(token_info)
                # A curve with no virtual token reserves prices at 0.0 rather
                # than raising, and the seller rejects a non-positive price
                # before its own try block, so a stored 0.0 would escape the
                # bounded exit handling next time the feed went down.
                if current_price > 0:
                    last_known_price = current_price

                # Check if position should be exited
                should_exit, exit_reason = position.should_exit(current_price)

                if should_exit and exit_reason:
                    logger.info(f"Exit condition met: {exit_reason.value}")
                    logger.info(f"Current price: {current_price:.8f} {quote_label}")

                    # 0.0 satisfies the stop-loss comparison, so the exit can
                    # fire on a price the sell cannot be floored against.
                    exit_price = (
                        current_price if current_price > 0 else last_known_price
                    )
                    if exit_price != current_price:
                        logger.warning(
                            f"Curve priced {token_info.symbol} at 0; flooring the "
                            f"exit sell at the last real price "
                            f"{exit_price:.8f} {quote_label}"
                        )

                    exit_sell_attempts += 1
                    if await self._run_exit_attempt(
                        token_info,
                        position,
                        exit_reason,
                        exit_price,
                        exit_sell_attempts,
                    ):
                        break
                    # Keep monitoring: the next iteration re-reads the price and
                    # retries the sell with a floor that matches the market.
                else:
                    # Log current status
                    exit_sell_attempts = 0
                    pnl = position.get_pnl(current_price)
                    logger.debug(
                        f"Position status: {current_price:.8f} {quote_label} "
                        f"({pnl['price_change_pct']:+.2f}%)"
                    )

                # Wait before next price check
                await asyncio.sleep(self.price_check_interval)

            except Exception:
                logger.exception("Error monitoring position")

                # max_hold_time is the one exit condition that needs no price,
                # and should_exit cannot be asked without one. Ask the time-only
                # question here so the deadline is enforced with the feed down.
                if position.should_exit_on_time():
                    logger.warning(
                        f"Max hold time reached for {token_info.symbol} while "
                        f"the price is unreadable - exiting against the last "
                        f"known price {last_known_price:.8f} {quote_label}"
                    )
                    exit_sell_attempts += 1
                    if await self._run_exit_attempt(
                        token_info,
                        position,
                        ExitReason.MAX_HOLD_TIME,
                        last_known_price,
                        exit_sell_attempts,
                    ):
                        break

                await asyncio.sleep(
                    self.price_check_interval
                )  # Continue monitoring despite errors

    async def _register_unsold_position(
        self, token_info: TokenInfo, position: Position
    ) -> None:
        """Hand a position to the persistent exit watcher.

        Called when the bounded exit retries are spent. The watcher keeps
        re-reading the price and selling until the position clears or
        `persistent_exit_ttl` expires, so a temporarily unsellable token is
        never silently abandoned.
        """
        key = str(token_info.mint)
        if position.quantity <= 0:
            logger.warning(
                f"{token_info.symbol}: no quantity left to sell; skipping "
                f"persistent exit watcher"
            )
            return
        self._unsold_positions[key] = (token_info, position)
        logger.warning(
            f"{token_info.symbol}: bounded exit retries spent; "
            f"registering with persistent exit watcher "
            f"(TTL {self.persistent_exit_ttl}s, interval {self.persistent_exit_interval}s)"
        )
        if self._unsold_watcher_task is None or self._unsold_watcher_task.done():
            self._unsold_watcher_task = asyncio.create_task(
                self._unsold_position_watcher()
            )

    async def _unsold_position_watcher(self) -> None:
        """Continuously attempt to clear unsold positions until TTL expiry."""
        from time import monotonic as _monotonic

        # Track when each position entered the watcher for the TTL bound.
        entered: dict[str, float] = {
            key: _monotonic() for key in self._unsold_positions
        }
        while self._unsold_positions:
            # Back off before (re)trying: the hot-path exit retries were just
            # spent seconds ago, so an immediate redundant sell would both waste
            # a fee and break the bounded-retry contract tests rely on.
            await asyncio.sleep(self.persistent_exit_interval)
            now = _monotonic()
            for key in list(self._unsold_positions):
                token_info, position = self._unsold_positions[key]
                if now - entered[key] > self.persistent_exit_ttl:
                    logger.error(
                        f"Persistent exit TTL expired for {token_info.symbol}; "
                        f"tokens still held ({position.quantity}). "
                        f"Reclaim with tools/cleanup_accounts.py {token_info.mint}"
                    )
                    del self._unsold_positions[key]
                    continue
                try:
                    price = await self._read_price(token_info)
                except Exception:
                    logger.exception(
                        f"Persistent exit price read failed for {token_info.symbol}"
                    )
                    continue
                if price is None or price <= 0:
                    logger.debug(
                        f"Persistent exit: no usable price for {token_info.symbol}; retrying"
                    )
                    continue
                # Reuse the same exit-sell path as the monitor loop, with a
                # fresh price floor; MANUAL keeps the exit reason neutral.
                verdict = await self._attempt_position_exit(
                    token_info,
                    position,
                    ExitReason.MANUAL,
                    price,
                )
                if verdict is ExitSellVerdict.RETRY:
                    logger.debug(
                        f"Persistent exit retry needed for {token_info.symbol}"
                    )
                else:
                    del self._unsold_positions[key]
                    if key in entered:
                        del entered[key]
            if self._unsold_positions:
                await asyncio.sleep(self.persistent_exit_interval)

    def _log_attempt(
        self,
        action: str,
        token_info: TokenInfo,
        attempt: int,
        tx_hash: str | None,
        failure_reason: str | None = None,
    ) -> None:
        """Append per-attempt telemetry to the ledger (trades/attempts.log).

        Every individual buy/sell submission gets a row with its signature and
        the reason it was counted as failed, so post-mortems (e.g. a buy that
        failed to confirm) can be traced attempt-by-attempt.
        """
        try:
            trades_dir = Path("trades")
            trades_dir.mkdir(exist_ok=True)
            entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "action": action,
                "symbol": token_info.symbol,
                "token_address": str(token_info.mint),
                "attempt": attempt,
                "tx_hash": str(tx_hash) if tx_hash else None,
                "failure_reason": failure_reason,
            }
            path = trades_dir / "attempts.log"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            logger.exception("Failed to log attempt telemetry")

    async def _run_exit_attempt(
        self,
        token_info: TokenInfo,
        position: Position,
        exit_reason: ExitReason,
        price: float,
        attempt: int,
    ) -> bool:
        """Make one bounded attempt to close the position.

        Args:
            position: The open position
            exit_reason: Why the exit fired
            price: Price the sell is floored against
            attempt: 1-based attempt counter, for the bound and the logs

        Returns:
            True when monitoring should stop, whether because the position is
            closed, because the outcome could not be resolved, or because the
            attempt budget is spent.
        """
        verdict = await self._attempt_position_exit(
            token_info, position, exit_reason, price
        )
        if verdict is not ExitSellVerdict.RETRY:
            return True

        if attempt >= self.max_exit_sell_attempts:
            logger.error(
                f"Giving up on exiting {token_info.symbol} after "
                f"{attempt} attempts. Handing the position to the persistent "
                f"exit watcher - tokens are still held."
            )
            await self._register_unsold_position(token_info, position)
            return True
        return False

    async def _attempt_position_exit(
        self,
        token_info: TokenInfo,
        position: Position,
        exit_reason: ExitReason,
        price: float,
    ) -> ExitSellVerdict:
        """Sell the position once and report what came of it.

        Args:
            position: The open position, closed in place on success
            exit_reason: Why the exit fired
            price: Price the sell is floored against. The seller turns it into
                `min_quote_output`, so it must be a price the pool can pay — an
                exit fires precisely because the price left the entry price.

        Returns:
            SOLD, LATE_SUCCESS, RETRY, or STOP
        """
        # Log PnL before exit
        pnl = position.get_pnl(price)
        logger.info(
            f"Position PnL: {pnl['price_change_pct']:.2f}% "
            f"({pnl['unrealized_pnl_quote']:.6f} "
            f"{quote_symbol(normalize_quote_mint(token_info.quote_mint))})"
        )

        if self.shadow_mode:
            # Shadow/paper mode (#8): simulate the exit at the current price.
            sim_price = await self._read_price(token_info)
            sell_result = TradeResult(
                success=sim_price > 0,
                platform=token_info.platform,
                tx_signature=None,
                amount=position.quantity,
                price=sim_price if sim_price > 0 else 0.0,
            )
            logger.info(
                f"[SHADOW] would exit {token_info.symbol} at "
                f"{sim_price:.3e} SOL/token ({exit_reason.value}, no tx sent)"
            )
        else:
            sell_result = await self.seller.execute(
                token_info,
                token_amount=position.quantity,
                token_price=price,
            )

        if sell_result.success:
            # Close position with actual exit price
            position.close_position(sell_result.price, exit_reason)

            logger.info(f"Successfully exited position: {exit_reason.value}")
            self._log_trade(
                "sell",
                token_info,
                sell_result.price,
                sell_result.amount,
                sell_result.tx_signature,
                quote_received_sol=(
                    sell_result.price * sell_result.amount
                    if sell_result.price and sell_result.amount and (
                        normalize_quote_mint(token_info.quote_mint) == WSOL_MINT
                    )
                    else None
                ),
            )

            final_pnl = position.get_pnl()
            logger.info(
                f"Final PnL: {final_pnl['price_change_pct']:.2f}% "
                f"({final_pnl['unrealized_pnl_quote']:.6f} "
                f"{quote_symbol(normalize_quote_mint(token_info.quote_mint))})"
            )
            await self._cleanup_after_exit(token_info)
            return ExitSellVerdict.SOLD

        logger.error(f"Failed to exit position: {sell_result.error_message}")
        self._log_attempt(
            "exit_sell",
            token_info,
            1,
            sell_result.tx_signature,
            sell_result.error_message,
        )

        verdict = await self._classify_failed_exit_sell(token_info, sell_result)
        if verdict is ExitSellVerdict.LATE_SUCCESS:
            # The sell landed, only its confirmation was late. Close against the
            # price it was floored at; the real fill was never read back.
            position.close_position(price, exit_reason)
            logger.info(f"Position for {token_info.symbol} closed by a late fill")
            await self._cleanup_after_exit(token_info)
        elif verdict is ExitSellVerdict.STOP:
            logger.error(
                f"Stopping exit attempts for {token_info.symbol} - the last "
                f"sell could not be resolved. Position stays open and is no "
                f"longer monitored."
            )
        return verdict

    async def _cleanup_after_exit(self, token_info: TokenInfo) -> None:
        """Run the configured post-sell cleanup for a closed position.

        Args:
            token_info: Token whose account may now be closable
        """
        await handle_cleanup_after_sell(
            self.solana_client,
            self.wallet,
            token_info.mint,
            token_info.token_program_id,
            self.priority_fee_manager,
            self.cleanup_mode,
            self.cleanup_with_priority_fee,
            self.cleanup_force_close_with_burn,
        )

    def _get_pool_address(self, token_info: TokenInfo) -> Pubkey:
        """Get the pool/curve address for price monitoring using platform-agnostic method."""
        address_provider = self.platform_implementations.address_provider

        if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
            return token_info.bonding_curve
        elif hasattr(token_info, "pool_state") and token_info.pool_state:
            return token_info.pool_state
        else:
            return address_provider.derive_pool_address(token_info.mint)

    async def _save_token_info(self, token_info: TokenInfo) -> None:
        """Save token information to a file."""
        try:
            trades_dir = Path("trades")
            trades_dir.mkdir(exist_ok=True)
            file_path = trades_dir / f"{token_info.mint}.txt"

            # Convert to dictionary for saving - platform-agnostic
            token_dict = {
                "name": token_info.name,
                "symbol": token_info.symbol,
                "uri": token_info.uri,
                "mint": str(token_info.mint),
                "platform": token_info.platform.value,
                "user": str(token_info.user) if token_info.user else None,
                "creator": str(token_info.creator) if token_info.creator else None,
                "creation_timestamp": token_info.creation_timestamp,
            }

            # Add platform-specific fields only if they exist
            platform_fields = {
                "bonding_curve": token_info.bonding_curve,
                "associated_bonding_curve": token_info.associated_bonding_curve,
                "creator_vault": token_info.creator_vault,
                "pool_state": token_info.pool_state,
                "base_vault": token_info.base_vault,
                "quote_vault": token_info.quote_vault,
            }

            for field_name, field_value in platform_fields.items():
                if field_value is not None:
                    token_dict[field_name] = str(field_value)

            file_path.write_text(json.dumps(token_dict, indent=2))

            logger.info(f"Token information saved to {file_path}")
        except OSError:
            logger.exception("Failed to save token information")

    def _log_trade(
        self,
        action: str,
        token_info: TokenInfo,
        price: float,
        amount: float,
        tx_hash: str | None,
        quote_spent_sol: float | None = None,
        quote_received_sol: float | None = None,
    ) -> None:
        """Log trade information to the execution ledger (trades/trades.log).

        Args:
            action: "buy" or "sell"
            token_info: The traded token
            price: Price in the coin's quote asset per whole token
            amount: Token quantity
            tx_hash: Transaction signature
            quote_spent_sol: SOL value paid out (buys, settlement-denominated)
            quote_received_sol: SOL value received (sells)
        """
        try:
            trades_dir = Path("trades")
            trades_dir.mkdir(exist_ok=True)

            log_entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "action": action,
                "platform": token_info.platform.value,
                "token_address": str(token_info.mint),
                "symbol": token_info.symbol,
                "price": price,
                "amount": amount,
                "tx_hash": str(tx_hash) if tx_hash else None,
                # Phase 6 ledger: net-of-fee settlement in SOL when known.
                "quote_spent_sol": quote_spent_sol,
                "quote_received_sol": quote_received_sol,
            }

            log_file_path = trades_dir / "trades.log"
            with log_file_path.open("a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(log_entry) + "\n")
        except OSError:
            logger.exception("Failed to log trade information")


# Backward compatibility alias
PumpTrader = UniversalTrader  # Legacy name for backward compatibility
