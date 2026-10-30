"""Universal trading coordinator that works with any platform."""

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from time import monotonic
from typing import Any

import aiohttp
import ipaddress
import socket as _socket
from solders.pubkey import Pubkey
from urllib.parse import urlparse

from backfill.transaction_crawler import TransactionCrawler
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
from monitoring.metrics import metrics
from platforms import get_platform_implementations
from screening.honeypot import honeypot_reason
from trading.base import TradeResult
from trading.exit_engine import (
    DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
    ExitSellVerdict,
    _exit_sell_verdict_for,
)
from trading.gate_stack import GateStackMixin
from trading.exit_engine import ExitEngineMixin
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller
from trading.position import ExitReason, Position
from trading.staged_exit import ShareRange
from trading.trail_exit import TrailExitState
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

# C10.2 (SSRF): scheme + host allowlist for off-chain metadata URIs. The
# URI is attacker-controlled (set by the token creator), so a bare GET is a
# server-side-request-forgery primitive: `http://169.254.169.254/latest/...`
# hits the cloud metadata service, `http://127.0.0.1:...` hits local
# services, `file://` etc. reads local files. ipfs:// and ar:// are the two
# legitimate non-http schemes seen on-chain; both are mapped to public
# gateways. Everything else must be https/http with a public resolved host.
_METADATA_ALLOWED_SCHEMES = {"https", "http"}
_IPFS_GATEWAY = "https://ipfs.io/ipfs/{path}"
_ARWEAVE_GATEWAY = "https://arweave.net/{path}"
# RFC 6598 shared address space (CGNAT). Python < 3.13 does not classify
# 100.64.0.0/10 as private, so check it explicitly.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def _safe_metadata_uri(uri: str) -> str | None:
    """Return a fetchable, SSRF-safe URL for a metadata URI, or None.

    Refuses non-http(s) schemes (except mapped ipfs/ar) before any network
    work, and refuses any host that resolves to a loopback, link-local,
    private, reserved, or unspecified address (covers 127.0.0.0/8,
    ::1/128, 169.254.0.0/16 incl. cloud metadata, 10/8, 172.16/12,
    192.168/16, CGNAT 100.64/10, and ::ffff: mapped IPv4). Failed/hostless
    parses also return None. The caller treats None as \"no snapshot\".
    """
    if not uri:
        return None
    uri = uri.strip()
    if not uri:
        return None

    # IPFS / Arweave native URIs -> public gateway URLs.
    if uri.startswith("ipfs://"):
        return _IPFS_GATEWAY.format(path=uri[len("ipfs://") :].lstrip("/"))
    if uri.startswith("ar://"):
        return _ARWEAVE_GATEWAY.format(path=uri[len("ar://") :].lstrip("/"))

    try:
        parsed = urlparse(uri)
    except ValueError:
        return None
    if parsed.scheme not in _METADATA_ALLOWED_SCHEMES or not parsed.hostname:
        return None

    # Resolve and reject private/reserved addresses. Port is kept only when
    # explicitly present in the URI (aiohttp treats "https://host" as 443).
    try:
        infos = _socket.getaddrinfo(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=_socket.SOCK_STREAM,
        )
    except _socket.gaierror:
        return None
    for _family, _type, _proto, _canon, sockaddr in infos:
        host = sockaddr[0]
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_private
            or ip.is_reserved
            or ip.is_unspecified
            or ip in _CGNAT_NET
        ):
            return None
    return uri



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


# Module constants for the shadow research capture (ruff PLR2004).
_SOL_PRICE_TTL_SECONDS = 60
_SOL_PRICE_TIMEOUT_SECONDS = 5
_HTTP_OK = 200


