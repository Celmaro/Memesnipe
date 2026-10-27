"""Universal Geyser gRPC listener, platform-agnostic via the interfaces."""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

import grpc

from geyser.generated import geyser_pb2, geyser_pb2_grpc
from interfaces.core import Platform, TokenInfo
from monitoring.base_listener import BaseTokenListener
from monitoring.metrics import metrics
from platforms import platform_factory
from utils.logger import get_logger

logger = get_logger(__name__)


def _describe_envelope(update: geyser_pb2.SubscribeUpdate) -> str:
    """Summarise the transaction format a coin was created in, for the log.

    Transaction v1 (SIMD-0385) carries its compute budget inline on the message
    as `config` rather than as ComputeBudget instructions, and geyser sets that
    field only for v1, so its presence is the version test — `versioned` is true
    for v0 and v1 alike. Diagnostic only; detection stays on `meta.log_messages`.

    Args:
        update: The geyser update a TokenInfo was just parsed out of

    Returns:
        A one-line summary of the version, inline budget and reported cost
    """
    transaction = update.transaction.transaction
    message = transaction.transaction.message

    if not message.HasField("config"):
        return "v0" if message.versioned else "legacy"

    config = message.config
    # Every field has presence, and an unset one means zero rather than a
    # runtime default, so report "unset" rather than implying a fallback.
    parts = [
        f"priority_fee={config.priority_fee} lamports"
        if config.HasField("priority_fee")
        else "priority_fee=unset",
        f"cu_limit={config.compute_unit_limit}"
        if config.HasField("compute_unit_limit")
        else "cu_limit=unset",
        f"data_size={config.loaded_accounts_data_size_limit}"
        if config.HasField("loaded_accounts_data_size_limit")
        else "data_size=unset",
    ]
    if config.HasField("heap_size"):
        parts.append(f"heap={config.heap_size}")
    if transaction.meta.HasField("cost_units"):
        parts.append(f"cost_units={transaction.meta.cost_units}")

    return f"v1 ({', '.join(parts)})"


