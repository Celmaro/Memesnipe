"""R3-P2-9 regression: consensus-reset filter semantics (VERIFY).

The reference behavior (solana-trading-bot-v3 bot.ts:357-391): on any failed
check `matchCount` resets to 0; only N CONSECUTIVE gate passes allow the
buy. Memesnipe's `_handle_token` implements the same rule by aborting the
token on ANY failed recheck — a fail-then-pass sequence can never reach the
buy (each failure returns without buying, so the next pass starts a fresh
window). This verifier proves that contract:

- N=2: pass -> pass reaches the buy.
- N=2: pass -> FAIL aborts before the buy (buyer.execute never called).
- N=3: pass -> pass -> FAIL aborts (the fail resets the whole attempt).
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.universal_trader import UniversalTrader  # noqa: E402


class _Token:
    def __init__(self, quote_mint_b58: str):
        from interfaces.core import Platform
        from solders.pubkey import Pubkey

        self.mint = "Mint111111111111111111111111111111111111111"
        self.symbol = "TST"
        self.name = "Test Token"
        self.creation_timestamp = None
        self.creator_vault = None
        self.token_program_id = None
        self.is_mayhem_mode = False
        self.is_cashback_coin = False
        self.is_holder_reward = False
        self.quote_token_program_id = None
        self.virtual_quote_reserves = None
        self.associated_bonding_curve = None
        self.platform = Platform.PUMP_FUN
        self.creator = "Creator111111111111111111111111111111111"
        self.user = None
        self.quote_mint = Pubkey.from_string(quote_mint_b58)
        self.transfer_fee_bps = 0
        self.bonding_curve = "pooladdr"
        self.pool_state = None
        self.associated_bonding_curve = None
        self.uri = ""


class _Buyer:
    def __init__(self):
        self.calls = 0

    async def execute(self, _token_info):
        self.calls += 1
        return SimpleNamespace(success=True, price=1.0, amount=1.0)


def _make_trader(
    n: int, gate_results: list[str | None], quote_mint_b58: str
) -> UniversalTrader:
    from core.pubkeys import WSOL_MINT

    t = object.__new__(UniversalTrader)
    t._momentum_peak_rate = {}  # R4-P1-8 mock state
    t._momentum_peak_price = {}  # R4-P1-8 mock state
    t.fragility_scale_min = 1.0  # R4-P3-1 mock default (off)
    t.fragility_whale_threshold = 0.6  # R4-P3-1 mock default
    t.fragility_rate_threshold = 0.6  # R4-P3-1 mock default
    t.stagnation_exit_min = 0  # R4-P1-9 mock default (off)
    t.stagnation_band_pct = 0.02  # R4-P1-9 mock default
    t.max_wash_share = 0.0  # R4-P1-6 mock default
    from screening.wash_lpi import WashLpiDetector as _WLD  # R4-P1-6 mock default
    t._wash_lpi = _WLD()  # R4-P1-6 mock default
    t.max_gini_coefficient = 0.0  # R4-P1-10 mock default
    t.rug_score_threshold = 0.0  # R4-P1-1 mock default
    t.factory_deploy_rate = 0.0  # R4-P1-2 mock default
    t.max_coordinated_supply_share = 0.0  # R4-P1-3 mock default
    t.exit_mode = "tp_sl"  # R4-P1-7 mock default
    t.strategies_ledger = None  # R4-P1-7 mock default
    t.contention_enabled = False  # R4-P0-3 mock default
    t.contention_multiplier = 2.5
    t.contention_creates_per_min = 20.0
    t.contention_window_s = 60.0
    t.max_fee_fraction_of_position = 0.005
    t._jito_executor = None
    from interfaces.core import Platform

    from interfaces.core import Platform

    t.platform = Platform.PUMP_FUN
    t.consecutive_match_count = n
    t.blocked_creators = set()
    t.creator_pnl_store = None
    t.allow_mayhem_mode = True
    t.holder_reward_mode = "trade"
    t.min_unique_holders = 0
    t.max_top10_concentration = 0.0
    t.min_metadata_extensions = 0
    from screening.holder_snapshot import HolderSnapshotStore
    from screening.metadata_socials import MetadataStore

    t._holder_store = HolderSnapshotStore()  # noqa: SLF001
    t._metadata_store = MetadataStore()  # noqa: SLF001
    t.max_transfer_fee_bps = 0
    t.allowed_quote_mints = None
    t.quote_amounts = {WSOL_MINT: 1.0}
    t.gate_recheck_interval = 0.01
    t.extreme_fast_mode = False
    t.quote_amounts = {WSOL_MINT: 1.0}
    t.buyer = _Buyer()
    t.shadow_mode = False
    t.shadow_capture = None
    from trading.risk_guard import RiskTracker
    t.risk_tracker = RiskTracker()  # R3-P2-10 off in verifiers
    import asyncio as _asyncio
    t._trade_serialization_lock = _asyncio.Lock()  # R3-P2-13
    t.event_driven_exits_enabled = False  # R3-P2-12 off in verifiers
    t._own_trade_prompt = set()  # noqa: SLF001
    t.seller = None
    t.platform_implementations = None
    t._capture_detect = lambda *_a, **_k: None  # noqa: SLF001
    t._save_token_info = _noop  # consensus test ignores persistence
    t._handle_successful_buy = _noop  # prevents further machinery
    t._handle_failed_buy = _noop  # noqa: SLF001
    t._buy_reservations = set()  # noqa: SLF001 — C3.4 pre-buy reservation
    t._active_exit_mints = set()  # noqa: SLF001 — C3.4 open-position window
    t._unsold_persist_path = None  # noqa: SLF001 — C7.2 persistence off in tests
    t._unsold_positions = {}  # noqa: SLF001
    t.wait_time_before_new_token = 0
    t.wait_time_after_creation = 0  # skip the stabilization sleep
    t.yolo_mode = False
    t.quote_mint = WSOL_MINT
    idx = {"i": 0}

    async def gate(_token_info):
        result = gate_results[min(idx["i"], len(gate_results) - 1)]
        idx["i"] += 1
        return result

    t._curve_gate_reason = gate  # noqa: SLF001
    return t


async def _noop(*_a, **_k):
    return None


def main() -> int:
    failures = []
    from core.pubkeys import WSOL_MINT

    wsol = str(WSOL_MINT)

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P2-9 consensus-reset filter semantics")

    # N=2 all-pass reaches the buy.
    t1 = _make_trader(2, [None, None], wsol)
    asyncio.run(t1._handle_token(_Token(wsol)))
    check("N=2 pass-pass buys", t1.buyer.calls == 1, f"-> {t1.buyer.calls}")

    # N=2 pass-FAIL aborts before the buy.
    t2 = _make_trader(2, [None, "reject on recheck"], wsol)
    asyncio.run(t2._handle_token(_Token(wsol)))
    check(
        "N=2 pass-fail never buys",
        t2.buyer.calls == 0,
        f"-> {t2.buyer.calls}",
    )

    # N=3 pass-pass-FAIL aborts (fail resets the whole window).
    t3 = _make_trader(3, [None, None, "reject on third"], wsol)
    asyncio.run(t3._handle_token(_Token(wsol)))
    check(
        "N=3 pass-pass-fail never buys",
        t3.buyer.calls == 0,
        f"-> {t3.buyer.calls}",
    )

    # N=1 (default) single pass reaches the buy.
    t4 = _make_trader(1, [None], wsol)
    asyncio.run(t4._handle_token(_Token(wsol)))
    check("N=1 single pass buys", t4.buyer.calls == 1, f"-> {t4.buyer.calls}")

    if failures:
        print(f"\nR3-P2-9 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P2-9 consensus-reset filter semantics OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())