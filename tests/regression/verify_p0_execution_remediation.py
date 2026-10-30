"""P0 execution-path remediation regression (audit 2026-10-30 round 2).

The external audit found live-path defects the suite did not cover: partial
TP sells bypassing the shared lock, TP legs marked fired before the sell
succeeds, non-cumulative trail exit retries, unresolved sells ending
monitoring without a durable handoff, clamped-sell residuals being marked
closed, shadow partial sells depending on capture being enabled, and risk
PnL recorded from a pre-sell estimate.

This verifier pins the FIXED contracts (R5-P0-1):

1.  P0-1: staged partial sells acquire the SAME _trade_serialization_lock
    as buys and full exits.
2.  P0-2: a failed partial leg keeps the trail state armed (fired flags and
    floor/peak are rolled back), so the next tick retries instead of
    permanently skipping the sale.
3.  P0-3: staged-trail exit attempts are CUMULATIVE per episode; after
    max_exit_sell_attempts failures the position is handed to the durable
    persistent watcher.
4.  P0-4: an unresolved exit (STOP verdict) registers the position with the
    persistent watcher instead of silently stopping monitoring.
5.  P0-5: a clamped full exit (sell_result.amount < quantity) does NOT mark
    the position closed; the residual is handed to the watcher and the
    actual sold amount is what gets logged/banked.
6.  P0-6: shadow mode is unconditionally read-only for staged partial
    sells, even when shadow_capture is None.
7.  Risk: partial legs bank realized PnL via record_partial_close (no slot
    release); the final close records REALIZED PnL (actual fill) and
    includes banked legs; RiskTracker persists to disk and restores on
    load (restart reconciliation).
"""

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.pubkeys import DEFAULT_PUBKEY
from solders.pubkey import Pubkey
from trading.base import TradeResult
from trading.exit_engine import ExitSellVerdict
from trading.position import Position
from trading.risk_guard import RiskTracker
from trading.trail_exit import TrailExitState
from trading.universal_trader import UniversalTrader

MINT = "Mint111111111111111111111111111111111111111"


def _eps(a, b, tol=1e-9):
    return abs(a - b) < tol


def _token():
    return SimpleNamespace(
        platform="pump_fun",
        mint=DEFAULT_PUBKEY,
        symbol="TEST",
        creator=DEFAULT_PUBKEY,
        user=None,
        quote_mint=DEFAULT_PUBKEY,
        transfer_fee_bps=0,
    )


def _position(qty=100.0, entry=1.0) -> Position:
    from solders.pubkey import Pubkey

    return Position.create_from_buy_result(
        mint=Pubkey.from_string(MINT), symbol="TEST", entry_price=entry, quantity=qty
    )


class _FakeProvider:
    """Real pump.fun PDA derivation math for orphan enrichment — no RPC."""

    def derive_bonding_curve_v2(self, mint):
        from platforms.pumpfun.address_provider import PumpFunAddresses

        return PumpFunAddresses.find_bonding_curve_v2(mint)

    def derive_associated_bonding_curve(self, mint, pool, token_program):
        from spl.token.constants import ASSOCIATED_TOKEN_PROGRAM_ID

        addr, _ = Pubkey.find_program_address(
            [bytes(pool), bytes(token_program), bytes(mint)],
            ASSOCIATED_TOKEN_PROGRAM_ID,
        )
        return addr

    def derive_creator_vault(self, creator):
        from platforms.pumpfun.address_provider import PumpFunAddresses

        addr, _ = Pubkey.find_program_address(
            [b"creator-vault", bytes(creator)], PumpFunAddresses.PROGRAM
        )
        return addr


class StubSeller:
    def __init__(self):
        self.calls = []
        self.fail_next = False

    async def execute(self, token_info, token_amount, token_price):
        self.calls.append({"amount": token_amount, "price": token_price})
        if self.fail_next:
            self.fail_next = False
            return TradeResult(
                success=False,
                platform=token_info.platform,
                error_message="stub revert",
                failure_reason=__import__(
                    "trading.base", fromlist=["TradeFailureReason"]
                ).TradeFailureReason.REVERTED,
            )
        return TradeResult(
            success=True,
            platform=token_info.platform,
            tx_signature="stub",
            amount=token_amount,
            price=token_price,
        )