def _log_envelope(update: geyser_pb2.SubscribeUpdate) -> None:
    """Report the creating transaction's format, when debug logging is on.

    The summary is built only if it will be printed: this sits between detection
    and submission, which extreme_fast_mode keeps free of avoidable work.

    Args:
        update: The geyser update a TokenInfo was just parsed out of
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"Creating transaction envelope: {_describe_envelope(update)}")


class UniversalGeyserListener(BaseTokenListener):
    """Universal Geyser listener that works with any platform."""

    def __init__(
        self,
        geyser_endpoint: str,
        geyser_api_token: str,
        geyser_auth_type: str,
        platforms: list[Platform] | None = None,
        in_stream_updates_enabled: bool = False,
    ):
        """Initialize universal Geyser listener."""
        super().__init__()
        self.geyser_endpoint = geyser_endpoint
        self.geyser_api_token = geyser_api_token
        # R3-P2-15: in-stream filter updates (off by default). When on, the
        # request side of the Subscribe RPC becomes a queue-fed stream so
        # update_subscription() can change watched filters with no reconnect.
        self.in_stream_updates_enabled = in_stream_updates_enabled
        self._in_stream_updates = in_stream_updates_enabled

        valid_auth_types = {"x-token", "basic"}
        self.auth_type: str = (geyser_auth_type or "x-token").lower()
        if self.auth_type not in valid_auth_types:
            raise ValueError(
                f"Unsupported auth_type={self.auth_type!r}. "
                f"Expected one of {valid_auth_types}"
            )

        if platforms is None:
            self.platforms = platform_factory.get_supported_platforms()
        else:
            self.platforms = platforms

        # Get event parsers for all platforms
        self.platform_parsers = {}
        self.platform_program_ids = set()

        # Stream health state (T1.1/T1.2): last slot/event time drive the
        # staleness watchdog, dead_slots rejects events from failed leaders,
        # and _last_slot feeds from_slot resume after a reconnect.
        self._last_slot: int | None = None
        self._last_event_time: float | None = None
        self._dead_slots: set[int] = set()
        self._dead_slots_max = 4096
        self._stream_healthy = True
        self._watchdog_task: asyncio.Task | None = None
        self._watchdog_poll_interval = 2.0
        self._watchdog_idle_threshold = 5.0
        self._watchdog_max_stale_slots = 60
        self._reconnect_delay = 1.0
        self._reconnect_delay_max = 30.0
        self._force_reconnect = asyncio.Event()

        # R3-P2-15: continuity revision — bumped on every discontinuity
        # (reconnect/resume, in-stream filter update). Consumers that cache
        # state built from the stream (curve cache, shadow capture) compare
        # the revision and invalidate pre-gap state, so a resume never
        # replays events against a cache primed from a different epoch.
        self._continuity_revision = 0
        self._in_stream_updates = False
        self._update_queue: asyncio.Queue | None = None
        self._stream_request_iter = None

        # T3.2: optional copy-trader callback. When set, every transaction
        # update's logs are forwarded after the token-creation check so a
        # leader-buy TradeEvent can be mirrored without extra RPC.
        self.trade_callback: Callable[
            [list[str], str | None, int | None], Awaitable[None]
        ] | None = None

        for platform in self.platforms:
            try:
                # Create a simple dummy client that doesn't start blockhash updater
                from core.client import SolanaClient

                # Create a mock client class to avoid network operations
                class DummyClient(SolanaClient):
                    def __init__(self):
                        # Skip SolanaClient.__init__ to avoid starting blockhash updater
                        self.rpc_endpoint = "http://dummy"
                        self._client = None
                        self._cached_blockhash = None
                        self._blockhash_lock = None
                        self._blockhash_updater_task = None

                dummy_client = DummyClient()

                implementations = platform_factory.create_for_platform(
                    platform, dummy_client
                )
                parser = implementations.event_parser
                self.platform_parsers[platform] = parser
                # A pump.fun parser names its program; a LaunchLab one its
                # platform configs, keeping other launchpads out of the stream.
                self.platform_program_ids.update(parser.get_stream_filter_accounts())

                logger.info(
                    f"Registered platform {platform.value} with program ID {parser.get_program_id()}"
                )

            except Exception as e:
                logger.warning(f"Could not register platform {platform.value}: {e}")

    @property
    def continuity_revision(self) -> int:
        """Stream generation counter; consumers invalidate pre-gap state."""
        return self._continuity_revision

    def _bump_continuity(self) -> int:
        """Bump the continuity revision; consumers invalidate pre-gap state.

        Called on every stream discontinuity (reconnect/resume, in-stream
        filter update). Returns the new revision.
        """
        self._continuity_revision += 1
        return self._continuity_revision

    async def update_subscription(
        self,
        program_ids: set[str] | None = None,
        platforms: list[str] | None = None,
    ) -> int:
        """Apply a filter change WITHOUT closing the stream (R3-P2-15).

        Only effective when ``_in_stream_updates`` is enabled; otherwise the
        change is recorded and applied on the next reconnect (today's
        behavior), and the revision still bumps so consumers drop pre-gap
        state either way.

        Args:
            program_ids: New watched program ID set (None = keep current).
            platforms: New platform list (None = keep current).

        Returns:
            The new continuity revision.
        """
        if program_ids is not None:
            self.platform_program_ids = set(program_ids)
        if platforms is not None and platforms:
            self.platforms = [Platform(p) for p in platforms]
        rev = self._bump_continuity()
        if self._in_stream_updates and self._update_queue is not None:
            request = self._create_subscription_request()
            self._update_queue.put_nowait(request)
            logger.info(
                f"In-stream subscription update queued " f"(revision {rev})"
            )
        else:
            logger.info(
                f"Filter update recorded (revision {rev}); applies on "
                f"next reconnect (in-stream updates disabled)"
            )
        return rev

    async def _request_iterator(self, initial: geyser_pb2.SubscribeRequest):
        """Request stream: the initial subscription then queued updates.

        Yellowstone's Subscribe RPC takes a stream of requests; sending a
        fresh SubscribeRequest on the same stream updates the filters with no
        reconnect gap. Only used when in-stream updates are enabled.
        """
        yield initial
        while True:
            request = await self._update_queue.get()
            yield request

    async def _create_geyser_connection(self):
        """Establish a secure connection to the Geyser endpoint.

        Channel options mirror alpamayo's and Shyft's production clients:
        keepalive pings stop proxies/NAT from silently reaping the stream, and
        a large receive window is required because Yellowstone account/block
        messages can exceed the gRPC default of 4 MiB, which would abort the
        stream mid-update.
        """

        if self.auth_type == "x-token":
            auth = grpc.metadata_call_credentials(
                lambda _, callback: callback(
                    (("x-token", self.geyser_api_token),), None
                )
            )
        else:  # Default to basic auth
            auth = grpc.metadata_call_credentials(
                lambda _, callback: callback(
                    (("authorization", f"Basic {self.geyser_api_token}"),), None
                )
            )
        creds = grpc.composite_channel_credentials(grpc.ssl_channel_credentials(), auth)
        channel = grpc.aio.secure_channel(
            self.geyser_endpoint,
            creds,
            options=[
                ("grpc.max_receive_message_length", 128 * 1024 * 1024),
                ("grpc.max_send_message_length", 128 * 1024 * 1024),
                ("grpc.keepalive_time_ms", 30000),
                ("grpc.keepalive_timeout_ms", 10000),
                ("grpc.keepalive_permit_without_calls", 1),
                ("grpc.http2.max_pings_without_data", 0),
                ("grpc.enable_http_proxy", 0),
            ],
        )

        return geyser_pb2_grpc.GeyserStub(channel), channel

    def _create_subscription_request(self):
        """Create a subscription request for all monitored platforms."""

        request = geyser_pb2.SubscribeRequest()

        # Add all platform program IDs to the filter
        for program_id in self.platform_program_ids:
            filter_name = f"platform_filter_{program_id}"
            request.transactions[filter_name].account_include.append(str(program_id))
            request.transactions[filter_name].failed = False

        # Subscribe to slot-status updates so we can (a) advance a stale-slot
        # watchdog, (b) drop events from slots that later go SLOT_DEAD (leader
        # failed, no execution) and (c) use from_slot resume on reconnect.
        slot_filter = request.slots["memesnipe_slots"]
        slot_filter.filter_by_commitment = False
        slot_filter.interslot_updates = True

        request.commitment = geyser_pb2.CommitmentLevel.PROCESSED
        return request

    async def _watchdog(self) -> None:
        """Force a reconnect when the stream goes quiet or lags the cluster.

        Runs for the listener's lifetime. A geyser stream that silently stops
        delivering (half-open TCP, provider-side freeze) looks like an idle
        market; the only way to notice is wall-clock time compared to the last
        observed slot. Mirrors alpamayo's getHealth + Shyft's idle_monitor.

        The watchdog only sets _force_reconnect; the listen loop owns the
        channel and checks the event at the top of each read, closing and
        reconnecting when it is set. Raising here would only kill this task.
        """
        while True:
            await asyncio.sleep(self._watchdog_poll_interval)
            try:
                now = asyncio.get_event_loop().time()
                if self._last_event_time is None:
                    continue  # no stream yet; nothing to judge
                idle = (now - self._last_event_time) > self._watchdog_idle_threshold
                if idle and self._stream_healthy:
                    logger.warning(
                        "Geyser stream idle/watchdog: no update for "
                        f"{self._watchdog_idle_threshold}s, forcing reconnect"
                    )
                    self._stream_healthy = False
                    self._force_reconnect.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("watchdog error")

    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None = None,
        creator_address: str | None = None,
    ) -> None:
        """Listen for new token creations using Geyser subscription."""
        if not self.platform_parsers:
            logger.error("No platform parsers available. Cannot listen for tokens.")
            return

        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog())

        while True:
            try:
                stub, channel = await self._create_geyser_connection()
                request = self._create_subscription_request()
                if self._last_slot is not None:
                    request.from_slot = self._last_slot + 1
                    logger.info(
                        f"Resuming geyser subscription from slot {request.from_slot}"
                    )

                logger.info(f"Connected to Geyser endpoint: {self.geyser_endpoint}")
                logger.info(
                    f"Monitoring platforms: {[p.value for p in self.platforms]}"
                )
                logger.info(
                    f"Monitoring accounts: {[str(pid) for pid in self.platform_program_ids]}"
                )
                # A reconnect is a stream discontinuity: any cached state a
                # consumer built from the previous stream generation is stale
                # the moment we resume from _last_slot + 1. Bump the revision
                # so cache-holding consumers drop pre-gap state.
                self._bump_continuity()
                logger.info(
                    f"Continuity revision -> {self.continuity_revision}"
                )
                self._stream_healthy = True
                self._reconnect_delay = 1.0

                try:
                    # One stream for the whole connection; iterate it with a
                    # bounded read so the watchdog can break a hung stream.
                    # grpc.aio's StreamStreamCall exposes __aiter__ (async
                    # iterator) but not __anext__ directly; pull the iterator
                    # once and drive it with __anext__ calls.
                    # R3-P2-15: with in-stream updates enabled the request
                    # side is a queue-fed stream so update_subscription can
                    # change filters without a reconnect gap.
                    if self._in_stream_updates:
                        self._update_queue = asyncio.Queue()
                        request_iter = self._request_iterator(request)
                        self._stream_request_iter = request_iter
                    else:
                        self._stream_request_iter = None
                        request_iter = iter([request])
                    stream_iter = stub.Subscribe(request_iter).__aiter__()
                    while True:
                        if self._force_reconnect.is_set():
                            logger.warning(
                                "Force-reconnect requested by watchdog; closing stream"
                            )
                            self._force_reconnect.clear()
                            break
                        # A read that never returns (half-open stream that the
                        # watchdog noticed) must not block the loop forever:
                        # bound it and let the outer handler reconnect.
                        update = await asyncio.wait_for(
                            stream_iter.__anext__(),
                            timeout=max(2.0, self._watchdog_idle_threshold),
                        )
                        self._last_event_time = asyncio.get_event_loop().time()
                        metrics.incr("geyser.updates_received")
                        token_info = await self._process_update(update)
                        if not token_info:
                            continue
                        metrics.incr("geyser.tokens_detected")

                        logger.info(
                            f"New token detected: {token_info.name} ({token_info.symbol}) on {token_info.platform.value}"
                        )
                        _log_envelope(update)

                        # Apply filters
                        if match_string and not (
                            match_string.lower() in token_info.name.lower()
                            or match_string.lower() in token_info.symbol.lower()
                        ):
                            logger.info(
                                f"Token does not match filter '{match_string}'. Skipping..."
                            )
                            continue

                        if creator_address and str(token_info.user) != creator_address:
                            logger.info(
                                f"Token not created by {creator_address}. Skipping..."
                            )
                            continue

                        await token_callback(token_info)

                except Exception as e:
                    if isinstance(e, grpc.aio.AioRpcError):
                        logger.exception(f"gRPC error: {e.details()}")
                    else:
                        logger.exception("Geyser error occurred")
                    await self._backoff_sleep()
                    self._stream_healthy = False

                finally:
                    await channel.close()

            except Exception:
                logger.exception("Geyser connection error")
                logger.info(
                    f"Reconnecting in {self._reconnect_delay:.1f}s (jittered)..."
                )
                await self._backoff_sleep()
                self._stream_healthy = False

    async def _backoff_sleep(self) -> None:
        """Sleep with truncated exponential backoff + jitter, bounded."""
        delay = min(self._reconnect_delay, self._reconnect_delay_max)
        await asyncio.sleep(delay * (0.5 + random.random()))
        self._reconnect_delay = min(self._reconnect_delay * 2, self._reconnect_delay_max)

    async def _process_update(self, update) -> TokenInfo | None:
        """Process a Geyser update and extract token creation info.

        Delegates to each platform parser's geyser method rather than decoding
        instructions here: the parser prefers the CreateEvent from
        meta.log_messages, which carries the canonical creator (instruction
        args.creator is user-supplied) and marks the TokenInfo
        state_from_event so extreme_fast_mode can buy with zero RPC calls.
        Each parser filters on its own program id internally.
        """
        # Slot/health bookkeeping runs for every frame, including slot-status
        # frames that carry no token. Dead slots are remembered so events that
        # arrive before the failure (a create in a slot whose leader failed)
        # can be rejected at dispatch time.
        if update.HasField("slot"):
            slot = update.slot.slot
            self._last_slot = max(self._last_slot or 0, slot)
            self._last_event_time = asyncio.get_event_loop().time()
            status = update.slot.status
            if status == geyser_pb2.SlotStatus.SLOT_DEAD:
                self._dead_slots.add(slot)
                if len(self._dead_slots) > self._dead_slots_max:
                    self._dead_slots = set(sorted(self._dead_slots)[-self._dead_slots_max :])
                return None

        if not update.HasField("transaction"):
            return None

        update_slot = getattr(update.transaction, "slot", None)
        if update_slot is not None and update_slot in self._dead_slots:
            logger.warning(
                f"Ignoring event in dead slot {update_slot} (leader failed, no execution)"
            )
            return None

        try:
            for parser in self.platform_parsers.values():
                token_info = parser.parse_token_creation_from_geyser(update)
                if token_info:
                    return token_info

            # T3.2: no token creation here, but a leader may have traded an
            # existing mint. Forward the transaction's logs to the copy engine
            # (guarded so a slow mirror never blocks the geyser loop).
            if self.trade_callback is not None:
                logs = []
                meta = getattr(update.transaction, "meta", None)
                if meta is not None:
                    logs = list(meta.log_messages or [])
                if logs:
                    # R3-P2-11: pass the outer signer (message account_keys[0])
                    # so the parser can correct CPI/aggregator attribution.
                    outer_signer = None
                    try:
                        msg = update.transaction.transaction.message
                        keys = msg.account_keys
                        if keys:
                            outer_signer = str(keys[0])
                    except Exception:
                        outer_signer = None
                    # R3-P2-14: pass the transaction slot so the consumer's
                    # ordering buffer can release fills in ascending slot
                    # order (geyser can deliver fills out of order).
                    tx_slot = update_slot
                    await self.trade_callback(logs, outer_signer, tx_slot)

            return None

        except Exception:
            logger.exception("Error processing Geyser update")
            return None
