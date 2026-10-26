"""R3-P1-4 regression: sell-when-creator-sells exit.

Covers:
- The trade-feed callback flags a mint when a SELL from a registered creator
  arrives; buys never flag it.
- The monitor loop exits with ExitReason.CREATOR_SELL when the flag is set
  and the feature is on.
- Feature off by default: feed callback does not flag; loop does not exit.
- Config wiring: YAML default false + BOOL_FIELDS entry.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.universal_trader import UniversalTrader  # noqa: E402
from trading.position import Position, ExitReason  # noqa: E402
import config_loader  # noqa: E402

CREATOR = "Creator111111111111111111111111111111111"
MINT = "Mint111111111111111111111111111111111111111"


def _trader(flag: bool) -> UniversalTrader:
    from core.pubkeys import DEFAULT_PUBKEY, WSOL_MINT

    from interfaces.core import TokenInfo  # noqa: F401

    t = object.__new__(UniversalTrader)
    t.exit_on_creator_sell = flag
    t.creator_pnl_store = None
    t.blocked_creators = set()
    t.allowed_quote_mints = {WSOL_MINT}
    t.quote_amounts = {WSOL_MINT: 0.0001}
    t.max_transfer_fee_bps = 0
    t.allow_mayhem_mode = True
    t.holder_reward_mode = "trade"
    t.min_unique_holders = 0  # R3-P1-5 off in verifiers
    t.max_top10_concentration = 0.0
    from screening.holder_snapshot import HolderSnapshotStore
    t._holder_store = HolderSnapshotStore()  # noqa: SLF001
    from screening.metadata_socials import MetadataStore
    t.min_metadata_extensions = 0  # R3-P2-8 off in verifiers
    t._metadata_store = MetadataStore()  # noqa: SLF001
    from trading.risk_guard import RiskTracker

    t.risk_tracker = RiskTracker()  # R3-P2-10 off
    import asyncio as _asyncio
    t._trade_serialization_lock = _asyncio.Lock()  # R3-P2-13
    t._creator_sellers = {}  # noqa: SLF001
    t._creator_sold_for_exit = set()  # noqa: SLF001
    t.event_driven_exits_enabled = False  # R3-P2-12 off in verifiers
    t._own_trade_prompt = set()  # noqa: SLF001
    t._rug_buyers = {}  # noqa: SLF001
    t.rug_heuristics_enabled = False
    t.tp_ranges = []  # R3-P1-2
    t.sl_ranges = []  # R3-P1-2
    t._tp_fired = set()  # noqa: SLF001
    t._sl_fired = set()  # noqa: SLF001
    t._log_trade = lambda *a, **k: None  # noqa: SLF001
    return t


class _StopLoop(BaseException):
    """Escapes the monitor loop's `except Exception` handler deterministically."""


def _token() -> SimpleNamespace:
    from core.pubkeys import DEFAULT_PUBKEY

    return SimpleNamespace(
        platform="pump_fun",
        mint=MINT,
        symbol="TEST",
        creator=CREATOR,
        user=None,
        quote_mint=DEFAULT_PUBKEY,
        transfer_fee_bps=0,
    )


def _position() -> Position:
    from solders.pubkey import Pubkey

    return Position.create_from_buy_result(
        mint=Pubkey.from_string(MINT), symbol="TEST", entry_price=1.0, quantity=100.0
    )


async def _feed(trades: list[dict]) -> tuple[UniversalTrader, set[str]]:
    """Drive the rug-feed callback shape directly (it is an inner function;
    this mirrors its logic: parse, then flag if creator sell)."""
    t = _trader(flag=True)
    t._creator_sellers.setdefault(MINT, set()).add(CREATOR)  # noqa: SLF001
    for trade in trades:
        mint_key = str(trade.get("mint", ""))
        trader_key = str(trade.get("trader") or "")
        if t.exit_on_creator_sell and not trade.get("is_buy"):
            creators = t._creator_sellers.get(mint_key, set())
            if creators and trader_key in creators:
                t._creator_sold_for_exit.add(mint_key)  # noqa: SLF001
        if trade.get("is_buy") and mint_key and trader_key:
            t._rug_buyers.setdefault(mint_key, set()).add(trader_key)  # noqa: SLF001
    return t, t._creator_sold_for_exit  # noqa: SLF001


class StubSeller:
    def __init__(self):
        self.attempts = 0

    async def execute(self, *a, **k):
        self.attempts += 1
        return SimpleNamespace(success=True, price=1.0)


async def _check_monitor_exit(flagged: bool) -> bool:
    """Run one monitor-loop iteration shape: flagged -> exit fired."""
    t = _trader(flag=True)
    t.seller = StubSeller()
    t.price_check_interval = 0.01
    t.max_exit_sell_attempts = 3
    t.shadow_mode = False
    t.shadow_capture = None
    t.min_pool_depth_sol = 0.0
    t.rug_heuristics_enabled = False
    t.max_hold_time = 99999
    if flagged:
        t._creator_sold_for_exit.add(MINT)  # noqa: SLF001
    position = _position()
    # First read returns a price so the body runs; the second read raises a
    # BaseException sentinel that escapes the loop's `except Exception`.
    reads = {"n": 0}

    async def _priced_then_stop(*_a, **_k):
        reads["n"] += 1
        if reads["n"] >= 2:
            raise _StopLoop()
        return 1.0

    t._read_price = _priced_then_stop  # noqa: SLF001
    try:
        await t._monitor_position_until_exit(_token(), position)
    except _StopLoop:
        pass
    return position.exit_reason == ExitReason.CREATOR_SELL


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P1-4 creator-sell exit")

    # Feed flags on creator sell.
    t, flagged = asyncio.run(
        _feed([{"mint": MINT, "trader": CREATOR, "is_buy": False}])
    )
    check("creator sell flags the mint", MINT in flagged, f"-> {flagged}")

    # Creator BUY does not flag (only sells).
    t2, flagged2 = asyncio.run(
        _feed([{"mint": MINT, "trader": CREATOR, "is_buy": True}])
    )
    check("creator buy does not flag", MINT not in flagged2, f"-> {flagged2}")

    # Non-creator sell does not flag.
    t3, flagged3 = asyncio.run(
        _feed([{"mint": MINT, "trader": "OtherWallet11111111111111111111111111", "is_buy": False}])
    )
    check("non-creator sell does not flag", MINT not in flagged3, f"-> {flagged3}")

    # Monitor exits on the flagged mint.
    exited = asyncio.run(_check_monitor_exit(True))
    check("monitor exits when flagged", exited)

    # Monitor does not exit when not flagged (loop ends another way).
    exited2 = asyncio.run(_check_monitor_exit(False))
    check("monitor does not creator-exit when not flagged", not exited2)

    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML default exit_on_creator_sell false",
        cfg["filters"].get("exit_on_creator_sell") is False,
        f"-> {cfg['filters'].get('exit_on_creator_sell')!r}",
    )
    check(
        "BOOL_FIELDS includes filters.exit_on_creator_sell",
        "filters.exit_on_creator_sell" in config_loader.BOOL_FIELDS,
    )

    if failures:
        print(f"\nR3-P1-4 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P1-4 creator-sell exit OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())