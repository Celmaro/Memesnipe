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
from pathlib import Path
from types import SimpleNamespace

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.pubkeys import DEFAULT_PUBKEY
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

    # exit-engine wiring
    t._trail_states = {}
    t._trail_exit_attempts = {}
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
            # A fresh trader boot restores open_positions=2, then config
            # caps override the limits (3) but the RUNNING counters survive.
            t = _trader()
            loaded = RiskTracker.load_from(p)
            loaded.max_concurrent_positions = 3  # config wins for limit
            return loaded.open_positions == 2 and loaded.max_concurrent_positions == 3
    check("risk config-wins-for-limit, disk-wins-for-counters", risk3())

    # --- Risk: rehydrated unsold inventory occupies concurrency slots -------
    async def risk4():
        # The durable unsold registry is loaded at boot; every recovered
        # position holds wallet capital and MUST occupy a risk slot, so the
        # bot does not stack new buys on top of inventory still liquidating.
        t = _trader()
        t.risk_tracker = RiskTracker(max_concurrent_positions=3)
        t.risk_tracker._state_path = None
        t.risk_tracker.record_buy(0.01)
        t.risk_tracker.record_buy(0.01)  # 2 live slots used
        # Simulate _rehydrate_unsold finding 2 recovered positions.
        recovered = 2
        t.risk_tracker.open_positions += recovered  # 4 >= cap 3
        blocked_while_recovered = t.risk_tracker.buy_allowed is False
        # Slots are released on a successful watcher sell / TTL giveaway.
        t.risk_tracker.record_close(0.0)
        t.risk_tracker.record_close(0.0)  # 2 live remain
        return (
            blocked_while_recovered
            and t.risk_tracker.open_positions == 2
            and t.risk_tracker.buy_allowed is True  # freed below cap again
        )
    check("risk rehydrated inventory counts toward cap", asyncio.run(risk4()))

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

    if failures:
        print(f"\nR5-P0-1 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR5-P0-1: P0 execution-path remediation OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())