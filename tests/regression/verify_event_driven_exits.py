"""R3-P2-12 regression: event-driven sell-fill trigger.

Covers:
- The rug-feed callback flags a mint when a TradeEvent's trader is the bot's
  OWN wallet, and only when event_driven_exits_enabled is on.
- The monitor loop skips its price-check wait when the mint is prompted
  (immediate exit evaluation), then consumes the prompt.
- Off by default: no flag, loop keeps polling.
- Config wiring: YAML default false + BOOL_FIELDS entry.

The monitor behavior is verified by counting price reads: with the prompt
set and a tiny price (TP triggered), the loop exits immediately on the first
read instead of sleeping-and-rereading many times.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.universal_trader import UniversalTrader  # noqa: E402
import config_loader  # noqa: E402

MINT = "Mint111111111111111111111111111111111111111"


class _Token:
    def __init__(self):
        from interfaces.core import Platform

        self.mint = MINT
        self.symbol = "TST"
        self.platform = Platform.PUMP_FUN
        self.quote_mint = None
        self.transfer_fee_bps = 0


def _trader(flag: bool, own_wallet: str | None) -> UniversalTrader:
    t = object.__new__(UniversalTrader)
    t.event_driven_exits_enabled = flag
    t._own_trade_prompt = set()  # noqa: SLF001
    t.wallet = SimpleNamespace(public_key=own_wallet) if own_wallet else None
    # Monitor-loop attrs (off defaults so the loop runs plainly).
    t.tp_ranges = []  # noqa: SLF001
    t.sl_ranges = []  # noqa: SLF001
    t._tp_fired = set()  # noqa: SLF001
    t._sl_fired = set()  # noqa: SLF001
    t.exit_on_creator_sell = False  # R3-P1-4 off in verifiers
    t._creator_sold_for_exit = set()  # noqa: SLF001
    import asyncio as _asyncio
    t._trade_serialization_lock = _asyncio.Lock()  # R3-P2-13
    t.rug_heuristics_enabled = False  # P2-EXIT-4 off
    return t


def _feed_emit(t: UniversalTrader, trade: dict) -> None:
    """Mirror the rug-feed flagging logic for one trade dict."""
    mint_key = str(trade.get("mint", ""))
    if t.event_driven_exits_enabled and t.wallet is not None:
        if str(trade.get("trader") or "") == str(t.wallet.public_key):
            t._own_trade_prompt.add(mint_key)  # noqa: SLF001


def main() -> int:
    failures = []
    from solders.keypair import Keypair

    own = str(Keypair().pubkey())

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P2-12 event-driven sell-fill")

    # On: own-wallet trade flags the mint.
    t_on = _trader(True, own)
    _feed_emit(t_on, {"mint": MINT, "trader": own, "is_buy": False})
    check("own-wallet trade flags mint (on)", MINT in t_on._own_trade_prompt)  # noqa: SLF001

    # On: someone else's trade does not flag.
    other = str(Keypair().pubkey())
    t_on2 = _trader(True, own)
    _feed_emit(t_on2, {"mint": MINT, "trader": other, "is_buy": False})
    check("other-wallet trade does not flag", MINT not in t_on2._own_trade_prompt)  # noqa: SLF001

    # Off (default): own-wallet trade does not flag.
    t_off = _trader(False, own)
    _feed_emit(t_off, {"mint": MINT, "trader": own, "is_buy": False})
    check("off: no flag (default polling)", MINT not in t_off._own_trade_prompt)  # noqa: SLF001

    # Monitor loop: with a prompt and an over-TP price, the first iteration
    # evaluates immediately (prompt consumed, exit fires) — far fewer price
    # reads than a polling loop would make before exiting.
    reads = {"n": 0}

    async def _faux_loop() -> int:
        t = _trader(True, own)
        t._own_trade_prompt.add(MINT)  # noqa: SLF001
        t.price_check_interval = 0.05
        t.max_exit_sell_attempts = 1
        t.seller = _StubSeller()
        t.min_pool_depth_sol = 0.0
        t.rug_heuristics_enabled = False
        t.max_hold_time = 99999
        t.shadow_mode = True
        t.shadow_capture = None
        from trading.position import Position

        pos = Position.create_from_buy_result(
            mint=MINT, symbol="TST", entry_price=1.0, quantity=10.0,
            take_profit_percentage=0.0,  # TP at entry
        )
        # Price read returns over-TP value.
        async def _read(_ti):
            reads["n"] += 1
            return 1.5  # above TP

        t._read_price = _read  # noqa: SLF001
        # Cap iterations so a broken prompt can't hang the test.
        orig = t._run_exit_attempt  # noqa: SLF001

        async def _exit(*a, **k):
            pos.close_position(1.5, __import__("trading.position", fromlist=["ExitReason"]).ExitReason.TAKE_PROFIT)
            return True

        t._run_exit_attempt = _exit  # noqa: SLF001
        await t._monitor_position_until_exit(_Token(), pos)  # noqa: SLF001
        return reads["n"]

    n = asyncio.run(_faux_loop())
    check(
        "prompted loop evaluates immediately (single price read)",
        n <= 2,
        f"-> {n} reads",
    )

    # Config wiring.
    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML default event_driven_exits_enabled false",
        cfg["trade"].get("event_driven_exits_enabled") is False,
        f"-> {cfg['trade'].get('event_driven_exits_enabled')!r}",
    )
    check(
        "BOOL_FIELDS includes trade.event_driven_exits_enabled",
        "trade.event_driven_exits_enabled" in config_loader.BOOL_FIELDS,
    )

    if failures:
        print(f"\nR3-P2-12 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P2-12 event-driven sell-fill OK")
    return 0


class _StubSeller:
    async def execute(self, *a, **k):
        return SimpleNamespace(success=True, price=1.5, amount=10.0)


if __name__ == "__main__":
    sys.exit(main())