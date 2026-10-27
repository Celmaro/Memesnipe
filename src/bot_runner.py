import asyncio
import gc
import logging
import multiprocessing
import sys
from datetime import datetime
from pathlib import Path

# A2-P2-1: raise GC thresholds so stop-the-world collections are rare and
# never land mid-transaction-build on the hot path (audit §7.2). The young
# (0) and old (1) generation thresholds matter less than the large gen-0
# trigger; the numbers come from the audited configuration.
gc.set_threshold(50000, 50, 50)

# Try to use uvloop on Unix or winloop on Windows for better performance
# Fall back to standard asyncio if not available
try:
    if sys.platform == "win32":
        import winloop

        asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
        logging.info("Using winloop event loop policy for improved performance")
    else:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        logging.info("Using uvloop event loop policy for improved performance")
except ImportError:
    logging.info(
        "Using standard asyncio event loop (install uvloop/winloop for better performance)"
    )

from config_loader import (
    get_platform_from_config,
    load_bot_config,
    print_config_summary,
    validate_platform_listener_combination,
)
from research.shadow_capture import ShadowDataCapture
from research.strategies_ledger import StrategiesLedger
from trading.universal_trader import (
    DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
    UniversalTrader,
)
from utils.logger import install_secret_redaction, setup_file_logging


