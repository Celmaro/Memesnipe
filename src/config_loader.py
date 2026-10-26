"""Bot configuration loading and platform/listener validation."""

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from interfaces.core import Platform

# Existing validation rules (keeping all existing ones)
REQUIRED_FIELDS = [
    "name",
    "rpc_endpoint",
    "wss_endpoint",
    "private_key",
    "trade.buy_amount",
    "trade.buy_slippage",
    "trade.sell_slippage",
    "filters.listener_type",
    "filters.max_token_age",
]

CONFIG_VALIDATION_RULES = [
    (
        "trade.buy_amount",
        (int, float),
        0,
        float("inf"),
        "trade.buy_amount must be a positive number",
    ),
    ("trade.buy_slippage", float, 0, 1, "trade.buy_slippage must be between 0 and 1"),
    ("trade.sell_slippage", float, 0, 1, "trade.sell_slippage must be between 0 and 1"),
    (
        "trade.max_exit_sell_attempts",
        int,
        1,
        100,
        "trade.max_exit_sell_attempts must be between 1 and 100",
    ),
    (
        "filters.max_transfer_fee_bps",
        int,
        0,
        10_000,
        "filters.max_transfer_fee_bps must be between 0 and 10000",
    ),
    (
        "priority_fees.fixed_amount",
        int,
        0,
        float("inf"),
        "priority_fees.fixed_amount must be a non-negative integer",
    ),
    (
        "priority_fees.extra_percentage",
        float,
        0,
        1,
        "priority_fees.extra_percentage must be between 0 and 1",
    ),
    (
        "priority_fees.hard_cap",
        int,
        0,
        float("inf"),
        "priority_fees.hard_cap must be a non-negative integer",
    ),
    (
        "priority_fees.bid_multiplier",
        (int, float),
        1.0,
        10.0,
        "priority_fees.bid_multiplier must be between 1.0 and 10.0",
    ),
    (
        "priority_fees.fee_cache_ttl",
        (int, float),
        0.0,
        60.0,
        "priority_fees.fee_cache_ttl must be between 0 and 60 seconds",
    ),
    (
        "retries.max_attempts",
        int,
        0,
        100,
        "retries.max_attempts must be between 0 and 100",
    ),
    (
        "filters.max_token_age",
        (int, float),
        0,
        float("inf"),
        "filters.max_token_age must be a non-negative number",
    ),
    (
        "filters.max_market_cap_sol",
        (int, float),
        0,
        float("inf"),
        "filters.max_market_cap_sol must be a non-negative number",
    ),
    (
        "filters.min_curve_progress_sol",
        (int, float),
        0,
        float("inf"),
        "filters.min_curve_progress_sol must be a non-negative number",
    ),
    (
        "filters.min_market_cap_sol",
        (int, float, type(None)),
        0,
        float("inf"),
        "filters.min_market_cap_sol must be a non-negative number or null",
    ),
    (
        "filters.max_tracked_tokens",
        int,
        1,
        100_000,
        "filters.max_tracked_tokens must be between 1 and 100000",
    ),
    (
        "filters.min_unique_holders",
        int,
        0,
        1_000_000,
        "filters.min_unique_holders must be a non-negative integer (0 = off)",
    ),
    (
        "filters.max_top10_concentration",
        (int, float),
        0.0,
        1.0,
        "filters.max_top10_concentration must be a fraction between 0 and 1 (0 = off)",
    ),
    (
        "filters.min_metadata_extensions",
        int,
        0,
        1_000,
        "filters.min_metadata_extensions must be a non-negative integer (0 = off)",
    ),
    (
        "risk.max_concurrent_positions",
        int,
        0,
        1_000_000,
        "risk.max_concurrent_positions must be a non-negative integer (0 = off)",
    ),
    (
        "risk.daily_loss_limit_sol",
        (int, float),
        0.0,
        1_000_000.0,
        "risk.daily_loss_limit_sol must be a non-negative number (0 = off)",
    ),
    (
        "risk.budget_sol",
        (int, float),
        0.0,
        1_000_000.0,
        "risk.budget_sol must be a non-negative number (0 = off)",
    ),
    (
        "filters.consecutive_match_count",
        int,
        1,
        100,
        "filters.consecutive_match_count must be between 1 and 100",
    ),
    (
        "filters.gate_recheck_interval",
        (int, float),
        0.1,
        60.0,
        "filters.gate_recheck_interval must be between 0.1 and 60 seconds",
    ),
    (
        "filters.min_curve_hold_fraction",
        (int, float),
        0.0,
        1.0,
        "filters.min_curve_hold_fraction must be between 0 and 1",
    ),
    (
        "copy_trader.min_buy_interval",
        (int, float),
        0.1,
        3600.0,
        "copy_trader.min_buy_interval must be between 0.1 and 3600 seconds",
    ),
    (
        "trade.sell_slippage_inflation_bps",
        int,
        0,
        5000,
        "trade.sell_slippage_inflation_bps must be between 0 and 5000 bps",
    ),
    (
        "trade.min_pool_depth_sol",
        (int, float),
        0.0,
        1_000_000.0,
        "trade.min_pool_depth_sol must be between 0 and 1,000,000 SOL",
    ),
    (
        "trade.max_sell_impact_bps",
        int,
        0,
        5000,
        "trade.max_sell_impact_bps must be between 0 and 5000 bps",
    ),
    (
        "trade.aggregator_slippage_bps",
        int,
        1,
        10_000,
        "trade.aggregator_slippage_bps must be between 1 and 10000 bps",
    ),
    (
        "trade.aggregator_max_price_impact_pct",
        (int, float),
        0.0,
        100.0,
        "trade.aggregator_max_price_impact_pct must be between 0 and 100",
    ),
    (
        "trade.blockhash_refresh_interval",
        (int, float),
        0.05,
        60.0,
        "trade.blockhash_refresh_interval must be between 0.05 and 60 seconds",
    ),
    (
        "trade.durable_nonce_account",
        (str, type(None)),
        0,
        0,
        "trade.durable_nonce_account must be a base58 nonce account or null",
    ),
    (
        "filters.creator_pnl_min_trades",
        int,
        1,
        100_000,
        "filters.creator_pnl_min_trades must be between 1 and 100000",
    ),
    (
        "filters.creator_pnl_max_net_sol",
        (int, float),
        -1_000_000.0,
        1_000_000.0,
        "filters.creator_pnl_max_net_sol must be between -1e6 and 1e6 SOL",
    ),
    (
        "filters.rug_min_unique_buyers",
        int,
        1,
        10_000,
        "filters.rug_min_unique_buyers must be between 1 and 10000",
    ),
    (
        "filters.rug_check_slots",
        int,
        1,
        100_000,
        "filters.rug_check_slots must be between 1 and 100000",
    ),
    ("filters.rug_early_drain_ratio", (int, float), 0.0, 1.0, "filters.rug_early_drain_ratio must be between 0 and 1"),
    # P2-STRUC-4: jito.auth_keypairs is a list of strings (validated as such)
    # with no numeric range — the base58-keypair validation happens at trader
    # construction (invalid keys are logged and skipped, never fatal).
    (
        "priority_fees.buy_fixed_amount",
        (int, float),
        0,
        float("inf"),
        "priority_fees.buy_fixed_amount must be a non-negative number",
    ),
    (
        "priority_fees.sell_fixed_amount",
        (int, float),
        0,
        float("inf"),
        "priority_fees.sell_fixed_amount must be a non-negative number",
    ),
    (
        "trade.stuck_sweep_interval",
        (int, float),
        0,
        86_400,
        "trade.stuck_sweep_interval must be between 0 and 86400 seconds",
    ),
]

