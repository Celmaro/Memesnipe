"""P2-STRUC-2 regression: creator-PnL persistent blacklist.

Reference behavior (trade_logger.rs:32-88): every trade outcome is appended
to a ledger and creators whose net history is negative go into a persistent
skip list. Guards:
1. The SQLite store accumulates trades/wins/net PnL per creator and survives
   reopen (persistence is the whole point of "persistent").
2. is_blacklisted requires BOTH a track record (>= min_trades) AND net PnL at
   or below the threshold — one bad trade never bans a creator.
3. The skip decision is wired into _skip_reason and fails open when the
   feature is off (store is None).
4. Config keys exist in YAML, BOOL_FIELDS and validation rules.
"""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from screening.creator_pnl_store import CreatorPnlStore

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "creator_pnl.db"
    store = CreatorPnlStore(tmp)

    # 1. Fresh store: unknown creator is not blacklisted.
    check("unseen creator not blacklisted", not store.is_blacklisted("c1"))

    # 2. Accumulation: 5 losses -> blacklisted at min_trades=3.
    for _ in range(5):
        store.record_trade("c1", -0.05)
    trades, net = store.get_stats("c1")
    check("trades accumulate", trades == 5, f"got {trades}")
    check("net accumulates negative", abs(net + 0.25) < 1e-9, f"got {net}")
    check("5 losses blacklisted", store.is_blacklisted("c1", min_trades=3))

    # 3. One bad trade never bans: 1 loss, 0 wins.
    store.record_trade("c2", -0.5)
    check(
        "single loss not blacklisted at min_trades=3",
        not store.is_blacklisted("c2", min_trades=3),
    )

    # 4. Profitable creator never blacklisted even with many trades.
    for _ in range(10):
        store.record_trade("c3", 0.1)
    check(
        "profitable creator not blacklisted",
        not store.is_blacklisted("c3", min_trades=3),
    )

    # 5. Threshold behavior: net exactly <= max_net_sol counts as blacklisted.
    for _ in range(3):
        store.record_trade("c4", 0.0)
    check(
        "breakeven creator blacklisted at max_net_sol=0",
        store.is_blacklisted("c4", min_trades=3, max_net_sol=0.0),
    )

    # 6. Persistence across reopen (the "persistent" in the feature name).
    store2 = CreatorPnlStore(tmp)
    check(
        "stats persist across reopen",
        store2.get_stats("c1") == store.get_stats("c1"),
    )

    # 7. _skip_reason integration: store None (feature off) fails open.
    from trading.universal_trader import UniversalTrader

    t = SimpleNamespace(
        blocked_creators=set(),
        creator_pnl_store=None,
        allowed_quote_mints=None,
        quote_amounts={
            "So11111111111111111111111111111111111111112": 0.01
        },
        max_transfer_fee_bps=None,
    )
    token = SimpleNamespace(
        creator="c1", user=None, quote_mint="So11111111111111111111111111111111111111112"
    )
    reason = UniversalTrader._skip_reason(t, token)
    check("feature off fails open", reason is None, f"got {reason!r}")

    # 8. _skip_reason integration: store present + blacklisted creator -> skip.
    t2 = SimpleNamespace(
        blocked_creators=set(),
        creator_pnl_store=store,
        creator_pnl_min_trades=3,
        creator_pnl_max_net_sol=0.0,
        allowed_quote_mints=None,
        quote_amounts={
            "So11111111111111111111111111111111111111112": 0.01
        },
        max_transfer_fee_bps=None,
    )
    reason2 = UniversalTrader._skip_reason(t2, token)
    check(
        "blacklisted creator skipped",
        reason2 is not None and "PnL blacklist" in reason2,
        f"got {reason2!r}",
    )

    # 9. Config wiring.
    import yaml

    import config_loader

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML declares creator_pnl_blacklist_enabled",
        "creator_pnl_blacklist_enabled" in cfg.get("filters", {}),
    )
    check(
        "YAML default off",
        cfg["filters"].get("creator_pnl_blacklist_enabled", False) is False,
    )
    check(
        "BOOL_FIELDS includes it",
        "filters.creator_pnl_blacklist_enabled" in config_loader.BOOL_FIELDS,
    )
    check(
        "min_trades validation rule exists",
        any(
            r[0] == "filters.creator_pnl_min_trades"
            for r in config_loader.CONFIG_VALIDATION_RULES
        ),
    )

    print()
    if FAILURES:
        print(f"P2-STRUC-2 FAILED: {len(FAILURES)} check(s) — {FAILURES}")
        return 1
    print("P2-STRUC-2 PASSED: creator-PnL blacklist verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())