def setup_logging(bot_name: str):
    """Set up logging to file for a specific bot instance."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = log_dir / f"{bot_name}_{timestamp}.log"

    setup_file_logging(str(log_filename))


async def start_bot(config_path: str):
    """Start a trading bot with the configuration from the specified path."""
    cfg = load_bot_config(config_path)
    setup_logging(cfg["name"])
    print_config_summary(cfg)

    # R4-P4-3: startup RPC latency self-test (off by default). Three
    # get_slot round trips, tiered warning. The trader's own first reads
    # would surface a dead endpoint, but a SLOW endpoint is the sniper's
    # silent killer (latency is the #1 variable), so measure it before any
    # listening starts.
    if cfg.get("node", {}).get("startup_latency_test", False):
        try:
            from core.client import SolanaClient
            from utils.logger import get_logger

            logger = get_logger("latency")
            probe = SolanaClient(
                cfg["rpc_endpoint"],
                cfg.get("node", {}).get("max_rps", 25),
            )
            samples: list[float] = []
            for _ in range(3):
                start = datetime.now()
                slot = await probe.get_slot()
                dt = (datetime.now() - start).total_seconds() * 1000.0
                if slot is not None:
                    samples.append(dt)
            if samples:
                avg = sum(samples) / len(samples)
                logger.info(
                    f"RPC latency self-test: avg {avg:.0f} ms "
                    f"({len(samples)} get_slot samples)"
                )
                if avg > 200:
                    logger.warning(
                        f"RPC latency {avg:.0f} ms > 200 ms — snipe latency "
                        f"will lose races; consider a co-located/dedicated "
                        f"endpoint (5-15 ms Yellowstone gRPC co-located)"
                    )
                else:
                    logger.info("RPC latency within the 200 ms snipe budget")
            else:
                logger.error("RPC latency self-test: get_slot never succeeded")
        except Exception:
            logging.exception("Startup latency self-test failed (non-fatal)")

    # Get and validate platform from configuration
    try:
        platform = get_platform_from_config(cfg)
        logging.info(f"Detected platform: {platform.value}")
    except ValueError as e:
        logging.exception(f"Platform configuration error: {e}")
        return

    # Validate platform support
    try:
        from platforms import platform_factory

        if not platform_factory.registry.is_platform_supported(platform):
            logging.error(
                f"Platform {platform.value} is not supported. Available platforms: {[p.value for p in platform_factory.get_supported_platforms()]}"
            )
            return
    except Exception as e:
        logging.exception(f"Could not validate platform support: {e}")
        return

    # Validate listener compatibility
    listener_type = cfg["filters"]["listener_type"]
    if not validate_platform_listener_combination(platform, listener_type):
        from config_loader import get_supported_listeners_for_platform

        supported = get_supported_listeners_for_platform(platform)
        logging.error(
            f"Listener '{listener_type}' is not compatible with platform '{platform.value}'. Supported listeners: {supported}"
        )
        return

    # Initialize universal trader with platform-specific configuration
    try:
        trader = UniversalTrader(
            # Connection settings
            rpc_endpoint=cfg["rpc_endpoint"],
            wss_endpoint=cfg["wss_endpoint"],
            private_key=cfg["private_key"],
            # Platform configuration - pass platform enum directly
            platform=platform,
            # Trade parameters
            buy_amount=cfg["trade"]["buy_amount"],
            buy_slippage=cfg["trade"]["buy_slippage"],
            sell_slippage=cfg["trade"].get("sell_slippage", 0.15),
            # P2-EXIT-3: extra sell-only slippage (bps) so exits land under
            # pool movement between read and confirm.
            sell_slippage_inflation_bps=cfg["trade"].get(
                "sell_slippage_inflation_bps", 10
            ),
            # P2-EXIT-2: impact-bounded sell clamp (bps); 0 = off
            max_sell_impact_bps=cfg["trade"].get("max_sell_impact_bps", 0),
            # P2-EXIT-5: close user ATA in-tx on full-balance sell
            close_ata_on_sell=cfg["trade"].get("close_ata_on_sell", False),
            # R3-P1-3: Jupiter aggregator sell fallback (off by default)
            aggregator_fallback=cfg["trade"].get("aggregator_fallback", False),
            aggregator_slippage_bps=cfg["trade"].get("aggregator_slippage_bps", 500),
            aggregator_max_price_impact_pct=cfg["trade"].get(
                "aggregator_max_price_impact_pct", 20.0
            ),
            # P2-EXIT-1: absolute pool-depth floor (SOL); 0 = off
            min_pool_depth_sol=cfg["trade"].get("min_pool_depth_sol", 0.0),
            # P2-EXIT-4: slot-delta rug heuristics (off by default)
            rug_heuristics_enabled=cfg["filters"].get(
                "rug_heuristics_enabled", False
            ),
            rug_min_unique_buyers=cfg["filters"].get(
                "rug_min_unique_buyers", 2
            ),
            rug_check_slots=cfg["filters"].get("rug_check_slots", 12),
            rug_early_drain_ratio=cfg["filters"].get(
                "rug_early_drain_ratio", 0.5
            ),
            # Extreme fast mode settings
            extreme_fast_mode=cfg["trade"].get("extreme_fast_mode", False),
            extreme_fast_token_amount=cfg["trade"].get("extreme_fast_token_amount", 30),
            curve_refresh_budget=cfg["trade"].get("curve_refresh_budget", 2.0),
            trust_create_event=cfg["trade"].get("trust_create_event", True),
            # Quote asset configuration (pump.fun non-SOL pairs)
            quote_amounts=cfg["trade"].get("quote_amounts"),
            allowed_quote_mints=cfg["filters"].get("allowed_quote_mints"),
            max_transfer_fee_bps=cfg["filters"].get("max_transfer_fee_bps"),
            # Exit strategy configuration
            exit_strategy=cfg["trade"].get("exit_strategy", "time_based"),
            # R4-P1-7: exit mode (tp_sl default; staged_trail = asymmetric
            # staged-then-trail exits) + trail knobs.
            exit_mode=cfg["trade"].get("exit_mode", "tp_sl"),
            trail_initial_tp_share=cfg["trade"].get(
                "trail_initial_tp_share", 0.4
            ),
            trail_initial_tp_pct=cfg["trade"].get("trail_initial_tp_pct", 0.25),
            trail_arm_pct=cfg["trade"].get("trail_arm_pct", 0.10),
            trail_trail_pct=cfg["trade"].get("trail_trail_pct", 0.08),
            trail_hard_stop_pct=cfg["trade"].get("trail_hard_stop_pct", 0.20),
            trail10_arm_pct=cfg["trade"].get("trail10_arm_pct", 0.10),
            trail10_trail_pct=cfg["trade"].get("trail10_trail_pct", 0.10),
            trail10_hard_stop_pct=cfg["trade"].get("trail10_hard_stop_pct", 0.20),
            take_profit_percentage=cfg["trade"].get("take_profit_percentage"),
            stop_loss_percentage=cfg["trade"].get("stop_loss_percentage"),
            tp_ranges=cfg["trade"].get("tp_ranges") or [],
            sl_ranges=cfg["trade"].get("sl_ranges") or [],
            max_hold_time=cfg["trade"].get("max_hold_time"),
            stagnation_exit_min=cfg["trade"].get("stagnation_exit_min", 0),
            stagnation_band_pct=cfg["trade"].get("stagnation_band_pct", 0.02),
            price_check_interval=cfg["trade"].get("price_check_interval", 10),
            max_exit_sell_attempts=cfg["trade"].get(
                "max_exit_sell_attempts", DEFAULT_MAX_EXIT_SELL_ATTEMPTS
            ),
            persistent_exit_ttl=cfg["trade"].get("persistent_exit_ttl", 600),
            persistent_exit_interval=cfg["trade"].get(
                "persistent_exit_interval", 15
            ),
            # Listener configuration
            listener_type=cfg["filters"]["listener_type"],
            # Geyser configuration (if applicable)
            geyser_endpoint=cfg.get("geyser", {}).get("endpoint"),
            geyser_api_token=cfg.get("geyser", {}).get("api_token"),
            geyser_auth_type=cfg.get("geyser", {}).get("auth_type", "x-token"),
            # Priority fee configuration
            enable_dynamic_priority_fee=cfg.get("priority_fees", {}).get(
                "enable_dynamic", False
            ),
            enable_fixed_priority_fee=cfg.get("priority_fees", {}).get(
                "enable_fixed", True
            ),
            fixed_priority_fee=cfg.get("priority_fees", {}).get("fixed_amount", 500000),
            extra_priority_fee=cfg.get("priority_fees", {}).get(
                "extra_percentage", 0.0
            ),
            hard_cap_prior_fee=cfg.get("priority_fees", {}).get("hard_cap", 500000),
            priority_fee_bid_multiplier=cfg.get("priority_fees", {}).get(
                "bid_multiplier", 1.0
            ),
            priority_fee_cache_ttl=cfg.get("priority_fees", {}).get(
                "fee_cache_ttl", 2.0
            ),
            # Retry and timeout settings
            max_retries=cfg.get("retries", {}).get("max_attempts", 10),
            wait_time_after_creation=cfg.get("retries", {}).get(
                "wait_after_creation", 15
            ),
            wait_time_after_buy=cfg.get("retries", {}).get("wait_after_buy", 15),
            wait_time_before_new_token=cfg.get("retries", {}).get(
                "wait_before_new_token", 15
            ),
            max_token_age=cfg.get("filters", {}).get("max_token_age", 0.001),
            token_wait_timeout=cfg.get("timing", {}).get("token_wait_timeout", 120),
            # Cleanup settings
            cleanup_mode=cfg.get("cleanup", {}).get("mode", "disabled"),
            cleanup_force_close_with_burn=cfg.get("cleanup", {}).get(
                "force_close_with_burn", False
            ),
            cleanup_with_priority_fee=cfg.get("cleanup", {}).get(
                "with_priority_fee", False
            ),
            # Trading filters
            match_string=cfg["filters"].get("match_string"),
            bro_address=cfg["filters"].get("bro_address"),
            blocked_creators=cfg["filters"].get("blocked_creators") or [],
            # P2-STRUC-2: persistent creator-PnL blacklist (off by default)
            creator_pnl_blacklist_enabled=cfg["filters"].get(
                "creator_pnl_blacklist_enabled", False
            ),
            creator_pnl_db_path=cfg["filters"].get(
                "creator_pnl_db_path", "state/creator_pnl.db"
            ),
            creator_pnl_min_trades=cfg["filters"].get(
                "creator_pnl_min_trades", 3
            ),
            creator_pnl_max_net_sol=cfg["filters"].get(
                "creator_pnl_max_net_sol", 0.0
            ),
            max_market_cap_sol=cfg["filters"].get("max_market_cap_sol"),
            min_curve_progress_sol=cfg["filters"].get("min_curve_progress_sol"),
            min_market_cap_sol=cfg["filters"].get("min_market_cap_sol"),
            allow_mayhem_mode=cfg["filters"].get("allow_mayhem_mode", False),
            holder_reward_mode=cfg["filters"].get("holder_reward_mode", "trade"),
            exit_on_creator_sell=cfg["filters"].get("exit_on_creator_sell", False),
            post_entry_advisories_enabled=cfg["filters"].get(
                "post_entry_advisories_enabled", False
            ),
            # R3-P2-12: event-driven sell-fill (off by default)
            event_driven_exits_enabled=cfg.get("trade", {}).get(
                "event_driven_exits_enabled", False
            ),
            # R3-P2-14: slot-ordered copy/monitor feed (off by default)
            slot_ordered_feed_enabled=cfg.get("research", {}).get(
                "slot_ordered_feed_enabled", False
            ),
            # R3-P2-15: in-stream filter updates (off by default)
            in_stream_updates_enabled=cfg.get("research", {}).get(
                "in_stream_updates_enabled", False
            ),
            min_unique_holders=cfg["filters"].get("min_unique_holders", 0),
            max_top10_concentration=cfg["filters"].get(
                "max_top10_concentration", 0.0
            ),
            max_gini_coefficient=cfg["filters"].get(
                "max_gini_coefficient", 0.0
            ),
            rug_score_threshold=cfg["filters"].get("rug_score_threshold", 0.0),
            factory_deploy_rate=cfg["filters"].get("factory_deploy_rate", 0.0),
            max_coordinated_supply_share=cfg["filters"].get(
                "max_coordinated_supply_share", 0.0
            ),
            max_wash_share=cfg["filters"].get("max_wash_share", 0.0),
            fragility_scale_min=cfg["filters"].get("fragility_scale_min", 1.0),
            fragility_whale_threshold=cfg["filters"].get(
                "fragility_whale_threshold", 0.6
            ),
            fragility_rate_threshold=cfg["filters"].get(
                "fragility_rate_threshold", 0.6
            ),
            # R4-P0-3: contention-mode fee racing (off by default).
            contention_enabled=cfg.get("filters", {}).get(
                "contention_enabled", False
            ),
            # R4-P1-4: graduation-band advisory (log-only).
            grad_timing_enabled=cfg.get("trade", {}).get(
                "grad_timing_enabled", False
            ),
            grad_band_stats_path=cfg.get("trade", {}).get(
                "grad_band_stats_path", "state/grad_band_stats.json"
            ),
            contention_multiplier=cfg.get("filters", {}).get(
                "contention_multiplier", 2.5
            ),
            contention_creates_per_min=cfg.get("filters", {}).get(
                "contention_creates_per_min", 20.0
            ),
            contention_window_s=cfg.get("filters", {}).get(
                "contention_window_s", 60.0
            ),
            max_fee_fraction_of_position=cfg.get("filters", {}).get(
                "max_fee_fraction_of_position", 0.005
            ),
            min_metadata_extensions=cfg["filters"].get(
                "min_metadata_extensions", 0
            ),
            # R3-P2-10: session risk guards (0 = off)
            max_concurrent_positions=cfg.get("risk", {}).get(
                "max_concurrent_positions", 0
            ),
            daily_loss_limit_sol=cfg.get("risk", {}).get("daily_loss_limit_sol", 0.0),
            budget_sol=cfg.get("risk", {}).get("budget_sol", 0.0),
            max_tracked_tokens=cfg["filters"].get("max_tracked_tokens", 500),
            consecutive_match_count=cfg["filters"].get("consecutive_match_count", 1),
            gate_recheck_interval=cfg["filters"].get("gate_recheck_interval", 0.5),
            honeypot_freeze_authority=cfg.get("filters", {}).get(
                "honeypot_freeze_authority", False
            ),
            min_curve_hold_fraction=cfg.get("filters", {}).get(
                "min_curve_hold_fraction", 0.0
            ),
            # T2.6: RPC catch-up backfill (off by default)
            backfill_enabled=cfg.get("backfill", {}).get("enabled", False),
            backfill_fetch_concurrency=cfg.get("backfill", {}).get(
                "fetch_concurrency", 5
            ),
            backfill_page_size=cfg.get("backfill", {}).get("page_size", 100),
            backfill_max_pages=cfg.get("backfill", {}).get("max_pages", 5),
            backfill_checkpoint=cfg.get("backfill", {}).get("checkpoint"),
            # T3.2: copy-trader engine (disabled by default)
            copy_trader_enabled=cfg.get("copy_trader", {}).get("enabled", False),
            copy_trader_db=cfg.get("copy_trader", {}).get("db"),
            copy_trader_min_interval=cfg.get("copy_trader", {}).get(
                "min_buy_interval", 5.0
            ),
            # R4-P2-1/2/3: copy-hardening knobs (off defaults).
            copy_ring_cofire_threshold=cfg.get("copy_trader", {}).get(
                "ring_cofire_threshold", 0
            ),
            copy_mirror_jitter_ms=cfg.get("copy_trader", {}).get(
                "mirror_jitter_ms", 0.0
            ),
            copy_mirror_split_quanta=cfg.get("copy_trader", {}).get(
                "mirror_split_quanta", 1
            ),
            copy_min_leader_score=cfg.get("copy_trader", {}).get(
                "min_leader_score", 0.0
            ),
            buy_priority_fee=cfg.get("priority_fees", {}).get("buy_fixed_amount"),
            sell_priority_fee=cfg.get("priority_fees", {}).get("sell_fixed_amount"),
            stuck_sweep_interval=cfg.get("trade", {}).get("stuck_sweep_interval", 0),
            shadow_mode=cfg.get("trade", {}).get("shadow_mode", False),
            # Shadow research capture: when shadow mode is on and capture is
            # enabled, the trader gets an append-only JSONL ledger of every
            # detect/sim_open/sim_close event for backtesting.
            shadow_capture=(
                ShadowDataCapture(
                    dir_path=cfg.get("research", {}).get(
                        "shadow_data_dir", "shadow_data"
                    )
                )
                if (
                    cfg.get("trade", {}).get("shadow_mode", False)
                    and cfg.get("research", {}).get("shadow_capture_enabled", True)
                )
                else None
            ),
            # R4-P1-7: strategies ledger — shadow-only parallel exit-strategy
            # testing (same hard guard as shadow_capture). Every open shadow
            # position is evaluated against tp_sl AND staged_trail so one
            # price path yields a per-strategy outcome row.
            strategies_ledger=(
                StrategiesLedger(
                    dir_path=cfg.get("research", {}).get(
                        "shadow_data_dir", "shadow_data"
                    )
                    + "/strategies",
                    tp_pct=cfg["trade"].get("take_profit_percentage", 0.20) or 0.20,
                    sl_pct=cfg["trade"].get("stop_loss_percentage", 0.20) or 0.20,
                    # research.strategies: optional list of {name, kind, config}
                    # specs; kinds: tp_sl | staged_trail | hold (baseline).
                    strategies=cfg.get("research", {}).get("strategies") or None,
                )
                if (
                    cfg.get("trade", {}).get("shadow_mode", False)
                    and cfg.get("research", {}).get("shadow_capture_enabled", True)
                    and cfg.get("research", {}).get(
                        "strategies_ledger_enabled", True
                    )
                )
                else None
            ),
            fail_closed_gates=cfg.get("filters", {}).get("fail_closed_gates", True),
            marry_mode=cfg["filters"].get("marry_mode", False),
            yolo_mode=cfg["filters"].get("yolo_mode", False),
            # Compute unit configuration
            compute_units=cfg.get("compute_units", {}),
            # Node provider configuration
            max_rps=cfg.get("node", {}).get("max_rps", 25),
            # P2-STRUC-1: background blockhash refresh cadence (s)
            blockhash_refresh_interval=cfg["trade"].get(
                "blockhash_refresh_interval", 5.0
            ),
            # R3-P2-2: durable nonce account (off by default)
            durable_nonce_account=cfg["trade"].get("durable_nonce_account"),
            # R3-P2-3: address lookup tables (off by default)
            alt_addresses=cfg["trade"].get("alt_addresses") or [],
            # R3-P2-5: PumpSwap graduation watcher (off by default)
            migrate_watcher_enabled=cfg.get("research", {}).get(
                "pumpswap_migrate_watcher", False
            ),
            # T2.2 Jito bundle executor (disabled by default)
            jito_enabled=cfg.get("jito", {}).get("enabled", False),
            jito_tip_lamports=cfg.get("jito", {}).get("tip_lamports", 10_000),
            jito_endpoints=cfg.get("jito", {}).get("endpoints"),
            # P2-STRUC-4: per-request auth keypairs (region rotation)
            jito_auth_keypairs=cfg.get("jito", {}).get("auth_keypairs") or [],
            # R3-P2-6: DontFront/MEV shield (off by default)
            jito_dontfront=cfg.get("jito", {}).get("dontfront", False),
        )

        # Close sims orphaned by the previous process (restart zombies) so
        # open-position counts stay honest; must run before trading starts.
        if trader.shadow_capture is not None:
            swept = trader.shadow_capture.sweep_zombies()
            if swept:
                logging.info(f"Shadow zombie sweep: closed {swept} orphaned sims")

        await trader.start()

    except Exception as e:
        logging.exception(f"Failed to initialize or start trader: {e}")
        raise


def run_bot_process(config_path):
    asyncio.run(start_bot(config_path))


def run_all_bots():
    """Run all bots defined in YAML files in the 'bots' directory."""
    bot_dir = Path("bots")
    if not bot_dir.exists():
        logging.error(f"Bot directory '{bot_dir}' not found")
        return

    bot_files = list(bot_dir.glob("*.yaml"))
    if not bot_files:
        logging.error(f"No bot configuration files found in '{bot_dir}'")
        return

    logging.info(f"Found {len(bot_files)} bot configuration files")

    processes = []
    skipped_bots = 0

    for file in bot_files:
        try:
            cfg = load_bot_config(str(file))
            bot_name = cfg.get("name", file.stem)

            # Skip bots with enabled=False
            if not cfg.get("enabled", True):
                logging.info(f"Skipping disabled bot '{bot_name}'")
                skipped_bots += 1
                continue

            # Validate platform configuration
            try:
                platform = get_platform_from_config(cfg)

                # Check platform support
                from platforms import platform_factory

                if not platform_factory.registry.is_platform_supported(platform):
                    logging.error(
                        f"Platform {platform.value} is not supported for bot '{bot_name}'. Available platforms: {[p.value for p in platform_factory.get_supported_platforms()]}"
                    )
                    skipped_bots += 1
                    continue

                # Validate listener compatibility
                listener_type = cfg["filters"]["listener_type"]
                if not validate_platform_listener_combination(platform, listener_type):
                    from config_loader import get_supported_listeners_for_platform

                    supported = get_supported_listeners_for_platform(platform)
                    logging.error(
                        f"Listener '{listener_type}' is not compatible with platform '{platform.value}' for bot '{bot_name}'. Supported listeners: {supported}"
                    )
                    skipped_bots += 1
                    continue

            except Exception as e:
                logging.exception(
                    f"Invalid platform configuration for bot '{bot_name}': {e}. Skipping..."
                )
                skipped_bots += 1
                continue

            # Start bot in separate process or main process
            if cfg.get("separate_process", False):
                logging.info(
                    f"Starting bot '{bot_name}' ({platform.value}) in separate process"
                )
                p = multiprocessing.Process(
                    target=run_bot_process, args=(str(file),), name=f"bot-{bot_name}"
                )
                p.start()
                processes.append(p)
            else:
                logging.info(
                    f"Starting bot '{bot_name}' ({platform.value}) in main process"
                )
                asyncio.run(start_bot(str(file)))

        except Exception as e:
            logging.exception(f"Failed to start bot from {file}: {e}")
            skipped_bots += 1

    logging.info(
        f"Started {len(bot_files) - skipped_bots} bots, skipped {skipped_bots} disabled/invalid bots"
    )

    # Wait for all processes to complete
    for p in processes:
        p.join()
        logging.info(f"Process {p.name} completed")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    # Turning the root logger up to INFO is what makes the HTTP clients start
    # printing request URLs, and those URLs carry the provider API key. Redact
    # before the first line is emitted, not after.
    install_secret_redaction()

    # Log supported platforms and listeners
    try:
        from platforms import platform_factory

        supported_platforms = platform_factory.get_supported_platforms()
        logging.info(f"Supported platforms: {[p.value for p in supported_platforms]}")

        # Log listener compatibility for each platform
        from config_loader import get_supported_listeners_for_platform

        for platform in supported_platforms:
            listeners = get_supported_listeners_for_platform(platform)
            logging.info(f"Platform {platform.value} supports listeners: {listeners}")

    except Exception as e:
        logging.warning(f"Could not load platform information: {e}")

    run_all_bots()


if __name__ == "__main__":
    main()