# Config fields that must be a boolean when present
BOOL_FIELDS = [
    "filters.fail_closed_gates",
    "filters.marry_mode",
    "filters.yolo_mode",
    "filters.honeypot_freeze_authority",
    "trade.shadow_mode",
    "research.shadow_capture_enabled",  # 2026-10-26 shadow research capture
    "research.pumpswap_migrate_watcher",  # R3-P2-5
    "research.slot_ordered_feed_enabled",  # R3-P2-14
    "research.in_stream_updates_enabled",  # R3-P2-15
    "trade.backfill.enabled",
    "trade.close_ata_on_sell",  # P2-EXIT-5
    "trade.aggregator_fallback",  # R3-P1-3
    "filters.creator_pnl_blacklist_enabled",  # P2-STRUC-2
    "filters.rug_heuristics_enabled",  # P2-EXIT-4
    "filters.allow_mayhem_mode",  # R3-P0-2
    "filters.exit_on_creator_sell",  # R3-P1-4
    "trade.event_driven_exits_enabled",  # R3-P2-12
    "copy_trader.enabled",
    "jito.dontfront",  # R3-P2-6
]

# Config fields that must be a list of strings when present
LIST_OF_STR_FIELDS = [
    "filters.blocked_creators",
    "trade.alt_addresses",  # R3-P2-3: address lookup tables
]