class UniversalTrader(ExitEngineMixin, GateStackMixin):
    """Universal trading coordinator that works with any supported platform."""

    #: Seconds shutdown cleanup may take before the process exits regardless.
    #: Cleanup settles for 15s per account before reading it, so this has to
    #: cover a session's worth of traded coins, not one.
    _CLEANUP_SHUTDOWN_BUDGET = 120.0

    #: R5-P2-3: log-only post-entry advisories — class default off so
    #: object.__new__-constructed test doubles (which skip __init__) read a
    #: defined flag.
    post_entry_advisories_enabled: bool = False

    def __init__(
        self,
        rpc_endpoint: str,
        wss_endpoint: str,
        private_key: str,
        buy_amount: float,
        buy_slippage: float,
        sell_slippage: float,
        # P2-EXIT-3: extra bps added to the sell-only slippage floor.
        sell_slippage_inflation_bps: int = 10,
        # P2-EXIT-2: max bps of pool depth a single sell may move price
        # (0 = off, sell whole balance in one tx).
        max_sell_impact_bps: int = 0,
        # P2-EXIT-5: close the user's token ATA in the same tx as a
        # full-balance sell (off by default; balance is RPC-verified first).
        close_ata_on_sell: bool = False,
        # R3-P1-3: Jupiter aggregator sell fallback (off by default). When a
        # platform sell fails with a pool-gone / liquidity signal, route the
        # exit through Jupiter's quote+swap API at the configured caps.
        aggregator_fallback: bool = False,
        aggregator_slippage_bps: int = 500,
        aggregator_max_price_impact_pct: float = 20.0,
        # P2-EXIT-1: absolute pool-depth floor (SOL). When the pool's real
        # quote reserves drop below this, force-exit immediately instead of
        # waiting for TP/SL — a pool that shallow may not absorb the sell
        # later. 0 = off.
        min_pool_depth_sol: float = 0.0,
        # P2-EXIT-4: slot-delta rug heuristics (off by default). After a buy,
        # a pool drained below early_drain_ratio of its entry depth, or a
        # coin with fewer than min_unique_buyers after check_slots, exits
        # immediately as ExitReason.RUG.
        rug_heuristics_enabled: bool = False,
        rug_min_unique_buyers: int = 2,
        rug_check_slots: int = 12,
        rug_early_drain_ratio: float = 0.5,
        # Shadow research capture (off by default): append-only JSONL ledger
        # of detect/sim_open/sim_close events so shadow runs produce
        # replayable data for backtesting. Belt-and-suspenders guard with
        # shadow_mode: capture only writes when BOTH are set.
        shadow_capture: Any = None,
        # R4-P1-7: strategies ledger (off by default). Shadow-only parallel
        # exit-strategy testing: every open shadow position is also evaluated
        # against every registered strategy (default tp_sl + staged_trail) so
        # one price path yields a per-strategy outcome row. Like
        # shadow_capture it is hard-guarded: only constructed in shadow mode.
        strategies_ledger: Any = None,
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
        # R4-P1-7: exit mode select. "tp_sl" (default) = the production
        # staged TP/SL path; "staged_trail" = asymmetric staged-then-trail
        # (sell initial share at +25%, arm a break-even floor at +10%, trail
        # the rest). Staged_trail uses the same partial-sell machinery as
        # the staged ranges.
        exit_mode: str = "tp_sl",
        # R4-P1-7 staged-trail knobs (fractions; only read when exit_mode
        # is "staged_trail" or the strategies ledger simulates it).
        trail_initial_tp_share: float = 0.4,
        trail_initial_tp_pct: float = 0.25,
        trail_second_tp_share: float = 0.0,
        trail_second_tp_pct: float = 0.0,
        trail_tp1_floor_pct: float = 0.0,
        trail_arm_pct: float = 0.10,
        trail_trail_pct: float = 0.08,
        trail_hard_stop_pct: float = 0.20,
        # trail10 knobs (exit_mode "trail10"): pure trailing stop, no
        # initial TP leg - arm at +10%, trail 10% off the peak, -20% stop.
        trail10_arm_pct: float = 0.10,
        trail10_trail_pct: float = 0.10,
        trail10_hard_stop_pct: float = 0.20,
        take_profit_percentage: float | None = None,
        stop_loss_percentage: float | None = None,
        # R3-P1-2: staged TP/SL ranges (off by default = current single
        # TP/SL unchanged). Lists of {from_pct, to_pct, step, share}.
        tp_ranges: list | None = None,
        sl_ranges: list | None = None,
        max_hold_time: int | None = None,
        # R4-P1-9: stagnation exit (opt-in, 0 = off). If the price stays
        # inside stagnation_band_pct of the entry price for
        # stagnation_exit_min minutes, exit — a flat token's hype window
        # has passed (53-trade cohort: flat 30-60 min = distribution).
        stagnation_exit_min: int = 0,
        stagnation_band_pct: float = 0.02,
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
        # T1.4 auction: multiply the dynamic sample by this before extra_fee
        # (1.0 = match the sample; >1.0 = bid above the observed top fee).
        priority_fee_bid_multiplier: float = 1.0,
        priority_fee_cache_ttl: float = 2.0,
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
        # P2-STRUC-2: persistent creator-PnL blacklist (off by default).
        creator_pnl_blacklist_enabled: bool = False,
        creator_pnl_db_path: str = "state/creator_pnl.db",
        creator_pnl_min_trades: int = 3,
        creator_pnl_max_net_sol: float = 0.0,
        max_market_cap_sol: float | None = None,
        min_curve_progress_sol: float | None = None,
        # R4-P1-1: multi-dimensional rug-risk score gate (0 = off). A
        # weighted sum over creator cadence / curve progress / holder
        # concentration / buyer set / dev balance; refuses entry when the
        # score exceeds rug_score_threshold. Calibrated on replay; ships
        # conservative (off).
        rug_score_threshold: float = 0.0,
        # R4-P1-2: creator-factory filter (0 = off). Refuses coins whose
        # creator launches more than factory_deploy_rate mints/hour.
        factory_deploy_rate: float = 0.0,
        min_market_cap_sol: float | None = None,
        # R4-P1-3: insider-coordination supply gate (0 = off). Refuses coins whose
        # early-buyer wallets co-fire across too many launches (MELT: 36.5%
        # of supply held by coordinated accounts on average). The tracker is
        # fed buy trade events; fail-open until it has observations.
        max_coordinated_supply_share: float = 0.0,
        # R4-P1-6: wash-trade share gate (0 = off). Refuses coins where this
        # fraction of recent quote volume comes from wallets that round-trip
        # (buy+sell the same mint) — fake volume (2507.01963). Fed by the
        # trade feed; fail-open (0) until observed.
        max_wash_share: float = 0.0,
        # R4-P3-1: fragility-scaled position sizing (ME2F; 0/1 = off).
        # When the holder-distribution whale score AND the observed trade
        # rate both spike (the paper's two-score trigger), buy size scales
        # down toward fragility_scale_min (1.0 = flat sizing, off).
        fragility_scale_min: float = 1.0,
        fragility_whale_threshold: float = 0.6,
        fragility_rate_threshold: float = 0.6,
        # R3-P0-2: Mayhem Mode coins are a distinct asset class (AI agent
        # mints +1B tokens and trades randomly for 24h; 1 in 50 graduations
        # fills the pool). Off by default: mayhem coins are skipped entirely.
        allow_mayhem_mode: bool = False,
        # R3-P1-4: force-exit when the creator sells (off by default).
        # The monitor loop watches the listener trade feed for a sell whose
        # trader is the tracked coin's creator, and exits on trigger.
        exit_on_creator_sell: bool = False,
        # R5-P2-3: post-entry protection signals as LOG-ONLY advisories
        # (off by default). When enabled, the monitor evaluates creator-sell,
        # pool-drain and buyer-growth signals on open positions but NEVER
        # exits on them — it logs the reason and records an advisory row so
        # the ledger can build evidence before any hard gate is armed.
        post_entry_advisories_enabled: bool = False,
        # R3-P2-12: event-driven sell-fill (off by default = current polling).
        # When on, a TradeEvent from the bot's OWN wallet on the log feed flags
        # the mint so the monitor evaluates the exit immediately instead of
        # waiting for the next price-check tick.
        event_driven_exits_enabled: bool = False,
        # R3-P2-14: slot-ordered feed (off by default = unordered). When on,
        # copy/monitor fills release in ascending slot order (out-of-order
        # fills would corrupt position state); the snipe path stays unordered.
        slot_ordered_feed_enabled: bool = False,
        # R3-P2-15: in-stream filter updates (off by default). When on, the
        # geyser request stream is queue-fed so update_subscription() changes
        # watched filters without a reconnect gap; the continuity revision
        # still bumps so consumers invalidate pre-gap cache state.
        in_stream_updates_enabled: bool = False,
        # R3-P1-5: holder-concentration gates (0 = off). min_unique_holders
        # refuses coins whose observed top-account set is smaller than the
        # floor; max_top10_concentration refuses coins where the top-10
        # holders control more than this fraction of supply. The background
        # refresher fills an immutable snapshot; _skip_reason reads only the
        # snapshot (never RPC on the hot path) and FAILS OPEN when a mint has
        # no snapshot yet.
        min_unique_holders: int = 0,
        max_top10_concentration: float = 0.0,
        # R4-P1-10: Gini-coefficient gate (0 = off). Refuses coins where the
        # OBSERVED holder balance distribution is too unequal — rugs run
        # Gini > 0.8, normal projects < 0.5 (2608.01609). Same snapshot,
        # same fail-open contract as the other holder gates.
        max_gini_coefficient: float = 0.0,
        # R3-P2-8: metadata socials gate (0 = off). When >0, the pre-buy
        # gate requires the token's metadata JSON to carry at least this many
        # non-empty extensions (socials/description). The URI read happens in
        # the background refresher; _skip_reason checks the snapshot only.
        min_metadata_extensions: int = 0,
        # R3-P2-10: session risk guards (0 = off).
        max_concurrent_positions: int = 0,
        daily_loss_limit_sol: float = 0.0,
        budget_sol: float = 0.0,
        # R3-P0-3: holder-reward coins (creator fee to holders; creator is a
        # pump.fun address). Trading is identical, so the default is "trade";
        # "skip" refuses them. cashback coins are legacy-only (create_v2
        # rejects them) but keep trading, so they are never gated here.
        holder_reward_mode: str = "trade",
        max_tracked_tokens: int = 500,
        token_queue_maxsize: int = 256,
        # C7.2: where the durable unsold registry is persisted. Relative to
        # the bot's cwd (state/ dir); None disables persistence (tests).
        unsold_persist_path: str | None = "state/unsold_positions.json",
        # T2.4 confirmation window: require this many consecutive curve-gate
        # passes (1 = single check, current behavior) before buying.
        consecutive_match_count: int = 1,
        gate_recheck_interval: float = 0.5,
        # T2.5 honeypot gates (off by default; fail-open on read errors).
        honeypot_freeze_authority: bool = False,
        min_curve_hold_fraction: float = 0.0,
        # T2.6: RPC catch-up backfill (disabled by default)
        backfill_enabled: bool = False,
        backfill_fetch_concurrency: int = 5,
        backfill_page_size: int = 100,
        backfill_max_pages: int = 5,
        backfill_checkpoint: str | None = None,
        # T3.2: copy-trader engine (disabled by default; off = safe baseline)
        copy_trader_enabled: bool = False,
        copy_trader_db: str | None = None,
        copy_trader_min_interval: float = 5.0,
        # R4-P2-1/2/3: copy-hardening knobs (all 0/1 = off = current behavior).
        copy_ring_cofire_threshold: int = 0,
        copy_mirror_jitter_ms: float = 0.0,
        copy_mirror_split_quanta: int = 1,
        copy_min_leader_score: float = 0.0,
        # Split buy/sell priority fees: overrides for priority_fees.fixed_amount
        buy_priority_fee: int | None = None,
        sell_priority_fee: int | None = None,
        # Stuck-position wallet sweeper: seconds between scans for orphaned
        # tokens (0 = off).
        stuck_sweep_interval: int = 0,
        # Shadow/paper mode (#8): record simulated decisions and outcomes
        # without sending transactions or moving funds.
        shadow_mode: bool = False,
        # Fail-closed gates: when a quality gate cannot get its data (RPC
        # error), refuse the coin instead of letting it through unchecked.
        fail_closed_gates: bool = True,
        marry_mode: bool = False,
        yolo_mode: bool = False,
        # Compute unit configuration
        compute_units: dict | None = None,
        # T2.2 Jito bundle executor: when enabled, sends go through a
        # JitoExecutor with the default path as fallback.
        jito_enabled: bool = False,
        jito_tip_lamports: int = 10_000,
        jito_endpoints: list[str] | None = None,
        # P2-STRUC-4: base58 auth keypairs rotated per request (region
        # rotation); empty = unsigned (pre-existing behavior).
        jito_auth_keypairs: list[str] | None = None,
        # R3-P2-6: DontFront/MEV shield (off by default). When real mode is
        # armed and jito is on, route sends through the DontFront-compatible
        # block-engine variant (sandwich protection). Shadow mode unaffected.
        jito_dontfront: bool = False,
        # Node provider configuration
        max_rps: float = 25.0,
        burst_size: int | None = None,
        # P2-STRUC-1: background blockhash refresh cadence (s); 0.05 min.
        blockhash_refresh_interval: float = 5.0,
        # R3-P2-2: durable nonce (off by default). When set to the base58
        # nonce ACCOUNT address, sends use the nonce account's stored
        # blockhash (advance + authority) instead of the short-lived recent
        # blockhash — replay-protected and valid far beyond 150 blocks.
        durable_nonce_account: str | None = None,
        # R3-P2-3: address lookup tables (off by default). When set to a list
        # of base58 ALT addresses, the buy/sell message compresses static keys
        # through the tables (smaller tx, lower fee, faster validation).
        alt_addresses: list[str] | None = None,
        # R3-P2-5: PumpSwap graduation watcher (off by default). Detects the
        # CompletePumpAmmMigrationEvent in logs so post-graduation leader
        # activity is seen as PumpSwap trades, not curve trades.
        migrate_watcher_enabled: bool = False,
        # R4-P0-3: contention-mode fee racing (off by default). When the
        # create-event rate (launches/min over the trailing window) exceeds
        # contention_creates_per_min, the buy+sell fee managers arm and
        # escalate their bids by contention_multiplier, capped at
        # min(hard_cap, max_fee_fraction_of_position of the trade quote) so
        # a fee surge can never eat the position (tierzero: plain-fee
        # landing collapses to 20-40% under launch competition; the Kaggle
        # top sniper pays 21% of gross on fees but nets on 8.5:1 asymmetry).
        contention_enabled: bool = False,
        contention_multiplier: float = 2.5,
        contention_creates_per_min: float = 20.0,
        contention_window_s: float = 60.0,
        max_fee_fraction_of_position: float = 0.005,
        # R4-P1-4: graduation-probability entry timing — LOG-ONLY advisory.
        # At curve-gate time (same RPC read as the progress gate) tag the
        # candidate with its historical graduation-outcome band. Never gates.
        grad_timing_enabled: bool = False,
        grad_band_stats_path: str = "state/grad_band_stats.json",
    ):
        """Initialize the universal trader."""
        self.contention_enabled = contention_enabled
        self.contention_multiplier = contention_multiplier
        self.contention_creates_per_min = contention_creates_per_min
        self.contention_window_s = contention_window_s
        self.max_fee_fraction_of_position = max_fee_fraction_of_position
        # R4-P1-4: graduation-band advisory (log-only; off default).
        from screening.grad_timing import load_band_stats

        self.grad_timing_enabled = grad_timing_enabled
        self._grad_band_stats = load_band_stats(grad_band_stats_path)
        # R4-P0-3: rolling create-event timestamps for the launch-flood
        # rate test (monotonic seconds, same clock as token_timestamps).
        self._contention_creates: list[float] = []

        # Core components
        self.solana_client = SolanaClient(
            rpc_endpoint,
            max_rps=max_rps,
            burst_size=burst_size,
            blockhash_refresh_interval=blockhash_refresh_interval,
            durable_nonce_account=durable_nonce_account,
            alt_addresses=alt_addresses,
        )
        self.wallet = Wallet(private_key)
        self.priority_fee_manager = PriorityFeeManager(
            client=self.solana_client,
            enable_dynamic_fee=enable_dynamic_priority_fee,
            enable_fixed_fee=enable_fixed_priority_fee,
            fixed_fee=buy_priority_fee or fixed_priority_fee,
            extra_fee=extra_priority_fee,
            hard_cap=hard_cap_prior_fee,
            bid_multiplier=priority_fee_bid_multiplier,
            fee_cache_ttl=priority_fee_cache_ttl,
            # R4-P0-3: contention escalation multiplier (off until armed).
            contention_multiplier=contention_multiplier,
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
            bid_multiplier=priority_fee_bid_multiplier,
            fee_cache_ttl=priority_fee_cache_ttl,
            # R4-P0-3: contention escalation multiplier (off until armed).
            contention_multiplier=contention_multiplier,
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
        # T2.2: executor chain. Jito first (bundle + tip), default path as
        # fallback; dedupe by signature so a rejected bundle cannot double-send.
        # Disabled (default): plain direct send, behavior unchanged.
        from execution.executor import (
            DefaultExecutor,
            FallbackChainExecutor,
            JitoExecutor,
        )

        if jito_enabled:
            # P2-STRUC-4: optional per-request auth keypairs (region
            # rotation); empty list = unsigned, pre-existing behavior.
            auth_keypairs: list = []
            if jito_auth_keypairs:
                from solders.keypair import Keypair

                for b58key in jito_auth_keypairs:
                    try:
                        auth_keypairs.append(Keypair.from_base58_string(b58key))
                    except Exception:
                        logger.exception(
                            f"Ignoring invalid jito auth keypair "
                            f"({b58key[:6]}...); rotating the valid ones only"
                        )
            self.executor = FallbackChainExecutor(
                [
                    JitoExecutor(
                        self.solana_client,
                        endpoints=jito_endpoints,
                        tip_lamports=jito_tip_lamports,
                        auth_keypairs=auth_keypairs or None,
                        dontfront=jito_dontfront,
                    ),
                    DefaultExecutor(self.solana_client, max_retries=max_retries),
                ]
            )
            # R4-P0-3: keep the Jito leg reachable so the contention arm can
            # escalate its tip (fallback chain hides it behind the chain).
            self._jito_executor = self.executor.executors[0]
        else:
            self.executor = None
            self._jito_executor = None

        # R3-P1-3: Jupiter aggregator fallback client (only built when the
        # feature is enabled; None otherwise keeps the fallback off).
        if aggregator_fallback:
            from execution.jupiter_client import JupiterClient

            self.jupiter_client = JupiterClient(
                slippage_bps=aggregator_slippage_bps,
                max_price_impact_pct=aggregator_max_price_impact_pct,
            )
        else:
            self.jupiter_client = None

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
                executor=self.executor,
            ),
            PlatformAwareSeller(
                self.solana_client,
                self.wallet,
                self.sell_priority_fee_manager,
                sell_slippage,
                max_retries,
                compute_units=self.compute_units,
                executor=self.executor,
                # P2-EXIT-3: exit floor loosened by this many extra bps so a
                # sell lands even if the pool moved after the read.
                sell_slippage_inflation_bps=sell_slippage_inflation_bps,
                # P2-EXIT-2: impact-bounded sell clamp (0 = off).
                max_sell_impact_bps=max_sell_impact_bps,
                # P2-EXIT-5: close ATA in-tx on full-balance sell.
                close_ata_on_sell=close_ata_on_sell,
                # R3-P1-3: Jupiter aggregator sell fallback (off by default).
                aggregator_fallback=aggregator_fallback,
                jupiter_client=self.jupiter_client,
                aggregator_slippage_bps=aggregator_slippage_bps,
                aggregator_max_price_impact_pct=aggregator_max_price_impact_pct,
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
            in_stream_updates_enabled=getattr(self, "in_stream_updates_enabled", False),
        )

        # Trading parameters
        self.buy_amount = buy_amount
        self.buy_slippage = buy_slippage
        self.sell_slippage = sell_slippage
        self.sell_slippage_inflation_bps = sell_slippage_inflation_bps
        self.max_sell_impact_bps = max_sell_impact_bps
        self.close_ata_on_sell = close_ata_on_sell
        # R3-P1-3: Jupiter aggregator fallback (off by default). The client is
        # built once and shared by the seller; None when the feature is off.
        self.aggregator_fallback = aggregator_fallback
        self.aggregator_slippage_bps = aggregator_slippage_bps
        self.aggregator_max_price_impact_pct = aggregator_max_price_impact_pct
        self.min_pool_depth_sol = min_pool_depth_sol
        # P2-EXIT-4: rug-heuristic state (off by default).
        self.rug_heuristics_enabled = rug_heuristics_enabled
        self.rug_min_unique_buyers = rug_min_unique_buyers
        self.rug_check_slots = rug_check_slots
        self.rug_early_drain_ratio = rug_early_drain_ratio
        # Research data capture — lives in BOTH shadow and live modes so real
        # trades are recorded for calibration (user directive: collect data
        # while live). The mode is tagged per row by the caller. Enabled purely
        # by the research.shadow_capture_enabled config; shadow_mode no longer
        # gates it (bot_runner passes None when disabled).
        self.shadow_capture = shadow_capture
        # R4-P1-7: strategies ledger — same live/shadow contract: every open
        # position (simulated or real) is evaluated against every strategy so
        # one price path yields a per-strategy outcome row.
        self.strategies_ledger = strategies_ledger
        # mint -> set of buyer wallet strings observed via trade events.
        self._rug_buyers: dict[str, set[str]] = {}
        # R4-P1-3: insider-coordination tracker (fed the same buy events).
        from screening.coordination import CoFireTracker
        from screening.grad_timing import (  # R4-P1-4
            assess as grad_timing_assess,
            load_band_stats,
        )

        self._cofire = CoFireTracker()
        self.max_coordinated_supply_share = max_coordinated_supply_share
        # R4-P1-6: wash-trade / LPI manipulation detector (fed every trade
        # event; no RPC). Gate config: max_wash_share (0 = off).
        from screening.wash_lpi import WashLpiDetector

        self._wash_lpi = WashLpiDetector()
        self.max_wash_share = max_wash_share
        # R4-P1-8: momentum-fade advisory state (log-only; peak trade rate
        # and peak price per mint for the -50%-rate-without-new-high test).
        self._momentum_peak_rate: dict[str, float] = {}
        self._momentum_peak_price: dict[str, float] = {}
        # R4-P3-1: fragility sizing knobs (off defaults).
        self.fragility_scale_min = fragility_scale_min
        self.fragility_whale_threshold = fragility_whale_threshold
        self.fragility_rate_threshold = fragility_rate_threshold
        # mint -> (entry_slot, entry_real_quote_lamports)
        self._rug_entry: dict[str, tuple[int, float]] = {}
        self.max_retries = max_retries
        self.extreme_fast_mode = extreme_fast_mode
        self.extreme_fast_token_amount = extreme_fast_token_amount

        # Exit strategy parameters
        self.exit_strategy = exit_strategy.lower()
        # R4-P1-7: exit mode + staged-trail knobs. Validated against the
        # same VALID_VALUES list as exit_strategy at config load; here we
        # keep a normalized lowercased copy plus the trail configuration.
        self.exit_mode = exit_mode.lower()
        self.trail10_arm_pct = trail10_arm_pct
        self.trail10_trail_pct = trail10_trail_pct
        self.trail10_hard_stop_pct = trail10_hard_stop_pct
        self.trail_initial_tp_share = float(trail_initial_tp_share)
        self.trail_initial_tp_pct = float(trail_initial_tp_pct)
        self.trail_second_tp_share = float(trail_second_tp_share)
        self.trail_second_tp_pct = float(trail_second_tp_pct)
        self.trail_tp1_floor_pct = float(trail_tp1_floor_pct)
        self.trail_arm_pct = float(trail_arm_pct)
        self.trail_trail_pct = float(trail_trail_pct)
        self.trail_hard_stop_pct = float(trail_hard_stop_pct)
        # Per-position trail state for exit_mode == "staged_trail".
        self._trail_states: dict[str, Any] = {}
        self.take_profit_percentage = take_profit_percentage
        self.stop_loss_percentage = stop_loss_percentage
        # R3-P1-2: staged exits. Parse once at construction; [] = feature off
        # (single TP/SL unchanged). Parse failures are loud (config error),
        # not silently ignored.
        self.tp_ranges: list = []
        self.sl_ranges: list = []
        if tp_ranges:
            self.tp_ranges = [
                ShareRange.from_dict(r) if not isinstance(r, ShareRange) else r
                for r in tp_ranges
            ]
        if sl_ranges:
            self.sl_ranges = [
                ShareRange.from_dict(r) if not isinstance(r, ShareRange) else r
                for r in sl_ranges
            ]
        # One-order state: per-step fired markers (band_index, trigger price)
        # so a partial sell does not repeat every price check.
        self._tp_fired: set[tuple[int, float]] = set()
        self._sl_fired: set[tuple[int, float]] = set()
        self.max_hold_time = max_hold_time
        # R4-P1-9: stagnation exit state (off by default).
        self.stagnation_exit_min = stagnation_exit_min
        self.stagnation_band_pct = stagnation_band_pct
        # The attempt cap is clamped: below 1 would mean "never try to sell".
        self.price_check_interval, self.max_exit_sell_attempts = (
            price_check_interval,
            max(1, max_exit_sell_attempts),
        )
        # P2-STRUC-2: creator-PnL persistent blacklist (off by default).
        self.creator_pnl_blacklist_enabled = creator_pnl_blacklist_enabled
        self.creator_pnl_min_trades = creator_pnl_min_trades
        self.creator_pnl_max_net_sol = creator_pnl_max_net_sol
        self.creator_pnl_store: Any = None
        if creator_pnl_blacklist_enabled and creator_pnl_db_path:
            from screening.creator_pnl_store import CreatorPnlStore

            self.creator_pnl_store = CreatorPnlStore(creator_pnl_db_path)
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
        self.blocked_creators = {str(a) for a in (blocked_creators or [])}
        self.max_market_cap_sol = max_market_cap_sol
        self.min_curve_progress_sol = min_curve_progress_sol
        # R4-P1-1: rug-risk score gate (0 = off) + the local state the score
        # reads: per-creator launch timestamps for cadence, and the per-mint
        # early-buyer set already tracked by the rug heuristics (_rug_buyers).
        self.rug_score_threshold = rug_score_threshold
        self._creator_launch_ts: dict[str, list[float]] = {}
        # C4.3: per-mint guard so a single detected launch records its
        # creator's cadence exactly ONCE, no matter how many times gates
        # re-evaluate the mint (detection, backfill, pre-buy recheck).
        self._cadence_recorded_mints: set[str] = set()
        self.factory_deploy_rate = factory_deploy_rate
        self.min_market_cap_sol = min_market_cap_sol
        self.allow_mayhem_mode = allow_mayhem_mode
        self.holder_reward_mode = holder_reward_mode
        self.exit_on_creator_sell = exit_on_creator_sell
        self.post_entry_advisories_enabled = post_entry_advisories_enabled
        # R3-P1-4: mint key -> set of known creator wallets; when the trade
        # feed sees a sell from one of these for a tracked mint, the monitor
        # exits on trigger.
        self._creator_sellers: dict[str, set[str]] = {}
        self._creator_sold_for_exit: set[str] = set()
        # R3-P2-12: event-driven sell-fill (off by default = current polling).
        self.event_driven_exits_enabled = event_driven_exits_enabled
        self._own_trade_prompt: set[str] = set()
        # R3-P1-5: holder-concentration snapshot store + gate config.
        from screening.holder_snapshot import HolderSnapshotStore

        self._holder_store = HolderSnapshotStore()
        self.min_unique_holders = min_unique_holders
        self.max_top10_concentration = max_top10_concentration
        self.max_gini_coefficient = max_gini_coefficient
        self._holder_refresh_task = None
        self._metadata_refresh_task = None
        # R3-P2-8: metadata socials gate (0 = off) + snapshot store.
        self.min_metadata_extensions = min_metadata_extensions
        from screening.metadata_socials import MetadataStore

        self._metadata_store = MetadataStore()
        # R3-P2-10: session risk guards (off by default: 0).
        from trading.risk_guard import RiskTracker

        self.risk_tracker = RiskTracker(
            max_concurrent_positions=max_concurrent_positions or 0,
            daily_loss_limit_sol=daily_loss_limit_sol or 0.0,
            budget_sol=budget_sol or 0.0,
        )
        # R3-P2-13: strict buy/sell serialization. One lock guards the
        # buy-vs-sell overlap (WSOL wrap/unwrap balance races): a sell never
        # starts while a buy is in flight and vice versa. Off-by-default
        # semantics are preserved (a lock held by nobody is a no-op).
        self._trade_serialization_lock = asyncio.Lock()
        # R3-P2-14: slot-ordered delivery for the monitor/copy path. The
        # geyser stream can deliver fills out of order; this buffer releases
        # them in ascending slot order so a copy fill is never processed
        # before the buy it depends on. Disabled-first (None = unordered).
        self.slot_ordered_feed_enabled = slot_ordered_feed_enabled
        self._slot_buffer = None
        if slot_ordered_feed_enabled:
            from monitoring.slot_buffer import SlotBuffer

            self._slot_buffer = SlotBuffer()
        self.in_stream_updates_enabled = in_stream_updates_enabled
        # R3-P2-5: PumpSwap graduation watcher (off by default).
        if migrate_watcher_enabled:
            from research.migrate_watcher import MigrateWatcher

            self.migrate_watcher = MigrateWatcher()
        else:
            self.migrate_watcher = None
        self.max_tracked_tokens = max_tracked_tokens
        self.token_queue_maxsize = max(1, int(token_queue_maxsize))
        self.consecutive_match_count = max(1, consecutive_match_count)
        self.gate_recheck_interval = max(0.1, gate_recheck_interval)
        self.honeypot_freeze_authority = honeypot_freeze_authority
        # R3-P2-1: pre-buy risk gate snapshot. Honeypot verdicts are cached
        # with a TTL so the gate never RPCs on the hot path when warm; the
        # holder-concentration + creator-PnL lookups are already local
        # (snapshot store / SQLite). Cache is mint -> (checked_at, reason).
        self._honeypot_cache: dict[str, tuple[float, str | None]] = {}
        self._honeypot_cache_ttl = 30.0
        self.min_curve_hold_fraction = max(0.0, min_curve_hold_fraction)
        self.backfill_enabled = backfill_enabled
        self.backfill_fetch_concurrency = backfill_fetch_concurrency
        self.backfill_page_size = backfill_page_size
        self.backfill_max_pages = backfill_max_pages
        self.backfill_checkpoint = backfill_checkpoint
        self.copy_trader_enabled = copy_trader_enabled
        self.copy_trader_db = copy_trader_db
        self.copy_trader_min_interval = copy_trader_min_interval
        # R4-P2-1/2/3: copy-hardening knobs (off defaults).
        self.copy_ring_cofire_threshold = copy_ring_cofire_threshold
        self.copy_mirror_jitter_ms = copy_mirror_jitter_ms
        self.copy_mirror_split_quanta = max(1, int(copy_mirror_split_quanta))
        self.copy_min_leader_score = copy_min_leader_score
        self.copy_trader_engine = None
        self.buy_priority_fee = buy_priority_fee
        self.sell_priority_fee = sell_priority_fee
        self.stuck_sweep_interval = stuck_sweep_interval
        self.shadow_mode = shadow_mode
        self.fail_closed_gates = fail_closed_gates
        self.marry_mode = marry_mode
        self.yolo_mode = yolo_mode

        # State tracking
        self.traded_mints: set[Pubkey] = set()
        self.traded_token_programs: dict[
            str, Pubkey
        ] = {}  # Maps mint (as string) to token_program_id
        self.token_queue: asyncio.Queue = asyncio.Queue(
            maxsize=self.token_queue_maxsize
        )
        self.processing: bool = False
        self.processed_tokens: set[str] = set()
        # C3.3: mints currently sitting in the queue (enqueue-time dedupe).
        # The listener can fire the same mint repeatedly; without this a
        # duplicate slips past the processed_tokens check and is processed
        # twice (seen live: duplicate entries in the queue for one mint).
        # Removed when the processor/drain dequeues the token, so the set
        # stays bounded by the queue size, not by lifetime events.
        self._queued_mints: set[str] = set()
        # Coins whose graduation has been logged, so it is said once.
        self._graduated_mints: set[Pubkey] = set()
        self.token_timestamps: dict[str, float] = {}
        self.token_launch_timestamps: dict[str, float] = {}
        # Positions whose bounded exit retries are spent: mint -> (token_info,
        # position). A background watcher keeps re-pricing and selling them
        # until they clear or the TTL expires, instead of abandoning them
        # ("no longer monitored" from the pre-persistent code).
        self._unsold_positions: dict[str, tuple[TokenInfo, Position]] = {}
        # C7.2: durable unsold registry — the unsold set survives a restart
        # (crash/upgrade) instead of stranding tokens with no watcher. On boot
        # we rehydrate from state/unsold_positions.json and re-arm the TTL
        # watcher so a process restart cannot lose a position that the hot
        # path already gave up on. Saved on every mutation.
        self._unsold_persist_path = unsold_persist_path
        # mints with a live TP/SL exit monitor; guards against duplicate buys
        self._active_exit_mints: set[str] = set()
        # C3.4: pre-buy mint reservation (duplicate-buy race). A mint is
        # reserved BEFORE the buyer executes so a second detect of the same
        # mint cannot slip a second order in while the first buy is in
        # flight (the existing per-mint check only fires after a successful
        # position setup, which is too late — the duplicate buy is already
        # signed). Released in the finally of the buy path.
        self._buy_reservations: set[str] = set()
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
                accounts = (
                    await self.solana_client.get_token_accounts_by_owner_all_programs(
                        self.wallet.pubkey
                    )
                )
            except Exception:
                logger.exception("Stuck-token sweep: wallet scan failed")
                continue
            traded = {str(m) for m in self.traded_mints}
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
                        or key in traded
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
        logger.info(f"Exit mode: {self.exit_mode}")

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

            # C7.2: rehydrate the durable unsold registry from the previous
            # process run BEFORE the listener starts, so tokens that lost
            # their watcher to a restart get re-armed now. Re-arms the TTL
            # watcher when anything was recovered.
            self._rehydrate_unsold()

            # T2.6: replay missed launches/events after a geyser gap. Best-effort:
            # failures are logged, never fatal.
            await self._backfill_gap()

            # T3.2: optional copy-trader engine (disabled by default).
            await self._init_copy_trader()

            # P2-EXIT-4 / R3-P1-4 / R3-P2-12 / C4.2: rug-buyer tracking,
            # creator-sell exits, event-driven exits, and the wash/cofire/
            # rug-score gates all consume the listener's trade feed — wire
            # it whenever ANY of them is on (and the copy engine hasn't
            # claimed the callback first). The gates' thresholds are the
            # enablers: a non-zero max_wash_share / rug_score_threshold /
            # copy_ring_cofire_threshold only means anything when the feed
            # is observed.
            feed_dependents = (
                self.rug_heuristics_enabled
                or self.exit_on_creator_sell
                or self.event_driven_exits_enabled
                or self.post_entry_advisories_enabled
                or self.max_wash_share > 0  # C4.2
                or self.rug_score_threshold > 0  # C4.2
                or self.copy_ring_cofire_threshold > 0  # C4.2
            )
            if feed_dependents and (
                not hasattr(self.token_listener, "trade_callback")
                or self.token_listener.trade_callback is None
            ):
                await self._init_rug_trade_feed()

            # Choose operating mode based on yolo_mode
            if not self.yolo_mode:
                # Single token mode: process one token and exit
                logger.info(
                    "Running in single token mode - will process one token and exit"
                )
                # R3-P1-5: holder-snapshot refresher (only when gates on).
                if self._holder_gates_on():
                    self._holder_refresh_task = asyncio.create_task(
                        self._holder_refresh_loop()
                    )
                # R3-P2-8: metadata-extension refresher (only when gate on).
                if self.min_metadata_extensions > 0:
                    self._metadata_refresh_task = asyncio.create_task(
                        self._metadata_refresh_loop()
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
                metrics_task = asyncio.create_task(self._metrics_logger())
                # R3-P1-5: holder-snapshot refresher (only when gates on).
                if self._holder_gates_on():
                    self._holder_refresh_task = asyncio.create_task(
                        self._holder_refresh_loop()
                    )
                # R3-P2-8: metadata-extension refresher (only when gate on).
                if self.min_metadata_extensions > 0:
                    self._metadata_refresh_task = asyncio.create_task(
                        self._metadata_refresh_loop()
                    )

                try:
                    await self.token_listener.listen_for_tokens(
                        lambda token: self._queue_token(token),
                        self.match_string,
                        self.bro_address,
                    )
                except Exception:
                    logger.exception("Token listening stopped due to error")
                finally:
                    # Drain what is still fresh before cancelling, mirroring
                    # carbon's ProcessPending shutdown strategy: queued
                    # launches would otherwise be dropped mid-flight.
                    await self._drain_token_queue(self.max_token_age)
                    processor_task.cancel()
                    try:
                        await processor_task
                    except asyncio.CancelledError:
                        pass
                    metrics_task.cancel()
                    try:
                        await metrics_task
                    except asyncio.CancelledError:
                        pass

        except Exception:
            logger.exception("Trading stopped due to error")

        finally:
            if self._stuck_sweeper_task is not None:
                self._stuck_sweeper_task.cancel()
            # R3-P1-5: cancel the holder-snapshot refresher if running.
            if self._holder_refresh_task is not None:
                self._holder_refresh_task.cancel()
                try:
                    await self._holder_refresh_task
                except asyncio.CancelledError:
                    pass
            # R3-P2-8: cancel the metadata-extension refresher if running.
            meta_task = getattr(self, "_metadata_refresh_task", None)
            if meta_task is not None:
                meta_task.cancel()
                try:
                    await meta_task
                except asyncio.CancelledError:
                    pass
            await self._cleanup_without_interruption()
            # Shadow research capture: one aggregate row per session so a
            # backtest can see the whole run at a glance.
            if self.shadow_capture is not None:
                try:
                    self.shadow_capture.summary(
                        shadow=True,
                        tokens_traded=len(self.traded_mints),
                        rug_heuristics=self.rug_heuristics_enabled,
                        creator_pnl_blacklist=(self.creator_pnl_blacklist_enabled),
                        exit_strategy=self.exit_strategy,
                    )
                except Exception:
                    logger.exception("Shadow summary capture failed")
            if self.strategies_ledger is not None:
                try:
                    self.strategies_ledger.summary(
                        shadow=True,
                        exit_mode=self.exit_mode,
                        exit_strategy=self.exit_strategy,
                    )
                except Exception:
                    logger.exception("Strategies-ledger summary failed")
            # R3-P1-1: net-metrics report from the research ledger. Shadow
            # mode: summarize shadow_data/ (the sim trades just recorded).
            # Real mode: also fold in trades/ when present. Best-effort —
            # a metrics failure must never break shutdown.
            if self.shadow_capture is not None:
                try:
                    from research.net_metrics import compute_metrics

                    metrics = compute_metrics(
                        real_trades_dir="trades" if not self.shadow_mode else None,
                        source="session",
                    )
                    logger.info("\n%s", metrics.to_report())
                except Exception:
                    logger.exception("Net-metrics report failed")
            logger.info("Universal Trader has shut down")

    async def _init_copy_trader(self) -> None:
        """T3.2: build the copy-trader engine and register its geyser hook.

        Disabled by default (no targets, no mirror). When enabled, mirrors
        leader buys through the shared buyer, subject to per-wallet limits.
        """
        if not self.copy_trader_enabled:
            return
        try:
            from copy_trader.engine import CopyTraderEngine
            from copy_trader.gmgn_profiler import WalletProfiler
            from screening.copy_target_store import CopyTargetStore

            db_path = self.copy_trader_db or "state/copy_targets.db"
            store = CopyTargetStore(db_path)
            engine = CopyTraderEngine(
                self.solana_client,
                store,
                buy_mirror=self._mirror_buy,
                sell_mirror=self._mirror_sell,  # R5-P0-4: leader-sell lifecycle
                min_buy_interval=self.copy_trader_min_interval,
                profiler=WalletProfiler(),
                ring_cofire_threshold=self.copy_ring_cofire_threshold,
                mirror_jitter_ms=self.copy_mirror_jitter_ms,
                mirror_split_quanta=self.copy_mirror_split_quanta,
                min_leader_score=self.copy_min_leader_score,
                # C5.3: durable mirror ledger — a restart must not strand
                # open copies (engine falls back to in-memory on error).
                persist_path="state/copy_positions.json",
            )

            async def on_trade_logs(
                logs: list[str],
                outer_signer: str | None = None,
                slot: int | None = None,
            ) -> None:
                # R3-P2-14: when slot ordering is enabled, buffer the copy
                # feed by slot and release in ascending order — an
                # out-of-order geyser delivery must not mirror a leader buy
                # before the fill it depends on.
                if self.slot_ordered_feed_enabled and self._slot_buffer is not None:
                    if slot is not None and self._slot_buffer.push(
                        slot, (logs, outer_signer)
                    ):
                        for buffered_logs, buffered_signer in self._slot_buffer.drain():
                            await self._consume_copy_logs(
                                engine, buffered_logs, buffered_signer
                            )
                        return
                    # Slot missing (non-geyser listener): process immediately.
                await self._consume_copy_logs(engine, logs, outer_signer)

            if hasattr(self.token_listener, "trade_callback"):
                self.token_listener.trade_callback = on_trade_logs
            self.copy_trader_engine = engine
            logger.info(
                f"Copy-trader engine enabled (db={db_path}, "
                f"min_interval={self.copy_trader_min_interval}s)"
            )
        except Exception:
            logger.exception("Copy-trader init failed; disabled for this run")

    async def _consume_copy_logs(
        self, engine: Any, logs: list[str], outer_signer: str | None = None
    ) -> None:
        """Parse one trade-log batch and dispatch to the copy engine.

        Shared by the copy feed and the slot-ordered buffer drain so both
        paths parse identically. Applies the SAME feed consumers as the rug
        feed (creator-sell exit, own-wallet prompt, migration watcher, rug
        buyer counting) before dispatching the trade to the mirror engine —
        otherwise enabling copy-trading would silently disable those R3
        exit triggers.
        """
        trade = await self._consume_trade_logs(logs, outer_signer)
        if trade is not None:
            await engine.on_trade_event(trade)

    async def _init_rug_trade_feed(self) -> None:
        """P2-EXIT-4: wire listener trade events into the rug buyer counter.

        Used when the copy engine is off (it registers its own callback
        otherwise). Best-effort: listener without the hook just means the
        unique-buyer test cannot run — the drain test still works.
        """
        try:

            async def on_rug_trade_logs(
                logs: list[str],
                outer_signer: str | None = None,
                slot: int | None = None,
            ) -> None:
                # R3-P2-14: when slot ordering is enabled, buffer the feed by
                # slot and release in ascending order — an out-of-order geyser
                # delivery must not let a later fill count a buyer/creator
                # before the earlier one (position state stays consistent).
                if self.slot_ordered_feed_enabled and self._slot_buffer is not None:
                    if slot is not None and self._slot_buffer.push(
                        slot, (logs, outer_signer)
                    ):
                        for buffered_logs, buffered_signer in self._slot_buffer.drain():
                            await self._consume_trade_logs(
                                buffered_logs, buffered_signer
                            )
                        return
                    # Slot missing (non-geyser listener): process immediately.
                await self._consume_trade_logs(logs, outer_signer)

            if hasattr(self.token_listener, "trade_callback"):
                self.token_listener.trade_callback = on_rug_trade_logs
                logger.info("P2-EXIT-4: rug trade feed wired (buyer counting)")
        except Exception:
            logger.exception("Rug trade feed init failed; drain test only")

    async def _consume_trade_logs(
        self, logs: list[str], outer_signer: str | None = None
    ) -> dict | None:
        """Parse one trade-log batch and apply feed consumers (rug feed).

        Shared by the rug feed and the slot-ordered buffer drain: scan the
        PumpSwap migration event, count unique buyers for the rug heuristic,
        flag a creator-sell exit, and prompt an event-driven exit on the
        bot's own wallet. Kept as one method so both the immediate and the
        buffered paths use identical logic. Returns the parsed trade dict
        (None when the logs carry no TradeEvent) so the copy feed can
        dispatch it to the mirror engine after the flags are applied.
        """
        # R3-P2-5: scan logs for the PumpSwap migration event first
        # (the same logs carry both events).
        if self.migrate_watcher is not None:
            migrated_mint = self.migrate_watcher.on_logs(logs)
            if migrated_mint:
                logger.info(
                    f"R3-P2-5: mint {migrated_mint} migrated to "
                    f"PumpSwap (watcher armed)"
                )
                # R4-P3-2: verify the label against a DIRECT pool-state read
                # before it is trusted. Fire-and-forget: the log line alone
                # (legacy behavior) already set migrated=True; the confirm
                # read either upgrades the label to verified or refutes it.
                asyncio.create_task(
                    self._confirm_migration(migrated_mint)
                )
        trade = self.platform_implementations.event_parser.parse_trade_event_from_logs(
            logs, outer_signer=outer_signer
        )
        if not trade:
            return None
        mint_key = str(trade.get("mint", ""))
        trader_key = str(trade.get("trader") or "")
        # R4-P1-6: feed the wash/LPI detector every trade event (bounded
        # per-mint windows; quote_lamports + implied price from the event).
        try:
            tok_amt = float(trade.get("token_amount") or 0)
            sol_amt = float(trade.get("sol_amount") or 0)
            self._wash_lpi.observe_trade(
                mint_key,
                trader_key,
                is_buy=bool(trade.get("is_buy")),
                quote_lamports=int(float(trade.get("quote_amount") or 0)),
                ts=monotonic(),
                token_price=(sol_amt / tok_amt if tok_amt > 0 else None),
            )
        except Exception:
            logger.debug("wash/LPI observe failed (non-fatal)", exc_info=True)
        # R3-P1-4: creator-sell trigger — a sell whose trader matches
        # a registered creator for this mint marks it for exit. The R5-P2-3
        # log-only advisory path needs the SAME observation to build evidence
        # without arming the exit.
        if (self.exit_on_creator_sell or self.post_entry_advisories_enabled) and not trade.get("is_buy"):
            creators = self._creator_sellers.get(mint_key, set())
            if creators and trader_key in creators:
                self._creator_sold_for_exit.add(mint_key)
        if trade.get("is_buy") and mint_key and trader_key:
            # Co-fire and wash/LPI are explicitly bounded cross-launch windows.
            # The rug-buyer set is position/candidate state, so do NOT retain
            # every buyer for every token on the whole subscribed program.
            if self._should_track_mint_buyers(mint_key):
                self._rug_buyers.setdefault(mint_key, set()).add(trader_key)
            self._cofire.observe_buy(mint_key, trader_key)
        # R3-P2-12: own-wallet trade flag — a TradeEvent whose trader
        # is the bot's wallet prompts the monitor for an immediate
        # exit evaluation (off by default = current polling).
        if self.event_driven_exits_enabled and self.wallet is not None:
            if (
                mint_key
                and (
                    mint_key in self._active_exit_mints
                    or mint_key in self._unsold_positions
                )
                and str(trade.get("trader") or "") == str(self.wallet.public_key)
            ):
                self._own_trade_prompt.add(mint_key)
        return trade

    def _should_track_mint_buyers(self, mint: str) -> bool:
        """True only while the mint is a candidate or has an owned position."""
        return (
            mint in getattr(self, "token_timestamps", {})
            or mint in getattr(self, "_buy_reservations", set())
            or mint in getattr(self, "_active_exit_mints", set())
            or mint in getattr(self, "_unsold_positions", {})
        )

    def _clear_mint_tracking_state(self, mint: str) -> None:
        """Drop transient per-mint screening/monitor state once it is no longer live.

        The Geyser trade feed can see transactions for every mint on the
        subscribed program, not just positions owned by this bot. Keep the
        expensive per-wallet buyer map only while a mint is a tracked
        candidate or an active/unsold position.
        """
        for name in (
            "_rug_buyers",
            "_creator_sellers",
            "_rug_entry",
            "_momentum_peak_rate",
            "_momentum_peak_price",
        ):
            state = getattr(self, name, None)
            if isinstance(state, dict):
                state.pop(mint, None)
        for name in ("_creator_sold_for_exit", "_own_trade_prompt"):
            state = getattr(self, name, None)
            if isinstance(state, set):
                state.discard(mint)
        graduated = getattr(self, "_graduated_mints", None)
        if isinstance(graduated, set):
            try:
                graduated.discard(Pubkey.from_string(mint))
            except ValueError:
                pass

    def _holder_gates_on(self) -> bool:
        """R3-P1-5: whether either holder-concentration gate is configured."""
        return (
            self.min_unique_holders > 0
            or self.max_top10_concentration > 0
            or self.max_gini_coefficient > 0
        )

    async def _holder_refresh_loop(self, interval: float = 30.0) -> None:
        """R3-P1-5: background holder-snapshot refresher.

        Pulls `getTokenLargestAccounts` + supply for every mint the detector
        has queued/tracked, publishing immutable snapshots. `_skip_reason`
        reads only the snapshot store — this loop owns the RPC. Best-effort:
        a failed pull leaves the old snapshot (fail-open for missing mints).
        """
        try:
            while True:
                for mint in self._refresh_target_mints():
                    await self._refresh_one_holder_snapshot(mint)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Holder snapshot refresher stopped")
            raise

    def _refresh_target_mints(self) -> list[str]:
        """Mints both background refreshers visit, bounded by the cap.

        Union of processed/traded mints plus whatever the detector queue
        still holds (so a gate sees fresh data before the buy, not only
        after), capped at `max_tracked_tokens`. Shared by the holder and
        metadata refresh loops so their collection logic cannot drift.
        """
        mints = set()
        for key in list(getattr(self, "processed_tokens", set()) or set()) + [
            str(m) for m in getattr(self, "traded_mints", set())
        ]:
            if key:
                mints.add(key)
        try:
            q = getattr(self, "token_queue", None)
            if q is not None and hasattr(q, "_queue"):
                for item in list(q._queue):  # noqa: SLF001
                    mint = getattr(item, "mint", None)
                    if mint:
                        mints.add(str(mint))
        except Exception:
            pass
        return list(mints)[: self.max_tracked_tokens]

    async def _refresh_one_holder_snapshot(self, mint: str) -> None:
        """Pull largest accounts + supply for one mint and store a snapshot."""
        try:
            mint_pk = Pubkey.from_string(mint)
            # Both reads use SolanaClient wrappers so the holder refresher
            # cannot bypass the process-wide rate limiter under live load.
            largest = await self.solana_client.get_token_largest_accounts(mint_pk)
            supply = await self.solana_client.get_token_supply(mint_pk)
            rows = [
                (str(item.address), int(str(item.amount)))
                for item in (largest.value or [])
            ]
            total_raw = int(supply) if supply is not None else 0
            slot = largest.context.slot if largest.context else 0
            from screening.holder_snapshot import compute_snapshot

            self._holder_store.put(compute_snapshot(mint, rows, total_raw, slot=slot))
        except Exception:
            # Fail-open: keep whatever snapshot exists (or none) for this mint.
            logger.debug("Holder snapshot pull failed for %s", mint)

    async def _metadata_refresh_loop(self, interval: float = 30.0) -> None:
        """R3-P2-8: background metadata-extension refresher.

        Reads each tracked mint's SPL metadata account (Metaplex Token
        Metadata PDA), extracts the off-chain URI, fetches the JSON, and
        stores a MetadataSnapshot. `_skip_reason` reads only the store — this
        loop owns the RPC + HTTP. Fail-open: a mint with no snapshot (RPC
        down / non-standard metadata program / fetch failure) is allowed.
        """
        try:
            while True:
                for mint in self._refresh_target_mints():
                    await self._refresh_one_metadata_snapshot(mint)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Metadata refresher stopped")
            raise

    async def _refresh_one_metadata_snapshot(self, mint: str) -> None:
        """Read the metadata account + URI JSON for one mint (fail-open)."""
        try:
            # Reuse the holder gate's stored snapshot when already fresh — the
            # metadata account also changes rarely, but the store key is the
            # same mint, so only pull when we have nothing for it yet.
            existing = self._metadata_store.get(mint)
            if existing is not None and existing.extensions:
                return
            mint_pk = Pubkey.from_string(mint)
            from screening.metadata_socials import (
                METADATA_PROGRAM,
                MetadataSnapshot,
                parse_metadata_json,
            )

            meta_pda, _ = Pubkey.find_program_address(
                [
                    b"metadata",
                    Pubkey.from_string(METADATA_PROGRAM).__bytes__(),
                    mint_pk.__bytes__(),
                ],
                Pubkey.from_string(METADATA_PROGRAM),
            )
            try:
                account = await self.solana_client.get_account_info(meta_pda)
            except ValueError:
                return  # no metadata account -> fail-open (no snapshot)
            data = bytes(getattr(account, "data", b""))
            if not data or len(data) < 1:
                return
            uri = self._extract_metadata_uri(data)
            if not uri:
                return
            raw = await self._fetch_metadata_json(uri)
            snapshot = parse_metadata_json(raw, mint)
            if not snapshot.extensions and not (snapshot.symbol or snapshot.name):
                # Non-standard/empty metadata: store an empty snapshot so the
                # gate can refuse it when enabled (positive evidence of empty).
                self._metadata_store.put(MetadataSnapshot(mint=mint))
                return
            self._metadata_store.put(snapshot)
        except Exception:
            logger.debug("Metadata snapshot pull failed for %s", mint)

    @staticmethod
    def _extract_metadata_uri(data: bytes) -> str:
        """Pull the URI out of a Token Metadata account body (borsh strings).

        Layout: key(1) + update_authority(32) + mint(32), then three borsh
        length-prefixed utf8 strings: name, symbol, uri.
        """
        try:
            off = 1 + 32 + 32
            for _ in range(3):
                if off + 4 > len(data):
                    return ""
                (ln,) = __import__("struct").unpack_from("<I", data, off)
                off += 4
                if ln > 200 or off + ln > len(data):
                    return ""
                s = data[off : off + ln].decode("utf-8", errors="replace")
                off += ln
                if _ == 2:
                    return s
            return ""
        except Exception:
            return ""

    async def _fetch_metadata_json(self, uri: str) -> str:
        """GET the metadata URI (off hot path, short timeout, fail-open).

        C10.2 (SSRF): the URI is attacker-controlled — it is set by the
        token creator in the on-chain metadata account — so it is validated
        against an allowlist before any connection is attempted:

        * schemes: https/http only (ipfs:// and ar:// mapped to their public
          gateways), everything else refused before DNS resolution;
        * hosts: the name is resolved and every address is checked against
          loopback / link-local / private / reserved ranges (blocks
          `169.254.169.254` cloud metadata, localhost, CGNAT, RFC1918);
        * body: streamed with an explicit 64 KiB cap instead of buffering
          the whole response.

        Fail-open: any validation failure or fetch error returns "" (the
        caller treats a missing snapshot as gate-pass when the gate is off).
        """
        target = _safe_metadata_uri(uri)
        if target is None:
            logger.debug(f"Metadata URI blocked by SSRF allowlist: {uri!r}")
            return ""
        try:
            timeout = aiohttp.ClientTimeout(total=4.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    target, headers={"User-Agent": "Mozilla/5.0"}
                ) as resp:
                    if resp.status != 200:
                        return ""
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(8192):
                        total += len(chunk)
                        if total > 65_536:
                            return b"".join(chunks).decode(
                                "utf-8", errors="replace"
                            )[:65_536]
                        chunks.append(chunk)
                    return b"".join(chunks).decode("utf-8", errors="replace")
        except Exception:
            return ""

    async def _mirror_buy(
        self, token_info: TokenInfo, target: Any, size_sol: float
    ) -> bool:
        """Mirror a leader buy through a fresh buyer sized per target (T3.2).

        Shadow-aware: when shadow_mode is on, the mirror is simulated at the
        current price and recorded in the research ledger (no transaction is
        sent). Real mode keeps the dedicated-buyer path.

        R3-P2-10: mirrors respect the session risk guards (loss limit and
        budget gate new buys) and record their spend toward the budget
        without opening a tracked position (the exit machinery never closes
        a mirror). R3-P2-13: a mirror buy never overlaps an in-flight sell.
        """
        # R3-P2-10: risk gate — a copy buy spends the wallet just like a
        # snipe buy; the loss limit and budget apply to mirrors too.
        if not self.risk_tracker.buy_allowed:
            reason = self.risk_tracker.blocked_reason or "risk guard blocked"
            logger.warning(
                f"Copy-trader: skipping mirror on {token_info.symbol} - {reason}"
            )
            return False
        try:
            if self.shadow_mode:  # R5-P0-4: mode flag, not shadow_capture presence
                price = await self._read_price(token_info)
                logger.info(
                    f"[SHADOW] would mirror {target.alias or target.wallet[:8]} "
                    f"buy of {token_info.symbol} for {size_sol:.6f} SOL "
                    f"at {price:.3e} SOL/token (no tx sent)"
                )
                race_fields = await self._race_kpi_fields(token_info)
                self.shadow_capture.sim_open(
                    mint=str(token_info.mint),
                    symbol=token_info.symbol,
                    platform=token_info.platform.value,
                    price=price,
                    amount_tokens=(size_sol / price if price > 0 else 0.0),
                    quote_spent=size_sol,
                    pool_snapshot=await self._pool_snapshot(token_info),
                    mirror_of=(target.alias or str(target.wallet)[:8]),
                    **race_fields,
                )
                self.risk_tracker.record_spend(size_sol)
                return True

            # The sniper's buyer is sized from config; a copy target carries
            # its own size (fixed or percent-of-leader), so build a dedicated
            # buyer for the mirror.
            from trading.platform_aware import PlatformAwareBuyer

            buyer = PlatformAwareBuyer(
                self.solana_client,
                self.wallet,
                self.priority_fee_manager,
                size_sol,
                slippage=(target.slippage_bps or 500) / 10_000,
                max_retries=self.max_retries,
                trust_create_event=False,
                executor=self.executor,
            )
            # R3-P2-13: a mirror buy never overlaps an in-flight sell (WSOL
            # wrap/unwrap balance race). No-op without contention.
            async with self._trade_serialization_lock:
                result = await buyer.execute(token_info)
            if result and result.success:
                self.risk_tracker.record_spend(size_sol)
            return bool(result and result.success)
        except Exception:
            logger.exception("Copy-trader mirror buy raised")
            return False

    async def _mirror_sell(
        self,
        token_info: TokenInfo,
        target: Any,
        size_sol: float,
        reason: str,
        entry: dict | None = None,  # C5.3: entry metadata from the engine
    ) -> bool:
        """Close a mirrored position when the leader sells (R5-P0-4).

        Shadow-aware, matching _mirror_buy: shadow mode records a simulated
        close in the research ledger (no transaction); live mode sells through
        the platform seller. A failed live sell keeps the position tracked by
        the engine (it re-arms on the next leader-sell event).
        """
        try:
            if self.shadow_mode:
                price = await self._read_price(token_info)
                entry_price = (
                    float(entry.get("entry_price", 0.0) or 0.0)
                    if entry
                    else 0.0
                )
                entry_ts = float(entry.get("entry_time", 0.0) or 0.0) if entry else 0.0
                # C5.5: real amount/pnl/hold instead of zeroed shadow rows.
                # If we restored an entry_price with no live price (stale
                # position), price <= 0 → fall back to the entry price so the
                # pnl is 0 rather than nonsense.
                fill_price = price if price and price > 0 else entry_price or 0.0
                amount_tokens = size_sol / fill_price if fill_price > 0 else 0.0
                pnl_quote = 0.0
                pnl_pct = 0.0
                hold_seconds = 0.0
                if entry_price > 0 and fill_price > 0 and amount_tokens > 0:
                    pnl_quote = (fill_price - entry_price) * amount_tokens
                    pnl_pct = (fill_price / entry_price - 1.0) * 100.0
                if entry_ts > 0:
                    hold_seconds = max(0.0, time.time() - entry_ts)
                logger.info(
                    f"[SHADOW] would mirror-sell {target.alias or target.wallet[:8]} "
                    f"{token_info.symbol} for {size_sol:.6f} SOL "
                    f"at {fill_price:.3e} SOL/token (no tx sent, {reason})"
                )
                if self.shadow_capture is not None:
                    copy_open_ts = int(entry_ts) if entry_ts > 0 else 0
                    self.shadow_capture.sim_close(
                        mint=str(token_info.mint),
                        symbol=token_info.symbol,
                        platform=token_info.platform.value,
                        price=fill_price,
                        amount_tokens=amount_tokens,
                        quote_received=size_sol,
                        exit_reason=f"mirror_{reason}",
                        pnl_quote=pnl_quote,
                        pnl_pct=pnl_pct,
                        hold_seconds=hold_seconds,
                        # C5.5: link the shadow close to the copy position.
                        position_id=f"copy:{target.wallet}:{token_info.mint}:{copy_open_ts}",
                    )
                return True
            # Live path: real sell through the platform-aware seller.
            from trading.platform_aware import PlatformAwareSeller

            seller = PlatformAwareSeller(
                self.solana_client,
                self.wallet,
                self.priority_fee_manager,
                slippage=self.sell_slippage,
                max_retries=self.max_retries,
                executor=self.executor,
            )
            # C5.3: the seller now REQUIRES token_amount + token_price —
            # calling execute(token_info) alone is a TypeError no-op. Derive
            # the amount from the mirrored SOL size at the current price.
            price = await self._read_price(token_info)
            if not price or price <= 0:
                logger.error(
                    f"Copy-trader mirror sell: cannot read price for "
                    f"{token_info.mint}; refusing (fail-closed)"
                )
                return False
            token_amount = size_sol / price
            async with self._trade_serialization_lock:
                result = await seller.execute(token_info, token_amount, price)
            return bool(result and result.success)
        except Exception:
            logger.exception("Copy-trader mirror sell raised")
            return False

    async def _race_kpi_fields(self, token_info: TokenInfo) -> dict[str, Any]:
        """R4-P0-4/1: landed-slot + reordering-slippage fields for the
        shadow ledger.

        ``landed_slot_offset`` is (decision slot - create slot), the bot's
        reaction latency in slots — 0-2 is the known-good benchmark (top
        sniper lands 79.6% in the deploy slot). ``reordering_bps`` is 0 by
        construction for shadow fills (they fill at the observed price, no
        adversarial reordering possible); the field exists so live fills
        populate it. One ``get_slot`` RPC, shadow mode only. Also records
        the UTC entry hour (R4-P1-5 research logging, no gate) so the
        ledger can be stratified by hour without parsing the ISO timestamp.
        """
        fields: dict[str, Any] = {
            "create_slot": getattr(token_info, "create_slot", None),
            "entry_tx_idx_in_block": None,  # live-only (tx position)
            "reordering_bps": 0,  # shadow fills simulate at observed price
            "entry_hour_utc": datetime.now(UTC).hour,  # R4-P1-5
        }
        if not self.shadow_mode:
            return fields
        decision_slot: int | None = None
        try:
            decision_slot = await self.solana_client.get_slot()
        except Exception:
            decision_slot = None
        fields["decision_slot"] = decision_slot
        create_slot = getattr(token_info, "create_slot", None)
        if decision_slot is not None and create_slot is not None:
            fields["landed_slot_offset"] = decision_slot - create_slot
        else:
            fields["landed_slot_offset"] = None
        return fields

    async def _backfill_gap(self) -> None:
        """T2.6: replay missed transactions from RPC when enabled.

        The crawler pages getSignaturesForAddress on the pump.fun program,
        parses each replayed transaction's logs with the same parser the live
        stream uses, and routes decoded creates through the normal token
        pipeline (dedupe + freshness + gates) so a geyser gap cannot silently
        drop launches or exit events.

        Disabled by default; off is the safe baseline.
        """
        if not self.backfill_enabled:
            return
        try:
            crawler = TransactionCrawler(
                self.solana_client,
                self.platform_implementations.event_parser.get_program_id(),
                fetch_concurrency=self.backfill_fetch_concurrency,
                page_size=self.backfill_page_size,
                max_retries=3,
                checkpoint_path=(
                    Path(self.backfill_checkpoint) if self.backfill_checkpoint else None
                ),
            )

            async def handle_replay(tx: dict, sig) -> None:  # noqa: ANN001
                logs = (
                    tx.get("meta", {}).get("logMessages", [])
                    if isinstance(tx, dict)
                    else []
                )
                parsed = self.platform_implementations.event_parser.parse_token_creation_from_logs(
                    logs, str(sig)
                )
                if not parsed:
                    return
                token_info = parsed[0] if isinstance(parsed, list) else parsed
                token_key = str(token_info.mint)
                if token_key in self.processed_tokens:
                    return
                if self._skip_reason(token_info):
                    return
                self.processed_tokens.add(token_key)
                self.token_timestamps[token_key] = monotonic()
                self._queued_mints.add(token_key)  # C3.3 enqueue dedupe
                await self.token_queue.put(token_info)
                logger.info(
                    f"Backfill: replayed {token_info.symbol} ({token_info.mint})"
                )

            await crawler.crawl(
                parser=self.platform_implementations.event_parser,
                handle_transaction=handle_replay,
                max_pages=self.backfill_max_pages,
            )
        except Exception:
            logger.exception("Backfill failed; continuing without catch-up")

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
        # Shadow mode must never mutate the real wallet, including a
        # post-session cleanup configured independently of the exit path.
        if self.traded_mints and not getattr(self, "shadow_mode", False):
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
        """Queue a token for processing if not already processed/queued.

        C3.3: dedupes at ENQUEUE time (a duplicate detect of a mint already
        sitting in the queue is dropped here, before it ever reaches the
        processor) and enforces a hard queue bound — when the queue is full,
        the OLDEST queued token is evicted to make room for the newest launch
        (the freshness gate in the processor would reject the evicted one
        anyway once it was behind a backed-up queue).
        """
        token_key = str(token_info.mint)

        if token_key in self.processed_tokens:
            logger.debug(f"Token {token_info.symbol} already processed. Skipping...")
            return
        if token_key in self._queued_mints:
            logger.debug(
                f"Token {token_info.symbol} already queued. Skipping... (C3.3 enqueue dedupe)"
            )
            return

        # Bounded queue: evict the OLDEST queued token when full, so the
        # newest launch is never dropped by a flood backlog.
        if self.token_queue.full():
            try:
                evicted = self.token_queue.get_nowait()
                self.token_queue.task_done()
                evicted_key = str(evicted.mint)
                self._queued_mints.discard(evicted_key)
                metrics.incr("tokens.queue_evictions")
                logger.info(
                    f"Token queue full ({self.token_queue_maxsize}); "
                    f"evicted oldest queued {evicted.symbol} ({evicted_key[:8]}...) "
                    f"to make room for {token_info.symbol}"
                )
            except asyncio.QueueEmpty:
                pass  # raced with the processor; just enqueue below

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
            # A queued candidate that aged out is no longer used by screening.
            # Preserve state for an in-flight buy, open position, or unsold tail.
            if (
                oldest_key not in self._buy_reservations
                and oldest_key not in self._active_exit_mints
                and oldest_key not in self._unsold_positions
            ):
                self._clear_mint_tracking_state(oldest_key)
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

        self._queued_mints.add(token_key)
        await self.token_queue.put(token_info)
        logger.info(
            f"Queued new token: {token_info.symbol} ({token_info.mint}) on {token_info.platform.value}"
        )

    def _claim_queue_token(
        self, token_info: TokenInfo, max_age: float, prefix: str = ""
    ) -> float | None:
        """Claim a queued token and apply the shared freshness gate.

        Returns the token age when fresh, or None after recording a stale skip.
        Queue ownership remains with the caller so each get/task_done pair
        keeps its existing cancellation semantics.
        """
        token_key = str(token_info.mint)
        self._queued_mints.discard(token_key)  # C3.3 enqueue dedupe
        token_age = monotonic() - self.token_timestamps.get(token_key, monotonic())
        if max_age > 0 and token_age > max_age:
            logger.info(
                f"{prefix}Skipping token {token_info.symbol} - too old "
                f"({token_age:.1f}s queue wait > {max_age}s)"
            )
            metrics.incr("tokens.skipped_stale")
            return None
        self.processed_tokens.add(token_key)
        return token_age

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
                metrics.set_gauge("token_queue.depth", self.token_queue.qsize())
                metrics.incr("tokens.dequeued")

                token_age = self._claim_queue_token(token_info, self.max_token_age)
                launch_age = self.token_launch_timestamps.get(token_key)
                age_note = (
                    f", launch_ts={launch_age:.3f}" if launch_age is not None else ""
                )

                if token_age is None:
                    continue

                logger.info(
                    f"Processing fresh token: {token_info.symbol} (age: {token_age:.1f}s)"
                )
                started = monotonic()
                try:
                    await self._handle_token(token_info)
                finally:
                    metrics.record("token.processing", monotonic() - started)

            except asyncio.CancelledError:
                logger.info("Token queue processor was cancelled")
                break
            except Exception:
                logger.exception("Error in token queue processor")
                metrics.incr("tokens.queue_errors")
            finally:
                # The stale-token `continue` above took an item, so it still
                # needs this; a cancellation parked on `get()` did not.
                if took_item:
                    self.token_queue.task_done()

    async def _metrics_logger(self) -> None:
        """Log runtime metrics every 60s (T1.7)."""
        while True:
            await asyncio.sleep(60)
            snap = metrics.snapshot()
            hist = snap["hist"].get("token.processing", {})
            logger.info(
                "metrics: received=%d detected=%d processed=%d queue_depth=%d "
                "proc_avg=%ss proc_p95=%ss errors=%d stale=%d "
                "rpc_requests=%d rpc_429=%d rpc_p95=%ss "
                "geyser_reconnects=%d grpc_exhausted=%d",
                snap["counters"].get("geyser.updates_received", 0),
                snap["counters"].get("geyser.tokens_detected", 0),
                snap["counters"].get("tokens.dequeued", 0),
                snap["gauges"].get("token_queue.depth", 0),
                hist.get("avg", 0.0),
                hist.get("p95", 0.0),
                snap["counters"].get("tokens.queue_errors", 0),
                snap["counters"].get("tokens.skipped_stale", 0),
                snap["counters"].get("rpc.requests", 0),
                snap["counters"].get("rpc.http_429", 0),
                snap["hist"].get("rpc.latency", {}).get("p95", 0.0),
                snap["counters"].get("geyser.reconnects", 0),
                snap["counters"].get("geyser.grpc_resource_exhausted", 0),
            )

    async def _drain_token_queue(self, max_age: float) -> None:
        """Process whatever is still fresh in the queue, then stop (T1.7).

        Cancelling the queue processor drops in-flight launches; this drains
        them first, honoring the same freshness gate the steady-state loop
        applies. A stale item is `task_done()`d and skipped; a fresh one goes
        through the normal handler so positions still get exits on shutdown.
        """
        while not self.token_queue.empty():
            token_info = await self.token_queue.get()
            try:
                token_age = self._claim_queue_token(
                    token_info, max_age, prefix="Drain: "
                )
                if token_age is None:
                    continue
                logger.info(
                    f"Drain: processing fresh token {token_info.symbol} "
                    f"(age: {token_age:.1f}s)"
                )
                await self._handle_token(token_info)
            finally:
                self.token_queue.task_done()


    async def _fee_estimate_fields(self) -> dict:
        """R3-P2-4: fee ground-truth fields for research capture.

        Estimates what the configured send path would pay (priority fee +
        jito tip) and returns capture fields. Best-effort: any failure yields
        empty fields (fee_source unknown), never raises into the trade path.
        """
        try:
            from research.fee_capture import estimate_send_fee

            priority_fee = await self.priority_fee_manager.calculate_priority_fee()
            tip_lamports = 0
            tip_account = None
            executor = getattr(self, "executor", None)
            if executor is not None:
                tip = getattr(executor, "tip_lamports", 0) or 0
                tip_account = getattr(executor, "_tip_account", None)
                if tip_account is not None:
                    tip_account = str(tip_account)
                tip_lamports = int(tip)
            est = estimate_send_fee(
                priority_fee,
                tip_lamports=tip_lamports,
                tip_account_b58=tip_account,
                compute_units=100_000,
            )
            return est.to_capture_fields()
        except Exception:
            return {"fee_source": "unknown"}

    def _capture_detect(
        self, token_info: TokenInfo, skip_reason: str | None = None
    ) -> None:
        """Record one detector event (skip reason included) for research."""
        if self.shadow_capture is None:
            return
        # R4-P0-3: every detected launch is a contention-clock tick (the
        # flood rate feeds the fee-racing arm decision before a buy).
        if self.contention_enabled:
            self._contention_creates.append(monotonic())
        try:
            self.shadow_capture.detect(
                mint=str(token_info.mint),
                symbol=token_info.symbol,
                platform=token_info.platform.value,
                quote_mint=str(normalize_quote_mint(token_info.quote_mint)),
                creator=(str(token_info.creator) if token_info.creator else None),
                transfer_fee_bps=token_info.transfer_fee_bps,
                listener=getattr(self.token_listener, "listener_type", ""),
                skip_reason=skip_reason,
                is_mayhem_mode=bool(getattr(token_info, "is_mayhem_mode", False)),
                is_holder_reward=bool(getattr(token_info, "is_holder_reward", False)),
                is_cashback_coin=bool(getattr(token_info, "is_cashback_coin", False)),
            )
        except Exception:
            logger.exception("Shadow detect capture failed")

    async def _sol_price_usd(self) -> float | None:
        """Current SOL/USD price for research capture, or None on failure.

        Cached for 60s so repeated snapshots cost at most one HTTP request a
        minute. Best-effort: research data must never raise into the trade
        path, so any failure yields None and the USD field is simply omitted.
        """
        cached = getattr(self, "_sol_price_cache", None)
        now = monotonic()
        if cached is not None and (now - cached[1]) < _SOL_PRICE_TTL_SECONDS:
            return cached[0]
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_SOL_PRICE_TIMEOUT_SECONDS)
            ) as session:
                async with session.get(
                    "https://api.coingecko.com/api/v3/simple/price"
                    "?ids=solana&vs_currencies=usd"
                ) as resp:
                    if resp.status != _HTTP_OK:
                        return None
                    data = await resp.json()
                    price = float(data["solana"]["usd"])
            self._sol_price_cache = (price, now)
            return price
        except Exception:
            return None

    async def _pool_snapshot(self, token_info: TokenInfo) -> dict:
        """Real on-chain pool state at this moment, for research capture.

        Returns the curve's decoded state (reserves, graduated, mayhem,
        cashback, fees, price, MC) or {} on any failure — research data is
        best-effort and must never raise into the trade path.
        """
        if self.shadow_capture is None:
            return {}
        try:
            implementations = get_platform_implementations(
                token_info.platform, self.solana_client
            )
            pool_address = (
                token_info.bonding_curve
                or token_info.pool_state
                or implementations.address_provider.derive_pool_address(token_info.mint)
            )
            state = await implementations.curve_manager.get_pool_state(pool_address)
            if not state:
                return {}
            keep = {
                "virtual_quote_reserves",
                "virtual_token_reserves",
                "real_quote_reserves",
                "real_token_reserves",
                "graduated",
                "is_mayhem_mode",
                "is_cashback_coin",
                "fee_fraction",
                "transfer_fee_bps",
                "price_per_token",
                "token_total_supply",
            }
            snapshot = {k: state.get(k) for k in keep if k in state}

            # Market cap in the quote asset (whole units) and USD. SOL-paired
            # curves only for USD — a USDC quote is already dollars.
            supply = snapshot.get("token_total_supply")
            quote_units = 10 ** (await self._quote_decimals_for(token_info))
            if supply and quote_units:
                vq = snapshot.get("virtual_quote_reserves", 0) or 0
                vt = snapshot.get("virtual_token_reserves", 0) or 0
                if vt > 0:
                    mcap_quote = (vq / quote_units) * (supply / vt)
                    snapshot["market_cap_quote"] = mcap_quote
                    if token_info.quote_mint is not None and str(
                        token_info.quote_mint
                    ) in (
                        "So11111111111111111111111111111111111111112",
                        "11111111111111111111111111111111",
                    ):
                        sol_usd = await self._sol_price_usd()
                        if sol_usd:
                            snapshot["market_cap_usd"] = mcap_quote * sol_usd
                    else:
                        snapshot["market_cap_usd"] = mcap_quote
            # Entry-MC band for strategy analysis. Every pump.fun launch is
            # born at the ~28-SOL floor, so raw MC carries no selection signal
            # at detection — the band makes the cohort explicit in the ledger
            # so backtests can prove (or kill) the "floor band = coin flip"
            # hypothesis directly.
            mc_usd = snapshot.get("market_cap_usd")
            if mc_usd is not None:
                if mc_usd < 3000:
                    snapshot["mc_band"] = "floor_lt_3k"
                elif mc_usd < 5000:
                    snapshot["mc_band"] = "3k_5k"
                elif mc_usd < 10000:
                    snapshot["mc_band"] = "5k_10k"
                else:
                    snapshot["mc_band"] = "gt_10k"
            return snapshot
        except Exception:
            logger.exception("Pool snapshot capture failed")
            return {}

    async def _quote_decimals_for(self, token_info: TokenInfo) -> int:
        """Quote mint decimals for the snapshot, defaulting to 9 (SOL).

        Research-only: an unresolved non-SOL quote mint reports an approximate
        market cap rather than failing the capture.
        """
        try:
            mint = token_info.quote_mint
            if mint is None:
                return 9
            return quote_decimals(resolve_quote_mint(mint))
        except Exception:
            return 9

    async def _update_contention_arms(self, quote_sol: float) -> None:
        """R4-P0-3: arm/disarm fee-racing contention for the next send.

        Launch flood = create-event rate over the trailing window above
        contention_creates_per_min (the tierzero landing-collapse scenario:
        plain fees land at 20-40% under launch competition). When armed,
        BOTH fee managers escalate their bid by contention_multiplier and
        tighten the cap to min(hard_cap, max_fee_fraction_of_position of
        the quote) so a fee surge can never eat the position.

        Off by default: contention_enabled False = today's behavior.
        """
        if not self.contention_enabled:
            return
        now = monotonic()
        cutoff = now - self.contention_window_s
        self._contention_creates[:] = [
            t for t in self._contention_creates if t >= cutoff
        ]
        rate = len(self._contention_creates) * (
            60.0 / max(self.contention_window_s, 1.0)
        )
        armed = rate >= self.contention_creates_per_min
        cap = None
        if armed and quote_sol > 0:
            # Microlamports ceiling so the fee stays a small fraction of the
            # position: price_microlamports * CU_used / 1e15 <= quote *
            # fraction  => price <= quote * fraction * 1e15 / CU_used.
            cu = self.compute_units.get("buy", 100_000) if self.compute_units else 100_000
            cap = int(quote_sol * self.max_fee_fraction_of_position * 1e15 / cu)
        self.priority_fee_manager.set_contention(armed, cap=cap)
        self.sell_priority_fee_manager.set_contention(armed, cap=cap)
        # R4-P0-3 Jito leg: escalate the bundle tip only up to the same
        # fraction of the position (tip_lamports * scale <= quote * fraction
        # * 1e9). Off (1.0) until armed.
        jito_exec = getattr(self, "_jito_executor", None)
        scale = 1.0
        if jito_exec is not None and hasattr(jito_exec, "set_contention_tip_scale"):
            base = float(getattr(jito_exec, "tip_lamports", 0) or 0)
            if armed and base > 0 and quote_sol > 0:
                max_tip = quote_sol * self.max_fee_fraction_of_position * 1e9
                scale = min(
                    self.contention_multiplier,
                    max(1.0, max_tip / base),
                )
            jito_exec.set_contention_tip_scale(scale)
        if armed:
            logger.warning(
                f"Launch flood detected: {rate:.1f} creates/min >= "
                f"{self.contention_creates_per_min} — contention fees armed "
                f"(multiplier {self.contention_multiplier}x, fee cap {cap}, "
                f"jito tip x{scale:.2f})"
            )

    async def _fragility_buy_scale(self, token_info: TokenInfo) -> float:
        """R4-P3-1: ME2F size multiplier for one candidate.

        The paper's action rule is two-signal: "VDS and SAS both high ->
        tighten risk". Volatility dynamics here is the max of the observed
        price-path volatility and the holder-distribution whale score
        (concentrated holders = fragile structure); sentiment amplification
        is the observed trade rate. Unknown dims fail open (no scale).
        Off by default (scale_min 1.0 = flat sizing).
        """
        if self.fragility_scale_min >= 1.0:
            return 1.0
        from screening.fragility import (
            fragility_scale,
            sentiment_score,
            volatility_score,
            whale_score,
        )

        snap = self._holder_store.get(str(token_info.mint))
        whale = whale_score(
            snap.gini if snap else None,
            snap.top10_fraction if snap else None,
        )
        prices = self._wash_lpi.price_path(str(token_info.mint))
        vds_path = volatility_score(prices)
        # Whale dominance acts as structural fragility: a concentrated
        # holder set is a fragile structure even before the price swings.
        vds = (
            max(vds_path, whale)
            if vds_path is not None and whale is not None
            else (vds_path or whale)
        )
        rate = self._wash_lpi.event_rate_per_min(str(token_info.mint))
        sas = sentiment_score([rate] if rate is not None else None)
        return fragility_scale(
            vds,
            sas,
            scale_min=self.fragility_scale_min,
            vds_threshold=self.fragility_whale_threshold,
            sas_threshold=self.fragility_rate_threshold,
        )

    async def _handle_token(self, token_info: TokenInfo) -> None:
        """Handle a new token creation event."""
        try:
            # Shadow research capture: record every token the detector hands
            # over — the raw opportunity set — with the reason a filter
            # refused it (if any), so backtests see what was thrown away.
            self._capture_detect(token_info)

            # Validate that token is for our platform
            if token_info.platform != self.platform:
                logger.warning(
                    f"Token platform mismatch: expected {self.platform.value}, got {token_info.platform.value}"
                )
                return

            skip_reason = self._skip_reason(token_info)
            if skip_reason:
                logger.info(f"Skipping {token_info.symbol} - {skip_reason}")
                self._capture_detect(token_info, skip_reason)
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
                    self._capture_detect(token_info, curve_gate_reason)
                    return

                # T2.4: confirmation window. A single gate pass can be a
                # transient RPC blip; require consecutive_match_count
                # consecutive passes (1 = current behavior) at
                # gate_recheck_interval apart before committing the buy.
                if self.consecutive_match_count > 1:
                    for recheck in range(1, self.consecutive_match_count):
                        await asyncio.sleep(self.gate_recheck_interval)
                        curve_gate_reason = await self._curve_gate_reason(token_info)
                        if curve_gate_reason:
                            logger.info(
                                f"Skipping {token_info.symbol} - "
                                f"{curve_gate_reason} (confirmation recheck "
                                f"{recheck}/{self.consecutive_match_count - 1})"
                            )
                            return
                    logger.info(
                        f"Gate confirmation ok for {token_info.symbol} "
                        f"({self.consecutive_match_count} consecutive passes)"
                    )

            # R5-P0-2: pre-buy fresh re-evaluation of the data-dependent
            # gates. The audit's P0-2: wash/cofire/holder/metadata were only
            # checked at detection (pre-wait); the curve gate was the only
            # thing re-run before buy. Re-run the stateless dynamic checks
            # now with the post-wait snapshot so a coin that passed on stale
            # (or empty) data is refused before submission. Stateless: the
            # creator-cadence side effect lives only in _skip_reason().
            prebuy_reason = self._dynamic_screening_reason(token_info)
            if prebuy_reason:
                logger.info(
                    f"Skipping {token_info.symbol} - {prebuy_reason} "
                    f"(pre-buy recheck)"
                )
                self._capture_detect(token_info, prebuy_reason)
                return

            # Buy token
            # R3-P2-10: session risk guards — the loss limit blocks NEW buys
            # while sells still run; never traps money in a falling position.
            if not self.risk_tracker.buy_allowed:
                reason = self.risk_tracker.blocked_reason or "risk guard blocked"
                logger.warning(f"Skipping {token_info.symbol} - {reason}")
                self._capture_detect(token_info, reason)
                return
            # C3.4: reserve the mint BEFORE executing the buy. A second
            # detect of the same mint racing the queue processor must not
            # start a duplicate order while this one is in flight. Released
            # in the finally below (failure, position setup, or exception).
            mint_str = str(token_info.mint)
            if (
                mint_str in self._buy_reservations
                or mint_str in self._active_exit_mints
            ):
                logger.warning(
                    f"Skipping {token_info.symbol} - buy/position already "
                    f"in flight for this mint"
                )
                self._capture_detect(token_info, "position_already_open")
                return
            self._buy_reservations.add(mint_str)
            try:
                logger.info(
                    f"Buying {self.quote_amounts[token_quote_mint]:.6f} of quote "
                    f"{token_quote_mint} worth of {token_info.symbol} "
                    f"on {token_info.platform.value}..."
                )
                # R4-P3-1: ME2F fragility size multiplier (1.0 = off). Applied
                # to the quote spent in shadow and to a per-trade scaled buyer
                # in real mode (the shared buyer keeps the configured size).
                frag_scale = await self._fragility_buy_scale(token_info)
                eff_quote = self.quote_amounts[token_quote_mint] * frag_scale
                # R4-P0-3: arm contention racing BEFORE sending (off default).
                await self._update_contention_arms(
                    float(self.quote_amounts.get(token_quote_mint, 0.0))
                )
                if frag_scale < 1.0:
                    logger.info(
                        f"[FRAGILITY] {token_info.symbol} scaled to "
                        f"{frag_scale:.2f}x ({eff_quote:.6f} "
                        f"quote vs {self.quote_amounts[token_quote_mint]:.6f})"
                    )
                if self.shadow_mode:
                    # Shadow/paper mode (#8): simulate the fill at the current
                    # curve price (like the normal path would price it) and run
                    # the exit machinery against the simulated position. No
                    # transaction is sent and nothing is logged as spent.
                    sim_price = await self._read_price(token_info)
                    sim_quote = eff_quote
                    sim_amount = sim_quote / sim_price if sim_price > 0 else 0.0
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
                    if self.shadow_capture is not None:
                        fee_fields = await self._fee_estimate_fields()
                        race_fields = await self._race_kpi_fields(token_info)
                        # One pool-state read shared by both shadow writers
                        # (the strategies ledger is beside sim_open, so a single
                        # snapshot avoids a duplicate get_pool_state RPC per
                        # shadow buy on the entry hot path).
                        snapshot = await self._pool_snapshot(token_info)
                        self.shadow_capture.sim_open(
                            mint=str(token_info.mint),
                            symbol=token_info.symbol,
                            platform=token_info.platform.value,
                            price=sim_price,
                            amount_tokens=sim_amount,
                            quote_spent=sim_quote,
                            pool_snapshot=snapshot,
                            **fee_fields,
                            **race_fields,
                        )
                        # R4-P1-7: register this position with the strategies
                        # ledger so the same price path is evaluated against
                        # every exit strategy in parallel (shadow only).
                        if self.strategies_ledger is not None:
                            self.strategies_ledger.open_position(
                                mint=str(token_info.mint),
                                symbol=token_info.symbol,
                                platform=token_info.platform.value,
                                entry_price=sim_price,
                                amount_tokens=sim_amount,
                                quote_spent=sim_quote,
                                pool_snapshot=snapshot,
                                **fee_fields,
                                **race_fields,
                            )
                else:
                    # R3-P2-13: a buy never overlaps an in-flight sell (WSOL
                    # wrap/unwrap balance race). No-op without contention.
                    async with self._trade_serialization_lock:
                        if frag_scale < 1.0:
                            # R4-P3-1: a fragile coin gets a per-trade SCALED
                            # buyer (same pattern as _mirror_buy); the shared
                            # buyer keeps the configured size for stable coins.
                            from trading.platform_aware import PlatformAwareBuyer

                            scaled_buyer = PlatformAwareBuyer(
                                self.solana_client,
                                self.wallet,
                                self.priority_fee_manager,
                                self.buy_amount * frag_scale,
                                self.buy_slippage,
                                self.max_retries,
                                self.extreme_fast_token_amount,
                                self.extreme_fast_mode,
                                compute_units=self.compute_units,
                                quote_amounts={
                                    q: v * frag_scale for q, v in self.quote_amounts.items()
                                },
                                curve_refresh_budget=2.0,
                                trust_create_event=True,
                                executor=self.executor,
                            )
                            buy_result: TradeResult = await scaled_buyer.execute(token_info)
                        else:
                            buy_result: TradeResult = await self.buyer.execute(token_info)

                if buy_result.success:
                    # R3-P2-10: track the buy for concurrency/budget accounting
                    # using the EFFECTIVE quote spent (fragility-scaled).
                    self.risk_tracker.record_buy(float(eff_quote))
                    # Live research capture: record the real fill using the
                    # same schema as shadow sim_open, tagged for calibration.
                    if not self.shadow_mode and self.shadow_capture is not None:
                        fee_fields = await self._fee_estimate_fields()
                        race_fields = await self._race_kpi_fields(token_info)
                        snapshot = await self._pool_snapshot(token_info)
                        self.shadow_capture.sim_open(
                            mint=str(token_info.mint),
                            symbol=token_info.symbol,
                            platform=token_info.platform.value,
                            price=buy_result.price,
                            amount_tokens=buy_result.amount,
                            quote_spent=eff_quote,
                            pool_snapshot=snapshot,
                            mode="live",
                            tx_signature=buy_result.tx_signature,
                            **fee_fields,
                            **race_fields,
                        )
                        if self.strategies_ledger is not None:
                            self.strategies_ledger.open_position(
                                mint=str(token_info.mint),
                                symbol=token_info.symbol,
                                platform=token_info.platform.value,
                                entry_price=buy_result.price,
                                amount_tokens=buy_result.amount,
                                quote_spent=eff_quote,
                                pool_snapshot=snapshot,
                                mode="live",
                                tx_signature=buy_result.tx_signature,
                                **fee_fields,
                                **race_fields,
                            )
                    await self._handle_successful_buy(token_info, buy_result)
                else:
                    await self._handle_failed_buy(token_info, buy_result)

                # Only wait for next token in yolo mode
                if self.yolo_mode:
                    logger.info(
                        f"YOLO mode enabled. Waiting {self.wait_time_before_new_token} seconds before looking for next token..."
                    )
                    await asyncio.sleep(self.wait_time_before_new_token)
            finally:
                # C3.4: release the pre-buy reservation. Failure, position
                # setup, and exceptions all land here; an exception cannot
                # strand a permanently-reserved mint (which would silently
                # exclude every later detect of the same launch). The
                # tp_sl path separately holds _active_exit_mints for the
                # open position's life, so the in-flight window and the
                # open-position window are both guarded.
                self._buy_reservations.discard(mint_str)

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
                if buy_result.price
                and buy_result.amount
                and (normalize_quote_mint(token_info.quote_mint) == WSOL_MINT)
                else None
            ),
        )
        self.traded_mints.add(token_info.mint)
        # P2-EXIT-4: capture entry slot + pool depth right after the buy so
        # the rug heuristic has a baseline. Only when enabled (feature off =
        # zero extra RPC).
        if self.rug_heuristics_enabled:
            try:
                entry_slot = await self.solana_client.get_slot()
                if entry_slot is None:
                    logger.warning(
                        "Rug heuristics: entry slot unavailable; "
                        "drain/buyer tests cannot baseline"
                    )
                depth_lamports = 0.0
                implementations = get_platform_implementations(
                    token_info.platform, self.solana_client
                )
                pool_address = (
                    token_info.bonding_curve
                    or token_info.pool_state
                    or (
                        implementations.address_provider.derive_pool_address(
                            token_info.mint
                        )
                    )
                )
                try:
                    state = await implementations.curve_manager.get_pool_state(
                        pool_address
                    )
                    depth_lamports = max(
                        state.get("real_quote_reserves", 0) or 0,
                        state.get("virtual_quote_reserves", 0) or 0,
                    )
                except Exception:
                    logger.exception("Rug heuristics: entry depth unavailable")
                if entry_slot is not None:
                    self._rug_entry[str(token_info.mint)] = (
                        entry_slot,
                        depth_lamports,
                    )
            except Exception:
                logger.exception("Rug heuristics entry capture failed")
        # Track token program for cleanup
        mint_str = str(token_info.mint)
        if token_info.token_program_id:
            self.traded_token_programs[mint_str] = token_info.token_program_id

        # Per-mint dedup: a second detect of the same mint must not open a
        # second position while one is live (seen live: GEKKO bought twice
        # 5s apart, both stopped out at -20%).
        if mint_str in self._active_exit_mints:
            logger.warning(
                f"Skipping {token_info.symbol} - position already open for this mint"
            )
            self._capture_detect(token_info, "position_already_open")
            return

        # Choose exit strategy
        if not self.marry_mode:
            if self.exit_strategy == "tp_sl":
                # The monitor runs for the position's whole life; with
                # max_hold_time removed it may never return. Await it inline
                # only for the single-token modes; in yolo mode it must run
                # as a task or the token-queue processor starves behind the
                # first open position (seen live: 1306 queued, 2 processed).
                self._active_exit_mints.add(mint_str)
                if self.yolo_mode:
                    # fire-and-forget: awaiting inline starves the token-queue
                    # processor (the 1306-queued/2-processed deadlock).
                    asyncio.create_task(self._handle_tp_sl_exit(token_info, buy_result))
                else:
                    try:
                        await self._handle_tp_sl_exit(token_info, buy_result)
                    finally:
                        self._active_exit_mints.discard(mint_str)
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

    async def _emit_shadow_mark(
        self,
        token_info: TokenInfo,
        position: Position,
        current_price: float,
        quote_label: str,
    ) -> None:
        """Rate-limited sim_mark + strategies-ledger tick (every ~15s).

        Shared by the tp_sl and trail exit paths so open positions stay
        replayable regardless of exit mode. Runs in BOTH shadow and live
        modes: real positions get marked against the real price path so the
        strategies ledger can evaluate every strategy on live data.
        """
        if self.shadow_capture is None:
            return
        pnl = position.get_pnl(current_price)
        mark_interval = max(1, 15 // max(1, self.price_check_interval))
        self._mark_tick = getattr(self, "_mark_tick", 0) + 1
        if self._mark_tick % mark_interval != 0:
            return
        logger.info(
            f"[SHADOW] {token_info.symbol} unrealized: "
            f"{pnl['price_change_pct']:+.2f}% "
            f"({pnl['unrealized_pnl_quote']:+.6f} {quote_label}) "
            f"price {current_price:.3e}"
        )
        self.shadow_capture.mark(
            mint=str(token_info.mint),
            symbol=token_info.symbol,
            platform=token_info.platform.value,
            price=current_price,
            pnl_pct=float(pnl["price_change_pct"]),
            pnl_quote=float(pnl["unrealized_pnl_quote"]),
            hold_seconds=(datetime.utcnow() - position.entry_time).total_seconds(),
        )
        if self.strategies_ledger is not None:
            self.strategies_ledger.decide(
                mint=str(position.mint),
                current_price=current_price,
                hold_seconds=(datetime.utcnow() - position.entry_time).total_seconds(),
            )

    async def _handle_tp_sl_exit(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Handle take profit/stop loss exit strategy."""
        try:
            await self._tp_sl_exit_inner(token_info, buy_result)
        finally:
            mint_key = str(token_info.mint)
            self._active_exit_mints.discard(mint_key)
            # trail states are per-position; a stale one would give a
            # re-opened mint the previous run's floor/peak
            self._trail_states.pop(mint_key, None)
            # The persistent unsold watcher needs TokenInfo + Position only;
            # transient buyer/advisory maps are safe to release here too.
            self._clear_mint_tracking_state(mint_key)

    async def _tp_sl_exit_inner(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Take-profit/stop-loss monitor body (dedup cleanup lives above)."""
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
        # R3-P1-4: register the coin's creator wallet(s) so the trade feed can
        # flag a creator sell for a monitored position.
        if self.exit_on_creator_sell:
            mint_key = str(token_info.mint)
            creators = self._creator_sellers.setdefault(mint_key, set())
            for creator in (token_info.creator, token_info.user):
                if creator:
                    creators.add(str(creator))
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
                if self.shadow_capture is not None:
                    # Shadow close for the time-based path: realized PnL is
                    # computed off the simulated sell price vs the buy price.
                    buy_px = buy_result.price or 0.0
                    amount = buy_result.amount or 0.0
                    pnl_quote = (price - buy_px) * amount
                    fee_fields = await self._fee_estimate_fields()
                    self.shadow_capture.sim_close(
                        mint=str(token_info.mint),
                        symbol=token_info.symbol,
                        platform=token_info.platform.value,
                        price=price,
                        amount_tokens=amount,
                        quote_received=price * amount,
                        exit_reason="time_based_sell",
                        pnl_quote=pnl_quote,
                        pnl_pct=(price - buy_px) / buy_px if buy_px > 0 else 0.0,
                        hold_seconds=self.wait_time_after_buy or 0.0,
                        pool_snapshot=await self._pool_snapshot(token_info),
                        **fee_fields,
                        reordering_bps=0,  # R4-P0-1: shadow fills at observed price
                    )
                    # R4-P1-7: finalize the strategies-ledger entry (time-
                    # based close; shadow only).
                    if self.strategies_ledger is not None:
                        self.strategies_ledger.close_position(
                            mint=str(token_info.mint),
                            close_price=price,
                            exit_reason="time_based_sell",
                            hold_seconds=self.wait_time_after_buy or 0.0,
                            **fee_fields,
                        )
            else:
                # R3-P2-13: serialize the sell against any in-flight buy.
                async with self._trade_serialization_lock:
                    sell_result = await self.seller.execute(
                        token_info,
                        token_amount=buy_result.amount or 0.0,
                        token_price=token_price,
                    )  # type: ignore[arg-type]  # token_price is built from buy_result.price

            if sell_result.success:
                logger.info(f"Successfully sold {token_info.symbol}")
                # Live research capture: real time-based close, same schema
                # as the shadow close, tagged for calibration (mirrors the
                # staging/trail live closes in exit_engine).
                if not self.shadow_mode and self.shadow_capture is not None:
                    fee_fields = await self._fee_estimate_fields()
                    _qty = sell_result.amount or buy_result.amount or 0.0
                    _buy_px = buy_result.price or 0.0
                    self.shadow_capture.sim_close(
                        mint=str(token_info.mint),
                        symbol=token_info.symbol,
                        platform=token_info.platform.value,
                        price=sell_result.price,
                        amount_tokens=_qty,
                        quote_received=(sell_result.price or 0.0) * _qty,
                        exit_reason="time_based_sell",
                        pnl_quote=(
                            (sell_result.price - _buy_px) * _qty
                            if sell_result.price and _buy_px
                            else 0.0
                        ),
                        pnl_pct=(
                            (sell_result.price - _buy_px) / _buy_px
                            if sell_result.price and _buy_px
                            else 0.0
                        ),
                        hold_seconds=self.wait_time_after_buy or 0.0,
                        pool_snapshot=await self._pool_snapshot(token_info),
                        **fee_fields,
                        reordering_bps=0,
                        mode="live",
                        tx_signature=sell_result.tx_signature,
                    )
                    if self.strategies_ledger is not None:
                        self.strategies_ledger.close_position(
                            mint=str(token_info.mint),
                            close_price=sell_result.price,
                            exit_reason="time_based_sell",
                            hold_seconds=self.wait_time_after_buy or 0.0,
                            **fee_fields,
                        )
                # R3-P2-10: track realized PnL for the risk guards. The
                # time-based path has the buy price; PnL = (sell-buy)*amount.
                try:
                    _buy_px = buy_result.price or 0.0
                    _qty = sell_result.amount or buy_result.amount or 0.0
                    _pnl = (
                        (sell_result.price - _buy_px) * _qty
                        if sell_result.price and _buy_px
                        else 0.0
                    )
                    self.risk_tracker.record_close(_pnl)
                except Exception:
                    self.risk_tracker.record_close(0.0)
                self._log_trade(
                    "sell",
                    token_info,
                    sell_result.price,
                    sell_result.amount,
                    sell_result.tx_signature,
                    quote_received_sol=(
                        sell_result.price * sell_result.amount
                        if sell_result.price
                        and sell_result.amount
                        and (normalize_quote_mint(token_info.quote_mint) == WSOL_MINT)
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

    async def _confirm_migration(self, mint_str: str) -> None:
        """R4-P3-2: verify a log-line migration label against pool state.

        Runs off the hot path (fire-and-forget task): derives the mint's
        PumpSwap pool address and reads its state. pool_exists=True upgrades
        the watcher's label to VERIFIED; False refutes it. Every failure
        mode leaves the label unconfirmed (fail-closed for
        require_confirmed consumers) and never raises into the trade path.
        """
        watcher = self.migrate_watcher
        if watcher is None:
            return
        try:
            from interfaces.core import Pubkey

            mint = Pubkey.from_string(mint_str)
            implementations = self.platform_implementations
            pool = implementations.address_provider.derive_pool_address(mint)
            pool_exists = False
            if pool is not None:
                try:
                    state = await implementations.curve_manager.get_pool_state(
                        pool
                    )
                    pool_exists = bool(state)
                except Exception:
                    # Curve manager may not serve AMM pools; fall back to an
                    # account existence read through the client.
                    info = await self.solana_client.get_account_info(pool)
                    pool_exists = info is not None
            watcher.confirm(mint_str, pool_exists)
            logger.info(
                f"R4-P3-2: migration label for {mint_str} "
                f"{'VERIFIED' if pool_exists else 'REFUTED'} (pool-state read)"
            )
        except Exception:
            logger.exception(
                f"R4-P3-2: migration confirm read failed for {mint_str} "
                f"(label left unconfirmed)"
            )

    async def _check_depth_floor(self, token_info: TokenInfo) -> str | None:
        """P2-EXIT-1: reason to force-exit for shallow pool depth, or None.

        Reads the pool's real quote reserves (already TTL-cached by the
        price read moments earlier) and compares against
        ``min_pool_depth_sol``. None when the pool is deep enough, when the
        depth is unknown (fail-open), or when the feature is off.
        """
        if self.min_pool_depth_sol <= 0:
            return None
        try:
            implementations = self.platform_implementations
            state = await implementations.curve_manager.get_pool_state(
                self._get_pool_address(token_info)
            )
            real_quote = state.get("real_quote_reserves", 0) or 0
            virtual_quote = state.get("virtual_quote_reserves", 0) or 0
            depth_lamports = max(real_quote, virtual_quote)
            if depth_lamports <= 0:
                # Graduated or unmappable pool: not a curve-depth case. The
                # AMM pool has its own liquidity; don't force-exit on this.
                return None
            depth_sol = depth_lamports / 1e9
            if depth_sol < self.min_pool_depth_sol:
                return (
                    f"pool depth {depth_sol:.6f} SOL < "
                    f"min_pool_depth_sol {self.min_pool_depth_sol}"
                )
        except Exception:
            logger.exception("Depth-floor check failed (fail-open)")
            return None
        return None

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

    async def _check_rug_signal(self, token_info: TokenInfo) -> str | None:
        """P2-EXIT-4: rug-heuristic reason to force-exit, or None.

        Compares the current slot + pool depth against the entry baseline
        captured at buy time. The unique-buyer half uses the trade-feed
        counter (may be empty if the listener has no trade feed — then only
        the drain half can fire). Fails open on any missing data.
        """
        if not self.rug_heuristics_enabled:
            return None
        mint_key = str(token_info.mint)
        entry = self._rug_entry.get(mint_key)
        if entry is None:
            return None  # no baseline (entry capture failed) -> fail open
        entry_slot, entry_depth = entry
        try:
            from screening.rug_heuristics import RugContext, rug_exit_reason

            current_slot = await self.solana_client.get_slot()
            implementations = get_platform_implementations(
                token_info.platform, self.solana_client
            )
            pool_address = (
                token_info.bonding_curve
                or token_info.pool_state
                or implementations.address_provider.derive_pool_address(token_info.mint)
            )
            state = await implementations.curve_manager.get_pool_state(pool_address)
            current_depth = max(
                state.get("real_quote_reserves", 0) or 0,
                state.get("virtual_quote_reserves", 0) or 0,
            )
            ctx = RugContext(
                entry_slot=entry_slot,
                unique_buyers=len(self._rug_buyers.get(mint_key, set())),
                entry_real_quote_lamports=entry_depth,
            )
            return rug_exit_reason(
                ctx,
                current_slot,
                current_depth,
                min_unique_buyers=self.rug_min_unique_buyers,
                check_slots=self.rug_check_slots,
                early_drain_ratio=self.rug_early_drain_ratio,
            )
        except Exception:
            logger.exception("Rug-heuristic check failed (fail-open)")
            return None

    async def _post_entry_advisory(
        self, token_info: TokenInfo, position: Position, current_price: float
    ) -> str | None:
        """R5-P2-3: log-only post-entry protection signals.

        Evaluates the signals that WANT to force-exit a held position —
        creator sold, pool drain, buyer stall — but never exits. Returns a
        reason string for the monitor to log (and for the ledger/backtester
        to correlate), so evidence can accumulate BEFORE any of these is
        armed as a hard gate. No side effects: no sells, no state mutation.
        """
        if not self.post_entry_advisories_enabled:
            return None
        if position.is_active is False:
            return None
        mint_key = str(token_info.mint)
        # Goal: the advisory fires at most once per position (repeated
        # warnings every price-check tick would flood the log).
        if getattr(position, "_advisory_fired", False):
            return None
        reasons: list[str] = []
        # R3-P1-4 signal: creator sold after we bought.
        if mint_key in self._creator_sold_for_exit:
            reasons.append("creator-sell")
        # Pool-drain signal (reuse the rug heuristic's depth comparison but
        # only as an advisory; the rug exit stays gated on its own flag).
        try:
            if self.rug_heuristics_enabled:
                rug = await self._check_rug_signal(token_info)
                if rug:
                    reasons.append(f"rug-drain ({rug})")
        except Exception:
            logger.debug("post-entry rug advisory failed (non-fatal)", exc_info=True)
        # Buyer stall: no NEW unique buyers since entry within the check
        # horizon — hype fading. Fails open: no data -> no advisory.
        buyers = self._rug_buyers.get(mint_key)
        if buyers is not None and len(buyers) == 0 and self.rug_min_unique_buyers > 0:
            reasons.append("buyer-stall")
        if not reasons:
            return None
        position._advisory_fired = True  # noqa: SLF001
        return "; ".join(reasons)

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

                # R5-P2-3: log-only post-entry advisory. Evaluates
                # creator-sell / rug-drain / buyer-stall on held positions and
                # logs the reason WITHOUT exiting — evidence before any hard
                # gate is armed. Off by default.
                if self.post_entry_advisories_enabled:
                    advise = await self._post_entry_advisory(
                        token_info, position, current_price
                    )
                    if advise:
                        logger.warning(
                            f"[ADVISORY] {token_info.symbol}: {advise} "
                            f"(log-only, no exit — R5-P2-3 shadow evidence)"
                        )

                # P2-EXIT-1: absolute depth-floor forced exit. When the pool
                # can no longer absorb a full unwind within the slippage
                # budget, waiting for TP/SL is worse than exiting now — the
                # depth only shrinks from here. Off by default.
                if self.min_pool_depth_sol > 0:
                    depth_exit = await self._check_depth_floor(token_info)
                    if depth_exit:
                        logger.warning(
                            f"Liquidity exit for {token_info.symbol}: "
                            f"{depth_exit} (min_pool_depth_sol "
                            f"{self.min_pool_depth_sol})"
                        )
                        exit_reason = ExitReason.LIQUIDITY
                        exit_price = (
                            current_price if current_price > 0 else last_known_price
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
                        continue

                # R3-P1-2: staged TP/SL partial sells. When ranges are
                # configured, they fire BEFORE the single TP/SL check — a
                # step at +40% sells its share, and the position stays open
                # for the next step / the final close.
                if self.tp_ranges or self.sl_ranges:
                    staged = await self._handle_staged_exit(
                        token_info, position, current_price
                    )
                    if staged:
                        # A staged sell consumed part/all of the position.
                        if not position.is_active:
                            break
                        # The final band step may have closed the position
                        # already; otherwise keep monitoring for more steps.
                        continue

                # R4-P1-7: staged-then-trail exit mode. When exit_mode is
                # "staged_trail", this runs INSTEAD of the single TP/SL
                # check for the remainder of the position (the initial TP
                # share + trailing floor). Disabled-first (tp_sl default).
                if self.exit_mode in ("staged_trail", "trail10"):
                    trail_exit = await self._handle_staged_trail_exit(
                        token_info, position, current_price
                    )
                    if trail_exit:
                        break
                    # Emit the research mark/ledger tick here too: the shared
                    # mark block below is only reached on the tp_sl path, and
                    # skipping it left trail positions with no sim_mark and
                    # no strategies-ledger decide rows (audit 2026-10-27).
                    await self._emit_shadow_mark(
                        token_info, position, current_price, quote_label
                    )
                    # Keep monitoring: the floor re-arms as the price runs.
                    await asyncio.sleep(self.price_check_interval)
                    continue

                # R4-P1-9: stagnation exit (opt-in, 0 = off). If the price
                # has stayed inside stagnation_band_pct of the ENTRY price
                # for stagnation_exit_min minutes, the hype window passed —
                # exit rather than wait for a TP that's not coming.
                if self.stagnation_exit_min > 0:
                    elapsed_min = (
                        datetime.utcnow() - position.entry_time
                    ).total_seconds() / 60.0
                    if elapsed_min >= self.stagnation_exit_min:
                        upper = position.entry_price * (1 + self.stagnation_band_pct)
                        lower = position.entry_price * (1 - self.stagnation_band_pct)
                        if lower <= current_price <= upper:
                            logger.info(
                                f"Stagnation exit for {token_info.symbol}: price "
                                f"{current_price:.3e} inside "
                                f"[{lower:.3e}, {upper:.3e}] band for "
                                f"{elapsed_min:.1f} min (>= "
                                f"{self.stagnation_exit_min})"
                            )
                            exit_reason = ExitReason.STAGNATION
                            exit_price = current_price
                            exit_sell_attempts += 1
                            if await self._run_exit_attempt(
                                token_info,
                                position,
                                exit_reason,
                                exit_price,
                                exit_sell_attempts,
                            ):
                                break
                            continue

                # Check if position should be exited
                should_exit, exit_reason = position.should_exit(current_price)

                if self.rug_heuristics_enabled:
                    rug_reason = await self._check_rug_signal(token_info)
                    if rug_reason:
                        logger.warning(
                            f"Rug exit for {token_info.symbol}: {rug_reason}"
                        )
                        exit_reason = ExitReason.RUG
                        exit_price = (
                            current_price if current_price > 0 else last_known_price
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
                        continue

                # R3-P1-4: the creator sold — force exit (off by default).
                if (
                    self.exit_on_creator_sell
                    and str(token_info.mint) in self._creator_sold_for_exit
                ):
                    logger.warning(
                        f"Creator-sell exit for {token_info.symbol}: creator "
                        f"wallet sold on the feed"
                    )
                    exit_reason = ExitReason.CREATOR_SELL
                    exit_price = (
                        current_price if current_price > 0 else last_known_price
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
                    continue

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
                    # Research tick: rate-limited sim_mark + ledger decide,
                    # shared with the trail exit path via _emit_shadow_mark.
                    await self._emit_shadow_mark(
                        token_info, position, current_price, quote_label
                    )
                    # R4-P1-8: momentum-fade advisory (LOG-ONLY, no
                    # behavior change). The 53-trade cohort: volume
                    # -50% from peak while price is not making new highs
                    # = distribution, exit is the norm. Track the peak
                    # trade rate per mint and log a hint when the rate
                    # halves without a new price high. A replay can act
                    # on the hint later; shadow mode only reports it.
                    try:
                        rate = self._wash_lpi.event_rate_per_min(str(position.mint))
                        peak_rate = self._momentum_peak_rate.get(
                            str(position.mint), 0.0
                        )
                        peak_price = self._momentum_peak_price.get(
                            str(position.mint), 0.0
                        )
                        if rate is not None:
                            if rate > peak_rate or current_price > peak_price:
                                self._momentum_peak_rate[str(position.mint)] = max(
                                    peak_rate, rate
                                )
                                self._momentum_peak_price[str(position.mint)] = max(
                                    peak_price, current_price
                                )
                            elif (
                                peak_rate > 0
                                and rate < 0.5 * peak_rate
                                and current_price <= peak_price * 1.05
                            ):
                                logger.info(
                                    f"[SHADOW] momentum-fade hint for "
                                    f"{token_info.symbol}: trade rate "
                                    f"{rate:.1f}/min < 50% of peak "
                                    f"{peak_rate:.1f}/min with no new "
                                    f"high (price {current_price:.3e} vs "
                                    f"peak {peak_price:.3e}) — "
                                    f"distribution risk"
                                )
                    except Exception:
                        logger.debug(
                            "momentum-fade hint failed (non-fatal)",
                            exc_info=True,
                        )
                    if logger.isEnabledFor(10):
                        logger.debug(
                            f"Position status: {current_price:.8f} {quote_label} "
                            f"({pnl['price_change_pct']:+.2f}%)"
                        )

                # Wait before next price check
                # R3-P2-12: an own-wallet trade prompt skips the wait so the
                # exit is evaluated immediately (off by default = polling).
                if (
                    self.event_driven_exits_enabled
                    and str(token_info.mint) in self._own_trade_prompt
                ):
                    self._own_trade_prompt.discard(str(token_info.mint))
                    logger.debug(f"Event-driven exit prompt for {token_info.symbol}")
                    continue
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
        self._persist_unsold()  # C7.2: durable on every mutation
        logger.warning(
            f"{token_info.symbol}: bounded exit retries spent; "
            f"registering with persistent exit watcher "
            f"(TTL {self.persistent_exit_ttl}s, interval {self.persistent_exit_interval}s)"
        )
        if self._unsold_watcher_task is None or self._unsold_watcher_task.done():
            self._unsold_watcher_task = asyncio.create_task(
                self._unsold_position_watcher()
            )

    # ------------------------------------------------------------------
    # C7.2: durable unsold registry — serialize/rehydrate/persist helpers.
    # ------------------------------------------------------------------
    @staticmethod
    def _token_info_to_row(token_info: TokenInfo) -> dict[str, Any]:
        """Serialize the TokenInfo half of a durable unsold row."""
        pubkey_fields = (
            "bonding_curve", "associated_bonding_curve", "pool_state",
            "base_vault", "quote_vault", "global_config", "platform_config",
            "user", "creator", "creator_vault", "token_program_id",
            "quote_mint", "quote_token_program_id",
        )
        row: dict[str, Any] = {
            "symbol": token_info.symbol,
            "platform": token_info.platform.value,
            "is_mayhem_mode": token_info.is_mayhem_mode,
            "is_cashback_coin": token_info.is_cashback_coin,
            "is_holder_reward": token_info.is_holder_reward,
            "creator_fee_bps": token_info.creator_fee_bps,
            "create_slot": token_info.create_slot,
        }
        row.update({
            field: (str(value) if (value := getattr(token_info, field)) else None)
            for field in pubkey_fields
        })
        return row

    @staticmethod
    def _token_info_from_row(row: dict[str, Any]) -> TokenInfo:
        """Rebuild TokenInfo from the JSON-safe durable row fields."""
        pubkey_fields = (
            "bonding_curve", "associated_bonding_curve", "pool_state",
            "base_vault", "quote_vault", "global_config", "platform_config",
            "user", "creator", "creator_vault", "token_program_id",
            "quote_mint", "quote_token_program_id",
        )
        kwargs: dict[str, Any] = {
            "name": row.get("symbol", ""),
            "symbol": row.get("symbol", ""),
            "uri": "",
            "mint": Pubkey.from_string(row["mint"]),
            "platform": Platform(row.get("platform", Platform.PUMP_FUN.value)),
            "is_mayhem_mode": bool(row.get("is_mayhem_mode", False)),
            "is_cashback_coin": bool(row.get("is_cashback_coin", False)),
            "is_holder_reward": bool(row.get("is_holder_reward", False)),
            "creator_fee_bps": int(row.get("creator_fee_bps", 0) or 0),
            "create_slot": int(row["create_slot"]) if row.get("create_slot") else None,
        }
        kwargs.update({
            field: Pubkey.from_string(value) if (value := row.get(field)) else None
            for field in pubkey_fields
        })
        return TokenInfo(**kwargs)

    def _serialize_unsold(self) -> list[dict]:
        """Serialize the unsold registry for JSON persistence.

        Pubkey and datetime are not JSON-native; both are losslessly
        stringified here (and parsed back in `_rehydrate_unsold`).
        """
        rows: list[dict] = []
        for key, (token_info, position) in self._unsold_positions.items():
            # Class-bound call: verifier stubs (SimpleNamespace doubles) run
            # the real helper without owning it as an instance attribute.
            row = UniversalTrader._token_info_to_row(token_info)
            row.update(
                {
                    "mint": key,
                    "entry_price": position.entry_price,
                    "quantity": position.quantity,
                    "entry_time": position.entry_time.isoformat(),
                    "take_profit_price": position.take_profit_price,
                    "stop_loss_price": position.stop_loss_price,
                    "max_hold_time": position.max_hold_time,
                    "position_id": position.position_id,
                    # C4.8: persist banked leg PnL so a restart mid-ladder
                    # keeps the blended position result.
                    "realized_pnl_quote": position.realized_pnl_quote,
                    "entered_watcher_at": time.time(),
                }
            )
            rows.append(row)
        return rows

    def _persist_unsold(self) -> None:
        """Write the unsold registry to disk (best-effort, non-fatal)."""
        if not self._unsold_persist_path:
            return
        try:
            p = Path(self._unsold_persist_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            with tmp.open("w") as f:
                json.dump(
                    {"schema": 1, "updated_at": time.time(), "positions": self._serialize_unsold()},
                    f,
                )
            tmp.replace(p)
        except Exception:
            logger.exception("Failed to persist unsold registry")

    def _rehydrate_unsold(self) -> None:
        """Load the durable unsold registry from disk into the live dict.

        Best-effort: corrupt/absent files log a warning and leave the set
        empty (the stuck-token sweeper is the safety net for tokens a bad
        file would drop). Re-arms the TTL watcher when positions were
        recovered so the 10-minute deadline is not lost to the restart.
        """
        if not self._unsold_persist_path:
            return
        try:
            p = Path(self._unsold_persist_path)
            if not p.exists():
                return
            with p.open() as f:
                data = json.load(f)
            rows = data.get("positions", []) if isinstance(data, dict) else []
            recovered = 0
            for row in rows:
                try:
                    mint = row["mint"]
                    # Class-bound call (same stub-compat reason as serialize).
                    info = UniversalTrader._token_info_from_row(row)
                    position = Position(
                        mint=info.mint,
                        symbol=info.symbol,
                        entry_price=float(row["entry_price"]),
                        quantity=float(row["quantity"]),
                        entry_time=datetime.fromisoformat(row["entry_time"]),
                        take_profit_price=(
                            float(row["take_profit_price"])
                            if row.get("take_profit_price") is not None
                            else None
                        ),
                        stop_loss_price=(
                            float(row["stop_loss_price"])
                            if row.get("stop_loss_price") is not None
                            else None
                        ),
                        max_hold_time=(
                            int(row["max_hold_time"])
                            if row.get("max_hold_time") is not None
                            else None
                        ),
                        is_active=True,
                        exit_reason=None,
                        exit_price=None,
                        exit_time=None,
                        position_id=row.get("position_id", ""),
                        # C4.8: restore banked leg PnL (0.0 for pre-C4.8 rows).
                        realized_pnl_quote=float(
                            row.get("realized_pnl_quote", 0.0) or 0.0
                        ),
                    )
                    if position.quantity <= 0:
                        logger.warning(
                            f"Unsold rehydrate: skipping {info.symbol} "
                            f"(quantity {position.quantity})"
                        )
                        continue
                    self._unsold_positions[mint] = (info, position)
                    recovered += 1
                except Exception:
                    logger.exception(
                        f"Unsold rehydrate: skipped malformed row {row.get('mint')}"
                    )
            if recovered:
                logger.warning(
                    f"Rehydrated {recovered} unsold position(s) from "
                    f"{self._unsold_persist_path}; re-arming persistent watcher"
                )
                if self._unsold_watcher_task is None or self._unsold_watcher_task.done():
                    self._unsold_watcher_task = asyncio.create_task(
                        self._unsold_position_watcher()
                    )
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("Failed to rehydrate unsold registry")

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
                    self._persist_unsold()  # C7.2
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
                    self._persist_unsold()  # C7.2
                    entered.pop(key, None)


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