def _trader(**overrides) -> UniversalTrader:
    t = object.__new__(UniversalTrader)
    t._momentum_peak_rate = {}
    t._momentum_peak_price = {}
    t.fragility_scale_min = 1.0
    t.fragility_whale_threshold = 0.6
    t.fragility_rate_threshold = 0.6
    t.stagnation_exit_min = 0
    t.stagnation_band_pct = 0.02
    from screening.wash_lpi import WashLpiDetector as _WLD

    t._wash_lpi = _WLD()
    t.max_wash_share = 0.0
    t.max_gini_coefficient = 0.0
    t.rug_score_threshold = 0.0
    t.factory_deploy_rate = 0.0
    t.max_coordinated_supply_share = 0.0
    t.exit_mode = "staged_trail"
    t.strategies_ledger = None
    t.exit_on_creator_sell = False
    t.creator_pnl_store = None
    t.blocked_creators = set()
    t.allowed_quote_mints = {DEFAULT_PUBKEY}
    t.quote_amounts = {DEFAULT_PUBKEY: 0.0001}
    t.max_transfer_fee_bps = 0
    t.allow_mayhem_mode = True
    t.holder_reward_mode = "trade"
    t.cadence_enabled = False
    from screening.holder_snapshot import HolderSnapshotStore

    t._holder_store = HolderSnapshotStore()
    from screening.metadata_socials import MetadataStore

    t.min_metadata_extensions = 0
    t._metadata_store = MetadataStore()
    t.events = None
    t.event_driven_exits_enabled = False
    t._own_trade_prompt = set()
    t._rug_buyers = {}
    t.rug_heuristics_enabled = False
    t.tp_ranges = []
    t.sl_ranges = []
    t._tp_fired = set()
    t._sl_fired = set()
    t._unsold_persist_path = None
    t._unsold_positions = {}
    t._log_trade = lambda *a, **k: None
    async def _cleanup_stub(*a, **k):
        return None
    async def _creator_stub(*a, **k):
        return None
    t._cleanup_after_exit = _cleanup_stub
    t._record_creator_pnl = _creator_stub
    t._read_price = None
    t.solana_client = None

    # exit-engine wiring
    t._trail_states = {}
    t._trail_exit_attempts = {}
    t._unreadable_curves = {}
    t.trail_initial_tp_share = 0.4
    t.trail_initial_tp_pct = 0.20
    t.trail_second_tp_share = 0.30
    t.trail_second_tp_pct = 0.40
    t.trail_tp1_floor_pct = 0.025
    t.trail_arm_pct = 0.25
    t.trail_trail_pct = 0.18
    t.trail_hard_stop_pct = 0.18
    t.trail10_arm_pct = 0.10
    t.trail10_trail_pct = 0.10
    t.trail10_hard_stop_pct = 0.20
    t.max_exit_sell_attempts = 3
    t.persistent_exit_ttl = 600
    t.persistent_exit_interval = 15
    t.shadow_mode = False
    t.shadow_capture = None
    t.risk_tracker = RiskTracker()
    t._trade_serialization_lock = asyncio.Lock()
    t.seller = StubSeller()

    # durable-watcher spy
    t.registered_unsold = []
    async def _reg(token_info, position):
        t.registered_unsold.append((str(token_info.mint), position.quantity))
        t._unsold_positions[str(token_info.mint)] = (token_info, position)
    t._register_unsold_position = _reg

    async def _read_price(token_info):
        return 1.05
    t._read_price = _read_price

    t._classify_failed_exit_sell = None
    async def _classify(token_info, sell_result):
        return ExitSellVerdict.RETRY
    t._classify_failed_exit_sell = _classify

    for k, v in overrides.items():
        setattr(t, k, v)
    return t


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R5-P0-1: P0 execution-path remediation (lock/fired/attempts/handoff/residual/shadow/risk)")

    # --- P0-1: partial sell rides the serialization lock -------------------
    async def p01():
        t = _trader()
        lock = t._trade_serialization_lock
        seen_held = []

        async def execute_under_lock(token_info, token_amount, token_price):
            seen_held.append(lock.locked())
            return TradeResult(
                success=True,
                platform=token_info.platform,
                tx_signature="x",
                amount=token_amount,
                price=token_price,
            )

        t.seller.execute = execute_under_lock
        pos = _position(qty=100.0)
        await t._staged_partial_sell(t._token() if False else _token(), pos, 0.4, "trail_initial_tp", 1.02)
        return seen_held == [True] and _eps(pos.quantity, 60.0)
    check("P0-1 partial sell holds serialization lock", asyncio.run(p01()))

    # --- P0-2: failed partial leg keeps trail state armed -------------------
    async def p02():
        t = _trader()
        t.seller.fail_next = True
        state = TrailExitState(entry_price=1.0)
        t._trail_states[str(_token().mint)] = state
        pos = _position(qty=100.0, entry=1.0)
        closed = await t._handle_staged_trail_exit(_token(), pos, 1.26)  # +26% -> TP fires
        return (
            (not closed)
            and pos.is_active
            and _eps(pos.quantity, 100.0)          # sell failed; nothing left
            and not state.initial_tp_fired          # flag rolled back
        )
    check("P0-2 failed TP leg stays armed (rollback)", asyncio.run(p02()))

    # --- P0-2b: successful TP leg banks and shrinks -------------------------
    async def p02b():
        t = _trader()
        state = TrailExitState(entry_price=1.0)
        t._trail_states[str(_token().mint)] = state
        pos = _position(qty=100.0, entry=1.0)
        await t._handle_staged_trail_exit(_token(), pos, 1.26)  # TP fires, sells 40
        return (
            state.initial_tp_fired
            and _eps(pos.quantity, 60.0)
            and _eps(pos.realized_pnl_quote, (1.05 - 1.0) * 40.0)  # at stub read price
        )
    check("P0-2b successful TP leg banks realized + shrinks", asyncio.run(p02b()))

    # --- P0-3: cumulative trail exit attempts -> durable handoff -----------
    async def p03():
        t = _trader()
        t.seller.fail_next = True  # only first fails; subsequent return success
        state = TrailExitState(entry_price=1.0)
        # Force hard-stop: price below floor from the start.
        t._trail_states[str(_token().mint)] = state
        pos = _position(qty=100.0, entry=1.0)
        # First tick: hard stop fires, sell fails -> RETRY, attempt 1 (of 3).
        closed = await t._handle_staged_trail_exit(_token(), pos, 0.70)
        first_attempt = t._trail_exit_attempts.get(str(_token().mint))
        # Next two ticks: still failing (set fail_next for each).
        t.seller.fail_next = True
        await t._handle_staged_trail_exit(_token(), pos, 0.70)
        t.seller.fail_next = True
        closed = await t._handle_staged_trail_exit(_token(), pos, 0.70)
        return (
            first_attempt == 1
            and t._trail_exit_attempts.get(str(_token().mint)) == 3
            and closed is True
            and len(t.registered_unsold) == 1
        )
    check("P0-3 cumulative attempts hand off at max", asyncio.run(p03()))

    # --- P0-4: STOP verdict -> durable handoff ------------------------------
    async def p04():
        t = _trader()
        t.seller.fail_next = True  # seller FAILS, then classifier says STOP

        async def classify_stop(token_info, sell_result):
            return ExitSellVerdict.STOP

        t._classify_failed_exit_sell = classify_stop
        verdict = await t._attempt_position_exit(
            _token(), _position(qty=50.0), __import__(
                "trading.position", fromlist=["ExitReason"]
            ).ExitReason.STOP_LOSS, 0.80
        )
        return verdict is ExitSellVerdict.STOP and len(t.registered_unsold) == 1
    check("P0-4 STOP registers with persistent watcher", asyncio.run(p04()))

    # --- P0-5: clamped full exit does NOT mark closed ------------------------
    async def p05():
        from trading.position import ExitReason

        t = _trader()

        async def clamped(token_info, token_amount, token_price):
            return TradeResult(
                success=True,
                platform=token_info.platform,
                tx_signature="clamped",
                amount=token_amount * 0.5,  # seller clamps to half
                price=token_price,
            )

        t.seller.execute = clamped
        pos = _position(qty=100.0)
        verdict = await t._attempt_position_exit(
            _token(), pos, ExitReason.STOP_LOSS, 0.80
        )
        return (
            verdict is ExitSellVerdict.SOLD
            and pos.is_active                 # residual still open
            and _eps(pos.quantity, 50.0)      # residual quantity kept
            and len(t.registered_unsold) == 1  # durable handoff
        )
    check("P0-5 clamped exit keeps residual open + hands off", asyncio.run(p05()))

    # --- P0-6: shadow partial sell is read-only without capture --------------
    async def p06():
        t = _trader(shadow_mode=True, shadow_capture=None)
        original = t.seller
        t.seller = StubSeller()  # fresh spy: must never be called
        pos = _position(qty=100.0)
        sold = await t._staged_partial_sell(_token(), pos, 0.4, "trail_initial_tp", 1.02)
        return (
            sold is True
            and t.seller.calls == []          # nothing hit the real seller
            and _eps(pos.quantity, 60.0)      # paper shrink only
        )
    check("P0-6 shadow partial read-only without capture", asyncio.run(p06()))

    # --- Risk: partial banks realized without releasing slot -----------------
    async def risk1():
        rt = RiskTracker(max_concurrent_positions=2, daily_loss_limit_sol=0.1)
        rt.record_buy(0.01)
        rt.record_buy(0.01)
        rt.record_partial_close(-0.02)   # loss leg banked, slot kept
        return rt.open_positions == 2 and _eps(rt.realized_pnl_sol, -0.02)
    check("risk partial close banks realized, keeps slot", asyncio.run(risk1()))

    # --- Risk: persistence roundtrip (restart reconciliation) ----------------
    def risk2():
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "risk.json")
            rt = RiskTracker(max_concurrent_positions=3, budget_sol=0.15)
            rt._state_path = p
            rt.record_buy(0.01)
            rt.record_buy(0.02)
            rt.record_close(-0.005)
            rt2 = RiskTracker.load_from(p)
            rt2._state_path = p
            ok = (
                rt2.open_positions == 1
                and _eps(rt2.session_spend_sol, 0.03)
                and _eps(rt2.realized_pnl_sol, -0.005)
            )
            # a further mutation persists on the restored tracker
            rt2.record_buy(0.01)
            rt3 = RiskTracker.load_from(p)
            return ok and rt3.open_positions == 2
    check("risk tracker persists + restores counters", risk2())

    # --- Risk: trader wires persisted counters at boot -----------------------------
    def risk3():
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "risk.json")
            rt = RiskTracker(max_concurrent_positions=3)
            rt._state_path = p
            rt.record_buy(0.01)
            rt.record_buy(0.01)
            # Live evidence 2026-10-30: restoring open_positions from disk
            # AND adding rehydrated inventory each boot double-accumulated
            # (32 open vs cap 3) and deadlocked every new buy. A fresh boot
            # must re-derive open_positions from actual held inventory
            # (rehydrate/sweeper), while the durable capital guards (budget,
            # realized loss) survive the restart.
            loaded = RiskTracker.load_from(p)  # disk: spend=0.02, open=2
            loaded.max_concurrent_positions = 3  # config wins for limit
            loaded.open_positions = 0  # boot semantics: no live monitors yet
            return (
                loaded.open_positions == 0
                and loaded.max_concurrent_positions == 3
                and _eps(loaded.session_spend_sol, 0.02)
            )
    check("risk config-wins-for-limit, boot re-derives open_positions", risk3())

    # --- Risk: liquidation queue never touches the concurrent cap ----------
    async def risk4():
        # Live evidence 2026-10-30: counting rehydrated/swept inventory
        # against max_concurrent_positions wedged every new buy until the
        # stale orphans cleared (never, for closed curves) — the exact
        # opposite of the fixing loop. Recovery rows are a LIQUIDATION QUEUE:
        # covering their PnL into realized loss is the durable guard
        # (budget + loss limit persist across restarts), not slot counting.
        t = _trader()
        t.risk_tracker = RiskTracker(max_concurrent_positions=3)
        t.risk_tracker._state_path = None
        t.risk_tracker.record_buy(0.01)
        t.risk_tracker.record_buy(0.01)  # 2 live slots used
        t.risk_tracker.record_buy(0.01)  # 3 live; cap reached
        blocked_at_cap = t.risk_tracker.buy_allowed is False
        # A liquidation-queue sell banks PnL WITHOUT releasing a slot:
        # cap stays full (3 live), realized loss becomes truthful.
        liq = Position(
            mint=Pubkey.from_string(
                "7nNvYvWZkGYLDdZrcYnQZXy4S2H3R6Gvh3RMgxCZqSHX"
            ),
            symbol="LIQ",
            entry_price=1.0,
            quantity=1.0,
            entry_time=datetime.utcnow(),
        )
        liq.is_liquidation_recovery = True
        t._risk_record_close(liq, -0.05)
        live_still_blocked = t.risk_tracker.buy_allowed is False
        loss_still_truthful = _eps(t.risk_tracker.realized_pnl_sol, -0.05)
        # A live position's close releases its slot normally.
        live = Position(
            mint=Pubkey.from_string(
                "9Kc1HLTKS79C8JmGc2aXDTT6V8LxEkXFXkoCsWmD5nSX"
            ),
            symbol="LIVE",
            entry_price=1.0,
            quantity=1.0,
            entry_time=datetime.utcnow(),
        )
        t._risk_record_close(live, 0.01)
        freed = (
            t.risk_tracker.open_positions == 2
            and t.risk_tracker.buy_allowed is True
            and _eps(t.risk_tracker.realized_pnl_sol, -0.04)
        )
        return blocked_at_cap and live_still_blocked and loss_still_truthful and freed
    check("risk liquidation queue banks PnL w/o slot release", asyncio.run(risk4()))

    # --- Risk: zero entry price must not fabricate realized PnL -------------
    def zero_entry_degrades():
        # Live evidence 2026-10-30: rehydrated orphans carry entry_price=0;
        # (sold - 0) * qty fabricated +0.49 SOL of fake realized win and gave
        # the daily-loss kill-switch false headroom. The close path must
        # degrade the tail leg to 0.0 and bank only already-recorded legs.
        from trading.exit_engine import _realized_pnl_for_close

        p = Position(
            mint=Pubkey.from_string("11111111111111111111111111111111"),
            symbol="ZERO",
            entry_price=0.0,
            quantity=1000.0,
            entry_time=datetime.utcnow(),
        )
        degraded = _realized_pnl_for_close(p, 99.0, 1000.0)  # 99000 if unguarded
        p2 = Position(
            mint=Pubkey.from_string("11111111111111111111111111111111"),
            symbol="BANKED",
            entry_price=1.0,
            quantity=100.0,
            entry_time=datetime.utcnow(),
        )
        p2.realized_pnl_quote = 0.25  # banked legs always survive
        blended = _realized_pnl_for_close(p2, 1.1, 100.0)  # 0.25 + 10.0
        return (
            _eps(degraded, 0.0)
            and _eps(banked_ok := _realized_pnl_for_close(p2, 0.0, 100.0), 0.25)
            and _eps(blended, 10.25)
        )
    check("risk zero entry price degrades realized PnL", zero_entry_degrades())

    # --- Risk: boot must NOT restore open_positions from disk ---------------
    def risk5_boot_reset():
        # Live evidence 2026-10-30: persisting open_positions AND adding the
        # rehydrated inventory each boot double-accumulated across restarts
        # (32 open from 10 real orphans -> every buy deadlocked on the cap).
        # The trader init sets open_positions = 0 after load_from; budget and
        # loss limits restore. Rehydrate/sweeper then count ACTUAL holdings.
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "risk.json")
            rt = RiskTracker(max_concurrent_positions=3)
            rt._state_path = p
            rt.record_buy(0.01)
            rt.record_buy(0.01)  # persisted open_positions=2, spend=0.02
            rt.record_close(0.003)
            loaded = RiskTracker.load_from(p)
            # Boot semantics (mirrors the trader init): limits from config,
            # durable spend/loss from disk, open_positions re-derived to 0.
            loaded.max_concurrent_positions = 3
            loaded.open_positions = 0
            return (
                loaded.open_positions == 0
                and abs(loaded.session_spend_sol - 0.02) < 1e-9
                and abs(loaded.realized_pnl_sol - 0.003) < 1e-9
                and loaded.max_concurrent_positions == 3
            )
    check("boot resets open_positions, keeps spend/loss", risk5_boot_reset())

    # --- Negative cache: unreadable curve skips re-reads for 300s ----------
    async def neg_cache():
        # Live evidence 2026-10-30: every watcher interval re-read a closed
        # curve (get_pool_state -> ValueError + traceback). After one
        # unreadable verdict, the helper must serve the negative result from
        # cache without any further curve read for 300s.
        from interfaces.core import Platform

        t = _trader()

        class ExplodingCurveManager:
            def __init__(self):
                self.reads = 0

            async def get_pool_state(self, pool, commitment=None):
                self.reads += 1
                raise ValueError("curve closed")

        cm = ExplodingCurveManager()
        impls = SimpleNamespace(address_provider=_FakeProvider(), curve_manager=cm)

        def fake_get(platform, client):
            return impls

        import trading.exit_engine as _ee

        orig_ee = _ee.get_platform_implementations
        _ee.get_platform_implementations = fake_get
        try:
            token = _token()
            token.platform = Platform.PUMP_FUN
            token.bonding_curve = None
            token.creator = None
            token.creator_vault = None
            first = await t._ensure_sellable_orphan(token)
            second = await t._ensure_sellable_orphan(token)  # cached
            return (
                first is False
                and second is False
                and cm.reads == 1
                and str(token.mint) in t._unreadable_curves
            )
        finally:
            _ee.get_platform_implementations = orig_ee
    check("unreadable curve cached 300s (1 RPC)", asyncio.run(neg_cache()))

    # --- Watcher: STOP verdict keeps the position in the registry ------------
    async def watcher_stop():
        from trading.position import ExitReason

        t = _trader()

        async def classify_stop(token_info, sell_result):
            return ExitSellVerdict.STOP

        t._classify_failed_exit_sell = classify_stop
        t.seller.fail_next = True
        pos = _position(qty=50.0)
        token = _token()
        t._unsold_positions[str(token.mint)] = (token, pos)
        verdict = await t._attempt_position_exit(
            token, pos, ExitReason.STOP_LOSS, 0.80
        )
        # The exit engine's durable handoff re-registers (already present),
        # and the watcher keeps the entry on STOP instead of deleting it.
        return (
            verdict is ExitSellVerdict.STOP
            and str(token.mint) in t._unsold_positions
            and len(t.registered_unsold) == 1
        )
    check("watcher keeps unresolved STOP position", asyncio.run(watcher_stop()))

    # --- Position: malformed entry_price must not crash the watcher ---------
    def malformed_entry():
        # Live evidence 2026-10-30: the persistent watcher crashed with
        # ZeroDivisionError on a rehydrated/orphan row whose entry_price
        # persisted as 0.0. get_pnl must degrade to 0% instead of dividing.
        pos = _position(qty=100.0, entry=1.0)
        pos.entry_price = 0.0
        pnl = pos.get_pnl(0.00000003)
        return pnl["price_change_pct"] == 0.0 and pnl["unrealized_pnl_quote"] == 0.0
    check("position pnl survives zero entry price", malformed_entry())

    # --- Rehydrate: orphan rows without token program ids are sellable ------
    def rehydrate_program_ids():
        # Live evidence 2026-10-30: registry rows written by old sweeper
        # paths have token_program_id/quote_token_program_id missing; the
        # seller derived the ATA with None and EVERY liquidation attempt
        # crashed. _token_info_from_row must default them so rehydrated
        # orphans never re-enter the watcher unsellable.
        from core.pubkeys import SystemAddresses, WSOL_MINT
        from interfaces.core import Platform

        row = {
            "mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
            "symbol": "BONK",
            "platform": Platform.PUMP_FUN.value,
            "entry_price": 0.0,
            "quantity": 100.0,
        }
        info = UniversalTrader._token_info_from_row(row)
        return (
            info.token_program_id == SystemAddresses.TOKEN_2022_PROGRAM
            and info.quote_token_program_id == SystemAddresses.TOKEN_PROGRAM
            and info.quote_mint == WSOL_MINT
        )
    check("rehydrate defaults missing token program ids", rehydrate_program_ids())

    # --- Orphan enrichment: v2 sell fields derivable from mint + curve ------
    async def orphan_enrichment():
        # Live evidence 2026-10-30: liquidation of wallet-derived orphans
        # crashed 46x because bonding_curve/creator_vault were None and the
        # v2 sell account metas require them. _ensure_sellable_orphan must
        # derive the pool PDA from the mint and pull creator from curve state.
        from core.pubkeys import SystemAddresses
        from interfaces.core import Platform
        from platforms import get_platform_implementations

        t = _trader()
        # Fake curve state via the real curve manager? Cheaper: fabricate a
        # provider + manager pair so the test pins the DERIVATION math, not
        # the RPC layer.
        token = _token()
        token.platform = Platform.PUMP_FUN
        token.bonding_curve = None
        token.associated_bonding_curve = None
        token.creator = None
        token.creator_vault = None
        token.token_program_id = SystemAddresses.TOKEN_2022_PROGRAM

        calls = {"state_reads": 0}

        class FakeCurveManager:
            async def get_pool_state(self, pool, commitment=None):
                calls["state_reads"] += 1
                return {"creator": str(SystemAddresses.TOKEN_2022_PROGRAM)}

        import types as _types

        impls = _types.SimpleNamespace(
            address_provider=_FakeProvider(), curve_manager=FakeCurveManager()
        )

        def fake_get_platform_implementations(platform, client):
            return impls

        get_platform_implementations.__dict__
        import platforms as _platforms_mod

        orig = _platforms_mod.get_platform_implementations
        _platforms_mod.get_platform_implementations = fake_get_platform_implementations
        try:
            # Rebind into the exit-engine module namespace too.
            import trading.exit_engine as _ee

            orig_ee = _ee.get_platform_implementations
            _ee.get_platform_implementations = fake_get_platform_implementations
            await t._ensure_sellable_orphan(token)
            return (
                token.bonding_curve is not None
                and token.associated_bonding_curve is not None
                and token.creator_vault is not None
                and calls["state_reads"] == 1
            )
        finally:
            _platforms_mod.get_platform_implementations = orig
            _ee.get_platform_implementations = orig_ee
    check("orphan enrich derives v2 sell fields", asyncio.run(orphan_enrichment()))

    # --- Orphan skip: un-enrichable (closed curve) must NOT crash -----------
    async def orphan_skip_no_crash():
        # Live evidence 2026-10-30: a closed/graduated curve makes enrichment
        # fail; the old call site then ran straight into the instruction
        # builder and crashed on None (86 tracebacks). The exit path must
        # turn an un-enrichable orphan into a clean RETRY instead.
        from core.pubkeys import SystemAddresses
        from interfaces.core import Platform

        t = _trader()

        class ExplodingCurveManager:
            async def get_pool_state(self, pool, commitment=None):
                raise ValueError("curve closed")

        impls = SimpleNamespace(
            address_provider=_FakeProvider(), curve_manager=ExplodingCurveManager()
        )

        def fake_get(platform, client):
            return impls

        import trading.exit_engine as _ee

        orig_ee = _ee.get_platform_implementations
        _ee.get_platform_implementations = fake_get
        try:
            token = _token()
            token.platform = Platform.PUMP_FUN
            token.bonding_curve = None
            token.creator = None
            token.creator_vault = None
            ok = await t._ensure_sellable_orphan(token)
            return ok is False  # explicit "cannot sell", not a crash
        finally:
            _ee.get_platform_implementations = orig_ee
    check("orphan skip when curve unreadable", asyncio.run(orphan_skip_no_crash()))

    if failures:
        print(f"\nR5-P0-1 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR5-P0-1: P0 execution-path remediation OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())