# Config fields that must be lists of staged-exit range mappings when present.
LIST_OF_RANGE_FIELDS = ["trade.tp_ranges", "trade.sl_ranges"]

# Valid values for enum-like fields
VALID_VALUES = {
    "filters.listener_type": ["logs", "blocks", "geyser", "shreds"],
    "filters.holder_reward_mode": ["trade", "skip"],  # R3-P0-3
    "cleanup.mode": ["disabled", "on_fail", "after_sell", "post_session"],
    "trade.exit_strategy": ["time_based", "tp_sl", "manual"],
    "platform": ["pump_fun", "lets_bonk", "stonk_fun"],
}

# Platform-specific listener compatibility
PLATFORM_LISTENER_COMPATIBILITY = {
    Platform.PUMP_FUN: ["logs", "blocks", "geyser", "shreds"],
    Platform.LETS_BONK: ["blocks", "geyser"],
    Platform.STONK_FUN: ["blocks", "geyser"],
}


def load_bot_config(path: str) -> dict:
    """Load and validate a bot configuration from a YAML file."""
    config_path = Path(path)
    with config_path.open() as f:
        config = yaml.safe_load(f)

    env_file = config.get("env_file")
    if env_file:
        config_path = Path(path)
        env_path = config_path.parent / env_file
        if env_path.exists():
            load_dotenv(env_path, override=True)
        else:
            load_dotenv(env_file, override=True)

    resolve_env_vars(config)

    # Set default platform if not specified (backward compatibility)
    if "platform" not in config:
        config["platform"] = "pump_fun"

    validate_config(config)
    return config


def resolve_env_vars(config: dict) -> None:
    """Recursively resolve environment variables in the configuration."""

    def resolve_env(value):
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            env_var = value[2:-1]
            env_value = os.getenv(env_var)
            if env_value is None:
                raise ValueError(f"Environment variable '{env_var}' not found")
            return env_value
        return value

    def resolve_all(d):
        for k, v in d.items():
            if isinstance(v, dict):
                resolve_all(v)
            else:
                d[k] = resolve_env(v)

    resolve_all(config)


def get_nested_value(config: dict, path: str) -> Any:
    """Get a nested value from the configuration using dot notation."""
    keys = path.split(".")
    value = config
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"Missing required config key: {path}")
        value = value[key]
    return value


