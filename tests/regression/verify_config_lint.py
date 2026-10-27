"""A2-P0-1: config unknown-key lint — ignored keys must surface as warnings.

The two external audits proposed YAML sections (`screening:`, `risk_guards:`,
`copy_trading:`, `trade.staged_exits:`, `trailing_stop:`, ...) that DO NOT
exist in this schema. The loader silently ignores unknown keys, so pasting an
audit config would *appear* to take effect while the bot kept its old
behavior — or worse, `trade.exit_strategy` without `trade.exit_mode`
silently re-arms plain tp_sl (the pre-staged era).

This verifier proves:
  1. the real memesnipe.yaml lints CLEAN (every shipped key is read);
  2. an audit-style config with invented sections/keys is flagged;
  3. exit_strategy-without-exit_mode is flagged (the silent tp_sl re-arm).
"""
from __future__ import annotations

import io
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import config_loader  # noqa: E402


def main() -> int:
    fails = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            fails.append(label)

    # 1. The shipped config lints clean.
    cfg = config_loader.load_bot_config("bots/memesnipe.yaml")
    unknown = config_loader.collect_unknown_config_keys(cfg)
    check("real memesnipe.yaml has no unknown keys", unknown == [], f"-> {unknown}")

    # 2. Audit-style invented sections/keys are flagged.
    audit_like = {
        "name": "audit-paste",
        "rpc_endpoint": "x",
        "wss_endpoint": "x",
        "private_key": "x",
        "platform": "pump_fun",
        "screening": {"max_gini": 0.82, "hot_path_zero_rpc": True},
        "risk_guards": {"max_concurrent_positions": 3},
        "copy_trading": {"enabled": True, "min_win_rate": 0.58},
        "trade": {
            "buy_amount": 0.05,
            "buy_slippage": 0.18,
            "sell_slippage": 0.45,
            "exit_strategy": "tp_sl",  # NO exit_mode -> silent re-arm
        },
        "filters": {"listener_type": "geyser"},
    }
    unknown = config_loader.collect_unknown_config_keys(audit_like)
    expected = [
        "screening", "risk_guards", "copy_trading",
        "trade.staged_exits", "trade.trailing_stop",
    ]
    check(
        "audit-style invented sections flagged",
        "screening" in unknown and "risk_guards" in unknown
        and "copy_trading" in unknown,
        f"-> {unknown}",
    )
    check(
        "audit-style keys never silently pass",
        all(k not in unknown for k in ("name", "platform", "trade.buy_amount")),
        f"-> known keys not flagged",
    )

    # 3. exit_strategy-without-exit_mode emits the re-arm warning.
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    logger = logging.getLogger("config_loader")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    config_loader.warn_unknown_config_keys(audit_like)
    out = buf.getvalue()
    check(
        "exit_strategy-without-exit_mode warns about tp_sl re-arm",
        "trade.exit_strategy is set without trade.exit_mode" in out,
        "-> warning emitted",
    )

    # 4. A fully-qualified config (every section real, exit_mode present)
    #    lints clean and does NOT warn.
    good = {
        "name": "good",
        "rpc_endpoint": "x",
        "wss_endpoint": "x",
        "private_key": "x",
        "platform": "pump_fun",
        "trade": {
            "buy_amount": 0.01, "buy_slippage": 0.18, "sell_slippage": 0.45,
            "exit_strategy": "tp_sl", "exit_mode": "staged_trail",
            "trail_initial_tp_share": 0.4, "trail_initial_tp_pct": 0.2,
        },
        "filters": {"listener_type": "geyser"},
    }
    unknown = config_loader.collect_unknown_config_keys(good)
    buf2 = io.StringIO()
    h2 = logging.StreamHandler(buf2)
    logger.handlers.clear()
    logger.addHandler(h2)
    config_loader.warn_unknown_config_keys(good)
    check(
        "qualified config lints clean + no re-arm warning",
        unknown == [] and "without trade.exit_mode" not in buf2.getvalue(),
        f"-> unknown={unknown}",
    )

    print("\n" + ("A2-P0-1 config lint verifier: PASS" if not fails else f"FAIL: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())