def validate_config(config: dict) -> None:
    """Validate the configuration against defined rules with platform support."""
    # Validate required fields
    for field in REQUIRED_FIELDS:
        get_nested_value(config, field)

    # Validate config rules
    for path, expected_type, min_val, max_val, error_msg in CONFIG_VALIDATION_RULES:
        try:
            value = get_nested_value(config, path)

            if not isinstance(value, expected_type):
                raise ValueError(f"Type error: {error_msg}")

            if isinstance(value, (int, float)) and not (min_val <= value <= max_val):
                raise ValueError(f"Range error: {error_msg}")

        except ValueError as e:
            if str(e).startswith(("Type error:", "Range error:")):
                raise
            continue

    # Validate enum-like fields
    for path, valid_values in VALID_VALUES.items():
        try:
            value = get_nested_value(config, path)
            if value not in valid_values:
                raise ValueError(f"{path} must be one of {valid_values}")
        except ValueError as e:
            if "Missing required config key" not in str(e):
                raise

    # Validate list-of-string fields (e.g. blocked_creators)
    for path in LIST_OF_STR_FIELDS:
        try:
            value = get_nested_value(config, path)
        except ValueError as e:
            if "Missing required config key" in str(e):
                continue
            raise
        if value is None:
            continue
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ValueError(f"{path} must be a list of strings")

    # Validate boolean fields (e.g. filters.fail_closed_gates)
    for path in BOOL_FIELDS:
        try:
            value = get_nested_value(config, path)
        except ValueError as e:
            if "Missing required config key" in str(e):
                continue
            raise
        if value is None:
            continue
        if not isinstance(value, bool):
            raise ValueError(f"{path} must be a boolean (true/false)")

    # Validate staged-exit range fields (R3-P1-2): each must be a list of
    # mappings with numeric from_pct/to_pct/step/share. The ShareRange parser
    # in trading/staged_exit.py is the source of truth for semantics; here we
    # only enforce the container shape so a typo fails at config load.
    for path in LIST_OF_RANGE_FIELDS:
        try:
            value = get_nested_value(config, path)
        except ValueError as e:
            if "Missing required config key" in str(e):
                continue
            raise
        if value is None:
            continue
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            raise ValueError(
                f"{path} must be a list of {{from_pct, to_pct, step, share}} "
                f"mappings (empty list = feature off)"
            )
        for item in value:
            missing = {"from_pct", "to_pct", "step", "share"} - set(item)
            if missing:
                raise ValueError(
                    f"{path} entry missing keys {sorted(missing)}: {item!r}"
                )

    # Interval/hold coherence: an exit that checks the price slower than the
    # hold window can never fire on schedule. Require at least 4 checks within
    # max_hold_time (when both are set and exit_strategy is tp_sl).
    try:
        exit_strategy = get_nested_value(config, "trade.exit_strategy")
        hold = get_nested_value(config, "trade.max_hold_time")
        interval = get_nested_value(config, "trade.price_check_interval")
        if (
            exit_strategy == "tp_sl"
            and hold is not None
            and interval is not None
            and interval * 4 > hold
        ):
            raise ValueError(
                f"trade.price_check_interval ({interval}s) is too large for "
                f"trade.max_hold_time ({hold}s): a tp_sl exit needs at least "
                f"4 checks inside the hold window (interval <= hold/4)"
            )
    except ValueError as e:
        if "Missing required config key" not in str(e):
            raise

    # R3-P1-6: anti-bundler extreme-fast calibration. When extreme-fast is
    # enabled, the fixed token amount must stay above the dev-bundle revert
    # threshold documented in bots/README (below it the dust/6024 window
    # reopens on sell). Off = no-op.
    try:
        ef_mode = get_nested_value(config, "trade.extreme_fast_mode")
        ef_amount = get_nested_value(config, "trade.extreme_fast_token_amount")
        if ef_mode and (ef_amount is None or ef_amount < 2_620_000):
            raise ValueError(
                "trade.extreme_fast_token_amount must be >= 2_620_000 when "
                "extreme_fast_mode is on (below the dev-bundle revert "
                "threshold, dust positions hit pump.fun 6024 Overflow on "
                "sell — see bots/README R3-P1-6)"
            )
    except ValueError as e:
        if "Missing required config key" not in str(e):
            raise

    # Priority fee sources: the manager prefers the dynamic sample and falls
    # back to the fixed fee when the sample is unavailable, so enabling both
    # is the intended resilience combination (T1.4), not a conflict. Require
    # at least one source.
    try:
        dynamic = get_nested_value(config, "priority_fees.enable_dynamic")
        fixed = get_nested_value(config, "priority_fees.enable_fixed")
        if not dynamic and not fixed:
            raise ValueError(
                "At least one of priority_fees.enable_dynamic / enable_fixed "
                "must be true"
            )
    except ValueError as e:
        if "Missing required config key" not in str(e):
            raise

    # Quote-asset configuration must resolve before the bot starts trading,
    # otherwise a bad mint alias only surfaces on the first non-SOL coin.
    validate_quote_config(config)

    # Platform-specific validation
    platform_str = config.get("platform", "pump_fun")
    try:
        platform = Platform(platform_str)
        validate_platform_config(config, platform)
    except ValueError as e:
        if "is not a valid" in str(e):
            raise ValueError(
                f"Invalid platform '{platform_str}'. Must be one of: {[p.value for p in Platform]}"
            )
        raise


def validate_quote_config(config: dict) -> None:
    """Validate trade.quote_amounts and filters.allowed_quote_mints.

    Args:
        config: Loaded bot configuration

    Raises:
        ValueError: If a quote mint alias/address or amount is invalid
    """
    from core.pubkeys import resolve_quote_amounts, resolve_quote_mint

    quote_amounts = config.get("trade", {}).get("quote_amounts")
    if quote_amounts is not None:
        if not isinstance(quote_amounts, dict):
            raise ValueError(
                "trade.quote_amounts must be a mapping of quote mint to amount"
            )
        resolve_quote_amounts(quote_amounts)

    allowed = config.get("filters", {}).get("allowed_quote_mints")
    if allowed is not None:
        if not isinstance(allowed, list) or not allowed:
            raise ValueError(
                "filters.allowed_quote_mints must be a non-empty list of quote mints"
            )
        for mint in allowed:
            resolve_quote_mint(mint)


def validate_platform_config(config: dict, platform: Platform) -> None:
    """Validate platform-specific configuration requirements."""
    # Check if platform is supported
    try:
        from platforms import platform_factory

        if not platform_factory.registry.is_platform_supported(platform):
            raise ValueError(
                f"Platform {platform.value} is not supported. Available platforms: {[p.value for p in platform_factory.get_supported_platforms()]}"
            )
    except ImportError:
        # If platform factory not available, just validate enum
        pass

    # Validate listener compatibility with platform
    try:
        listener_type = get_nested_value(config, "filters.listener_type")
        compatible_listeners = PLATFORM_LISTENER_COMPATIBILITY.get(platform, [])

        if listener_type not in compatible_listeners:
            raise ValueError(
                f"Listener type '{listener_type}' is not compatible with platform '{platform.value}'. "
                f"Compatible listeners: {compatible_listeners}"
            )
    except ValueError as e:
        if "Missing required config key" not in str(e):
            raise

    # Platform-specific configuration validation
    if platform == Platform.PUMP_FUN:
        # pump.fun doesn't require additional config beyond base requirements
        pass

    elif platform == Platform.LETS_BONK:
        # LetsBonk may require additional configuration in the future
        # For now, it uses the same base configuration as pump.fun
        pass


def get_platform_from_config(config: dict) -> Platform:
    """Extract platform enum from configuration."""
    platform_str = config.get("platform", "pump_fun")
    try:
        return Platform(platform_str)
    except ValueError:
        raise ValueError(
            f"Invalid platform '{platform_str}'. Must be one of: {[p.value for p in Platform]}"
        )


def validate_platform_listener_combination(
    platform: Platform, listener_type: str
) -> bool:
    """Check if a platform and listener type are compatible.

    Args:
        platform: Platform enum

    Returns:
        True if combination is valid
    """
    compatible_listeners = PLATFORM_LISTENER_COMPATIBILITY.get(platform, [])
    return listener_type in compatible_listeners


def get_supported_listeners_for_platform(platform: Platform) -> list[str]:
    """Get list of supported listener types for a platform.

    Args:
        platform: Platform enum
    """
    return PLATFORM_LISTENER_COMPATIBILITY.get(platform, [])


def get_platform_specific_required_config(platform: Platform) -> list[str]:
    """Get platform-specific required configuration paths.

    Args:
        platform: Platform enum

    Returns:
        List of additional required config paths for the platform
    """
    if platform == Platform.PUMP_FUN:
        return []  # No additional requirements
    elif platform == Platform.LETS_BONK:
        return []  # No additional requirements yet
    else:
        return []


def print_config_summary(config: dict) -> None:
    """Print a summary of the loaded configuration with platform info."""
    platform_str = config.get("platform", "pump_fun")

    print(f"Bot name: {config.get('name', 'unnamed')}")
    print(f"Platform: {platform_str}")
    print(
        f"Listener type: {config.get('filters', {}).get('listener_type', 'not configured')}"
    )

    # Validate platform-listener combination
    try:
        platform = Platform(platform_str)
        listener_type = config.get("filters", {}).get("listener_type")
        if listener_type and not validate_platform_listener_combination(
            platform, listener_type
        ):
            print(
                f"WARNING: Listener '{listener_type}' may not be compatible with platform '{platform_str}'"
            )
    except ValueError:
        print(f"WARNING: Invalid platform '{platform_str}'")

    trade = config.get("trade", {})
    print("Trade settings:")
    print(f"  - Buy amount: {trade.get('buy_amount', 'not configured')} SOL")
    print(f"  - Buy slippage: {trade.get('buy_slippage', 'not configured') * 100}%")
    print(
        f"  - Extreme fast mode: {'enabled' if trade.get('extreme_fast_mode') else 'disabled'}"
    )

    fees = config.get("priority_fees", {})
    print("Priority fees:")
    if fees.get("enable_dynamic"):
        print("  - Dynamic fees enabled")
    elif fees.get("enable_fixed"):
        print(
            f"  - Fixed fee: {fees.get('fixed_amount', 'not configured')} microlamports"
        )

    print("Configuration loaded successfully!")


def validate_all_platform_configs(config_dir: str = "bots") -> dict[str, Any]:
    """Validate all bot configurations in a directory.

    Args:
        config_dir: Directory containing bot config files

    Returns:
        Dictionary with validation results
    """
    results = {
        "valid_configs": [],
        "invalid_configs": [],
        "platform_distribution": {},
        "listener_distribution": {},
    }

    config_files = list(Path(config_dir).glob("*.yaml"))

    for config_file in config_files:
        try:
            config = load_bot_config(config_file)
            platform = get_platform_from_config(config)
            listener_type = config.get("filters", {}).get("listener_type", "unknown")

            results["valid_configs"].append(
                {
                    "file": config_file,
                    "name": config.get("name"),
                    "platform": platform.value,
                    "listener": listener_type,
                    "enabled": config.get("enabled", True),
                }
            )

            # Track distributions
            platform_key = platform.value
            results["platform_distribution"][platform_key] = (
                results["platform_distribution"].get(platform_key, 0) + 1
            )
            results["listener_distribution"][listener_type] = (
                results["listener_distribution"].get(listener_type, 0) + 1
            )

        except Exception as e:
            results["invalid_configs"].append({"file": config_file, "error": str(e)})

    return results


if __name__ == "__main__":
    # Example usage with platform configuration validation
    import sys

    if len(sys.argv) > 1:
        config_path = sys.argv[1]
        try:
            config = load_bot_config(config_path)
            print_config_summary(config)

            platform = get_platform_from_config(config)
            print(f"Detected platform: {platform}")
            print(
                f"Supported listeners for this platform: {get_supported_listeners_for_platform(platform)}"
            )
        except Exception as e:
            print(f"Configuration error: {e}")
    else:
        # Validate all configs in bots directory
        results = validate_all_platform_configs()
        print("Configuration validation results:")
        print(f"Valid configs: {len(results['valid_configs'])}")
        print(f"Invalid configs: {len(results['invalid_configs'])}")
        print(f"Platform distribution: {results['platform_distribution']}")
        print(f"Listener distribution: {results['listener_distribution']}")

        if results["invalid_configs"]:
            print("\nInvalid configurations:")
            for invalid in results["invalid_configs"]:
                print(f"  {invalid['file']}: {invalid['error']}")
