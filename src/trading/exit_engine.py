"""Exit engine (C12.3 decomposition): position-exit lifecycle for UniversalTrader.

Extracted from ``src/trading/universal_trader.py`` — the exit-domain symbols
(``ExitSellVerdict``, ``_exit_sell_verdict_for``,
``DEFAULT_MAX_EXIT_SELL_ATTEMPTS``) and the staged/trail/attempt methods that
drive a position to its final close. ``ExitEngineMixin`` is mixed into
``UniversalTrader`` so the class keeps its method surface while the monolith
shrinks. No behavior change: every attribute read through ``self`` resolves on
the composed trader exactly as before.

``ExitSellVerdict`` / ``_exit_sell_verdict_for`` /
``DEFAULT_MAX_EXIT_SELL_ATTEMPTS`` are also re-exported from
``trading.universal_trader`` (which imports them here) so existing imports of
those names keep working.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from time import monotonic
from typing import Any

from core.pubkeys import (
    SystemAddresses,
    WSOL_MINT,
    normalize_quote_mint,
    quote_symbol,
)
from cleanup.modes import handle_cleanup_after_sell
from interfaces.core import Platform, TokenInfo
from platforms import get_platform_implementations
from solders.pubkey import Pubkey
from trading.base import TradeFailureReason, TradeResult
from trading.position import ExitReason, Position
from trading.trail_exit import TrailExitState
from utils.logger import get_logger

logger = get_logger(__name__)

# Default for trade.max_exit_sell_attempts. The seller's own max_retries covers
# submission only, so a revert is retried in the monitor loop, where the price is
# re-read first. Bounded so a token that keeps reverting cannot pin the bot.
DEFAULT_MAX_EXIT_SELL_ATTEMPTS = 3


class ExitSellVerdict(Enum):
    """What to do after an exit sell came back unsuccessful.

    An exit sell is not idempotent, so "it did not succeed" is not enough to act
    on: a reverted sell changed nothing and should be retried, while one whose
    confirmation never arrived may already have emptied the position.

    `_classify_failed_exit_sell` only returns the three failure verdicts; SOLD is
    reported by the attempt itself.
    """

    SOLD = "sold"  # confirmed on this attempt, the ordinary happy path
    LATE_SUCCESS = "late_success"  # it landed after all — stop, do not resell
    RETRY = "retry"  # provably nothing happened on chain
    STOP = "stop"  # still unknown — a blind resell is the worse risk


def _exit_sell_verdict_for(reason: TradeFailureReason | None) -> ExitSellVerdict | None:
    """Map a failure reason to a verdict, where one can be decided offline.

    Args:
        reason: The reported failure reason, or None if the seller did not set
            one — which means "unknown", never "reverted".

    Returns:
        The verdict, or None when the signature has to be re-checked first.
    """
    if reason is TradeFailureReason.REVERTED:
        # The program errored and the tokens are still held.
        return ExitSellVerdict.RETRY
    if reason is TradeFailureReason.SUBMIT_FAILED:
        # Nothing was confirmed as sent and there is no signature to re-check.
        # Stranding the position is the worse of the two risks.
        return ExitSellVerdict.RETRY
    return None


class ExitEngineMixin:
    """Position-exit methods mixed into UniversalTrader (C12.3)."""

    def _log_attempt(
        self,
        action: str,
        token_info: TokenInfo,
        attempt: int,
        tx_hash: str | None,
        failure_reason: str | None = None,
    ) -> None:
        """Append per-attempt telemetry to the ledger (trades/attempts.log).

        Every individual buy/sell submission gets a row with its signature and
        the reason it was counted as failed, so post-mortems (e.g. a buy that
        failed to confirm) can be traced attempt-by-attempt.
        """
        try:
            trades_dir = Path("trades")
            trades_dir.mkdir(exist_ok=True)
            entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "action": action,
                "symbol": token_info.symbol,
                "token_address": str(token_info.mint),
                "attempt": attempt,
                "tx_hash": str(tx_hash) if tx_hash else None,
                "failure_reason": failure_reason,
            }
            path = trades_dir / "attempts.log"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            logger.exception("Failed to log attempt telemetry")

    async def _handle_staged_trail_exit(
        self,
        token_info: TokenInfo,
        position: Position,
        current_price: float,
    ) -> bool:
        """One staged-then-trail evaluation (R4-P1-7, exit_mode staged_trail).

        Creates the per-position TrailExitState on first sight, then feeds
        every monitor tick through it. The returned decision is executed
        through the SAME machinery as the staged ranges:

        - ``sell_share`` (initial TP) -> ``_staged_partial_sell`` (partial
          sell, position stays open with the floor armed).
        - ``exit_all`` (trail floor / hard stop) -> full exit via
          ``_run_exit_attempt`` with TRAIL_STOP / STOP_LOSS.

        Returns True when the position is closed; False to keep monitoring.
        """
        mint_key = str(token_info.mint)
        state = self._trail_states.get(mint_key)
        if state is None:
            if self.exit_mode == "trail10":
                # Pure trailing stop: no initial TP leg (share=0), arm at
                # +trail10_arm_pct, trail trail10_trail_pct off the peak,
                # hard stop trail10_hard_stop_pct.
                state = TrailExitState(
                    entry_price=position.entry_price or 0.0,
                    initial_tp_share=0.0,
                    initial_tp_pct=self.trail10_arm_pct,
                    arm_pct=self.trail10_arm_pct,
                    trail_pct=self.trail10_trail_pct,
                    hard_stop_pct=self.trail10_hard_stop_pct,
                )
            else:
                state = TrailExitState(
                    entry_price=position.entry_price or 0.0,
                    initial_tp_share=self.trail_initial_tp_share,
                    initial_tp_pct=self.trail_initial_tp_pct,
                    second_tp_share=self.trail_second_tp_share,
                    second_tp_pct=self.trail_second_tp_pct,
                    tp1_floor_pct=self.trail_tp1_floor_pct,
                    arm_pct=self.trail_arm_pct,
                    trail_pct=self.trail_trail_pct,
                    hard_stop_pct=self.trail_hard_stop_pct,
                )
            self._trail_states[mint_key] = state

        # Snapshot state because on_tick arms a TP before the transaction is
        # confirmed. A failed sell must restore the pre-decision state.
        state_before = (
            state.peak_price,
            state.floor_price,
            state.initial_tp_fired,
            state.second_tp_fired,
            state.armed,
        )
        decision = state.on_tick(current_price)

        if decision.action == "sell_share":
            sold = await self._staged_partial_sell(
                token_info, position, decision.share, "trail_initial_tp", decision.price
            )
            if not sold:
                (state.peak_price, state.floor_price, state.initial_tp_fired,
                 state.second_tp_fired, state.armed) = state_before
            return not position.is_active

        if decision.action == "exit_all":
            reason = (
                ExitReason.TRAIL_STOP
                if decision.reason == "trail_floor"
                else ExitReason.STOP_LOSS
            )
            logger.info(
                f"Trail exit for {token_info.symbol}: {reason.value} "
                f"@ {decision.price:.3e}"
            )
            # Cumulative attempts for THIS exit episode: the old path passed
            # attempt=1 on every tick, so max_exit_sell_attempts never
            # tripped and the persistent watcher handoff was unreachable.
            # Reset is handled on state-map cleanup (position close) and by
            # the hold path below (price recovered = new episode).
            attempts = self._trail_exit_attempts.get(mint_key, 0) + 1
            self._trail_exit_attempts[mint_key] = attempts
            if await self._run_exit_attempt(
                token_info,
                position,
                reason,
                decision.price,
                attempt=attempts,
            ):
                return True
            # Exit sell failed: keep monitoring, the floor stays armed and
            # the next tick will retry against a fresh price. The counter
            # persists so the retry budget eventually hands off.
            return False

        # Hold / new high / no-price: price recovered (or nothing due). A new
        # exit episode starts fresh — reset the cumulative attempt counter so
        # a later hard-stop retry gets the full budget again.
        self._trail_exit_attempts.pop(mint_key, None)
        return False

    async def _handle_staged_exit(
        self,
        token_info: TokenInfo,
        position: Position,
        current_price: float,
    ) -> bool:
        """Fire any staged TP/SL steps the current price has reached.

        Returns True when at least one staged sell ran (or the position
        closed); False when nothing was due.

        Semantics (R3-P1-2):
        - TP steps walk low-to-high; each fires once, selling its share of the
          position at the current price (partial sell, position stays open).
        - SL steps walk high-to-low; the band bottom sells the remaining
          position (final close). An SL final step marks the position closed.
        - A single-step band (from_pct == to_pct) fires on a jump past it.
        """
        if not (self.tp_ranges or self.sl_ranges):
            return False
        from trading.staged_exit import due_sl_steps, due_tp_steps

        fired_any = False
        # TP first (take profit as it runs), then SL (protect on the way down).
        tp_due = due_tp_steps(
            position.entry_price, current_price, self.tp_ranges, self._tp_fired
        )
        for step in tp_due:
            # P0-2: mark a step fired only AFTER its sell succeeded — a
            # failed leg must stay armed for the next tick.
            sold = await self._staged_partial_sell(
                token_info, position, step.share, "staged_tp", step.trigger_price
            )
            if sold:
                self._tp_fired.add((step.band_index, step.trigger_price))
                fired_any = True
            if not position.is_active:
                return True

        sl_due = due_sl_steps(
            position.entry_price, current_price, self.sl_ranges, self._sl_fired
        )
        for step in sl_due:
            if step.is_final:
                # Band bottom: close the position in full.
                await self._run_exit_attempt(
                    token_info,
                    position,
                    ExitReason.STOP_LOSS,
                    current_price,
                    1,
                )
                return True
            sold = await self._staged_partial_sell(
                token_info, position, step.share, "staged_sl", step.trigger_price
            )
            if sold:
                self._sl_fired.add((step.band_index, step.trigger_price))
                fired_any = True
            if not position.is_active:
                return True
        return fired_any

    async def _ensure_sellable_orphan(self, token_info: TokenInfo) -> bool:
        """Best-effort enrich a wallet-derived TokenInfo so the sell can build.

        Sweeper-found and rehydrated orphans only carry mint/platform/quote
        basics — the pump.fun v2 sell requires bonding_curve, creator and
        creator_vault, and without them `derive_quote_token_account` receives
        None and every liquidation attempt crashes (observed live 2026-10-30,
        46+ tracebacks before the fleet was stopped).

        The bonding-curve PDA is deterministic from the mint (zero RPC); the
        creator (and thus creator_vault) comes from the curve state read,
        which the curve manager caches.

        Returns:
            True if the TokenInfo can build a v2 sell (either it already had
            the fields, or enrichment succeeded). False means the curve is
            unreadable (closed/graduated/legacy-v1) — the caller MUST skip
            the sell attempt this round instead of letting the instruction
            builder crash on None.
        """
        if getattr(token_info, "platform", None) != Platform.PUMP_FUN:
            return True
        if token_info.bonding_curve:
            # Event-parsed and test-fixture positions carry the v2 account
            # set (bonding_curve, creator, creator_vault). Only true
            # wallet-derived orphans lack bonding_curve — keying on it here
            # targets exactly the sweeper/rehydrate path and leaves every
            # normal exit untouched (zero-crash, zero extra RPC).
            return True
        try:
            implementations = get_platform_implementations(
                token_info.platform, self.solana_client
            )
            provider = implementations.address_provider
            curve_manager = implementations.curve_manager
            pool = token_info.bonding_curve or provider.derive_bonding_curve_v2(
                token_info.mint
            )
            token_info.bonding_curve = pool
            token_program = getattr(token_info, "token_program_id", None)
            token_info.associated_bonding_curve = (
                provider.derive_associated_bonding_curve(
                    token_info.mint,
                    pool,
                    token_program or SystemAddresses.TOKEN_2022_PROGRAM,
                )
            )
            if getattr(token_info, "creator", None) is None:
                state = await curve_manager.get_pool_state(pool)
                creator_raw = state.get("creator") or state.get(
                    "creator_address", ""
                )
                if creator_raw:
                    token_info.creator = (
                        Pubkey.from_string(str(creator_raw))
                        if not isinstance(creator_raw, Pubkey)
                        else creator_raw
                    )
                    token_info.creator_vault = provider.derive_creator_vault(
                        token_info.creator
                    )
            elif getattr(token_info, "creator_vault", None) is None:
                token_info.creator_vault = provider.derive_creator_vault(
                    token_info.creator
                )
        except Exception:
            logger.warning(
                "Could not enrich orphan TokenInfo for %s; sell skipped this "
                "round (curve unreadable)",
                token_info.symbol,
                exc_info=True,
            )
            return False
        # Enrichment may end with fields still unset if the curve state had
        # no creator (closed/graduated). Be explicit about sellability.
        return bool(token_info.bonding_curve and token_info.creator_vault)

    async def _staged_partial_sell(
        self,
        token_info: TokenInfo,
        position: Position,
        share: float,
        exit_reason: str,
        trigger_price: float,
    ) -> bool:
        """Sell ``share`` of the position at the current price (staged).

        Returns True when the leg sold (real or simulated). A failed leg
        returns False so the caller keeps the exit state armed for retry —
        a TP step must never be considered fired before its sell succeeds.
        """
        qty = position.quantity * share
        if qty <= 0:
            return True  # nothing to sell is not a failure
        price = await self._read_price(token_info)
        if price <= 0:
            price = trigger_price  # floor the partial at the trigger level
        # Whether the REQUESTED leg drains the position (used only to tag
        # captures as partial/final; the actual bookkeeping below re-derives
        # drained from the ACTUAL sold amount).
        drained = (position.quantity - qty) <= 0.0
        # P0-2: successful real sells (and shadow sims) report the amount
        # actually sold — the seller may clamp it (max_safe_sell_amount /
        # impact cap). Bookkeeping below always uses the ACTUAL amount so a
        # clamped leg can never understate the held quantity.
        sold_amount = qty
        sold_price = price
        if self.shadow_mode:
            # Shadow mode is unconditionally read-only: the simulated
            # partial path must not depend on shadow_capture being enabled.
            if self.shadow_capture is not None:
                fee_fields = await self._fee_estimate_fields()
                self.shadow_capture.sim_close(
                    mint=str(token_info.mint),
                    symbol=token_info.symbol,
                    platform=token_info.platform.value,
                    price=price,
                    amount_tokens=qty,
                    quote_received=price * qty,
                    exit_reason=exit_reason,
                    pnl_quote=(price - position.entry_price) * qty,
                    # Percent, matching get_pnl().price_change_pct used by final
                    # closes — partial legs and final closes must share units.
                    pnl_pct=(price - position.entry_price) / position.entry_price * 100
                    if position.entry_price > 0
                    else 0.0,
                    hold_seconds=(datetime.utcnow() - position.entry_time).total_seconds(),
                    pool_snapshot=await self._pool_snapshot(token_info),
                    **fee_fields,
                    reordering_bps=0,  # R4-P0-1: shadow fills at observed price
                    # C8.1/P0-2: a staged partial leg must not be counted as a
                    # full close downstream (net_metrics / sweep_zombies /
                    # rug_memory). Tag it with the shared position identity and
                    # mark non-draining legs as partial so aggregators can group.
                    is_partial=not drained,
                    position_id=position.position_id,
                    leg_exit_reason=exit_reason,
                )
            logger.info(
                f"[SHADOW] staged {exit_reason} sell {qty:.6f} {token_info.symbol} "
                f"@ {price:.3e} (no tx sent)"
            )
        else:
            # Wallet-derived orphans may lack pump.fun v2 account fields
            # (bonding_curve/creator_vault); enrich before building the
            # instruction or the ATA derivation crashes with None (observed
            # live 2026-10-30). Un-enrichable (curve closed/graduated)
            # orphans are skipped this round — the retry path re-reads price
            # and re-attempts, and the watcher TTL bounds the churn.
            if not await self._ensure_sellable_orphan(token_info):
                return False
            # P0-1: partial sells ride the SAME serialization lock as buys and
            # full exits — a partial TP racing an in-flight buy/sell can corrupt
            # the WSOL wrap/unwrap balance exactly like the full-sell race the
            # lock was built for.
            async with self._trade_serialization_lock:
                sell_result = await self.seller.execute(
                    token_info, token_amount=qty, token_price=price
                )
            if not sell_result.success:
                logger.warning(
                    f"Staged {exit_reason} sell failed for {token_info.symbol}: "
                    f"{sell_result.error_message}"
                )
                return False
            sold_amount = (
                sell_result.amount if sell_result.amount is not None else qty
            )
            sold_amount = min(sold_amount, position.quantity)
            sold_price = sell_result.price or price
            # Live research capture: record the real partial leg using the
            # same schema as shadow sim_close, tagged for calibration.
            if self.shadow_capture is not None:
                fee_fields = await self._fee_estimate_fields()
                self.shadow_capture.sim_close(
                    mint=str(token_info.mint),
                    symbol=token_info.symbol,
                    platform=token_info.platform.value,
                    price=sold_price,
                    amount_tokens=sold_amount,
                    quote_received=sold_price * sold_amount,
                    exit_reason=exit_reason,
                    pnl_quote=(sold_price - position.entry_price) * sold_amount,
                    # Percent, matching get_pnl().price_change_pct used by
                    # final closes — partial legs and final closes must share
                    # units.
                    pnl_pct=(
                        (sold_price - position.entry_price)
                        / position.entry_price
                        * 100
                        if position.entry_price > 0
                        else 0.0
                    ),
                    hold_seconds=(
                        datetime.utcnow() - position.entry_time
                    ).total_seconds(),
                    pool_snapshot=await self._pool_snapshot(token_info),
                    **fee_fields,
                    reordering_bps=0,
                    is_partial=not drained,
                    position_id=position.position_id,
                    leg_exit_reason=exit_reason,
                    mode="live",
                    tx_signature=sell_result.tx_signature,
                )
        # Reduce position quantity by the ACTUAL sold amount (keep entry
        # price). C4.8: bank the leg's realized PnL BEFORE shrinking quantity
        # so the final close can report the blended result.
        leg_realized = (sold_price - position.entry_price) * sold_amount
        position.realized_pnl_quote += leg_realized
        remaining_after = position.quantity - sold_amount
        position.quantity = max(0.0, remaining_after)
        # P0-6: feed the risk guard the leg's REALIZED PnL (partial legs bank
        # realized results without closing a concurrency slot; the final
        # close records its own tail + decrements).
        try:
            if remaining_after <= 0.0:
                self.risk_tracker.record_close(leg_realized)
            else:
                self.risk_tracker.record_partial_close(leg_realized)
        except Exception:
            self.risk_tracker.record_partial_close(0.0)
        if remaining_after <= 0.0:
            drained = True
            position.close_position(sold_price, ExitReason.TAKE_PROFIT)
            # R4-P1-7: a staged ladder that drains the position is also a
            # close — finalize the strategies-ledger entry here (shadow).
            if self.strategies_ledger is not None:
                self.strategies_ledger.close_position(
                    mint=str(token_info.mint),
                    close_price=sold_price,
                    exit_reason="staged_drain",
                    hold_seconds=(
                        datetime.utcnow() - position.entry_time
                    ).total_seconds(),
                )
            # C4.8: a draining staged leg IS the final close — feed the
            # creator-PnL ledger with the whole position's blended PnL
            # (banked legs + this one), same guarded contract as the
            # tail-leg close in _attempt_position_exit.
            await self._record_creator_pnl(token_info, position)
            await self._cleanup_after_exit(token_info)
        return True

    async def _run_exit_attempt(
        self,
        token_info: TokenInfo,
        position: Position,
        exit_reason: ExitReason,
        price: float,
        attempt: int,
    ) -> bool:
        """Make one bounded attempt to close the position.

        Args:
            position: The open position
            exit_reason: Why the exit fired
            price: Price the sell is floored against
            attempt: 1-based attempt counter, for the bound and the logs

        Returns:
            True when monitoring should stop, whether because the position is
            closed, because the outcome could not be resolved, or because the
            attempt budget is spent.
        """
        verdict = await self._attempt_position_exit(
            token_info, position, exit_reason, price
        )
        if verdict is not ExitSellVerdict.RETRY:
            return True

        if attempt >= self.max_exit_sell_attempts:
            logger.error(
                f"Giving up on exiting {token_info.symbol} after "
                f"{attempt} attempts. Handing the position to the persistent "
                f"exit watcher - tokens are still held."
            )
            await self._register_unsold_position(token_info, position)
            return True
        return False

    async def _attempt_position_exit(
        self,
        token_info: TokenInfo,
        position: Position,
        exit_reason: ExitReason,
        price: float,
    ) -> ExitSellVerdict:
        """Sell the position once and report what came of it.

        Args:
            position: The open position, closed in place on success
            exit_reason: Why the exit fired
            price: Price the sell is floored against. The seller turns it into
                `min_quote_output`, so it must be a price the pool can pay — an
                exit fires precisely because the price left the entry price.

        Returns:
            SOLD, LATE_SUCCESS, RETRY, or STOP
        """
        # Log PnL before exit
        pnl = position.get_pnl(price)
        logger.info(
            f"Position PnL: {pnl['price_change_pct']:.2f}% "
            f"({pnl['unrealized_pnl_quote']:.6f} "
            f"{quote_symbol(normalize_quote_mint(token_info.quote_mint))})"
        )

        if self.shadow_mode:
            # Shadow/paper mode (#8): simulate the exit at the current price.
            sim_price = await self._read_price(token_info)
            sell_result = TradeResult(
                success=sim_price > 0,
                platform=token_info.platform,
                tx_signature=None,
                amount=position.quantity,
                price=sim_price if sim_price > 0 else 0.0,
            )
            logger.info(
                f"[SHADOW] would exit {token_info.symbol} at "
                f"{sim_price:.3e} SOL/token ({exit_reason.value}, no tx sent)"
            )
            if self.shadow_capture is not None:
                hold_seconds = (datetime.utcnow() - position.entry_time).total_seconds()
                fee_fields = await self._fee_estimate_fields()
                self.shadow_capture.sim_close(
                    mint=str(token_info.mint),
                    symbol=token_info.symbol,
                    platform=token_info.platform.value,
                    price=sim_price,
                    amount_tokens=position.quantity,
                    quote_received=sim_price * position.quantity,
                    exit_reason=exit_reason.value,
                    pnl_quote=float(pnl.get("unrealized_pnl_quote", 0.0)),
                    pnl_pct=float(pnl.get("price_change_pct", 0.0)),
                    hold_seconds=hold_seconds,
                    pool_snapshot=await self._pool_snapshot(token_info),
                    **fee_fields,
                    reordering_bps=0,  # R4-P0-1: shadow fills at observed price
                    # P0-2: final close of this position; carries the same
                    # position_id as any staged partial legs (no is_partial).
                    position_id=position.position_id,
                )
                # R4-P1-7: finalize the strategies-ledger entry at the same
                # close so each registered strategy reports its would-be
                # outcome for this position (shadow only).
                if self.strategies_ledger is not None:
                    self.strategies_ledger.close_position(
                        mint=str(token_info.mint),
                        close_price=sim_price,
                        exit_reason=exit_reason.value,
                        hold_seconds=hold_seconds,
                        **fee_fields,
                    )
        else:
            # R3-P2-13: serialize the exit sell against any in-flight buy.
            # Wallet-derived orphans lack pump.fun v2 account fields that the
            # instruction builder requires; enrich first (best-effort). If the
            # curve is unreadable, skip this round — a RETRY verdict keeps the
            # position with the monitor/watcher instead of crashing the
            # instruction builder on None (2026-10-30 live evidence).
            if not await self._ensure_sellable_orphan(token_info):
                return ExitSellVerdict.RETRY
            async with self._trade_serialization_lock:
                sell_result = await self.seller.execute(
                    token_info,
                    token_amount=position.quantity,
                    token_price=price,
                )

        if sell_result.success:
            # P0-5: the seller can clamp the amount (max_safe_sell_amount /
            # sell-impact cap), so the ACTUAL sold quantity is authoritative.
            # A full exit that sold less than the held quantity must NOT mark
            # the position closed — the residual stays in the wallet and is
            # handed to the durable watcher (or retried).
            actual_sold = (
                getattr(sell_result, "amount", None)
                if getattr(sell_result, "amount", None) is not None
                else position.quantity
            )
            actual_sold = min(actual_sold, position.quantity)
            sold_price = sell_result.price or price
            residual = position.quantity - actual_sold
            if residual > 1e-12:
                logger.warning(
                    f"Exit sell for {token_info.symbol} filled only "
                    f"{actual_sold:.9f} of {position.quantity:.9f} tokens "
                    f"(clamped); {residual:.9f} remain — handing residual to "
                    f"the persistent watcher instead of marking closed."
                )
                # Bank the sold leg's realized PnL for the blended close.
                position.realized_pnl_quote += (
                    (sold_price - position.entry_price) * actual_sold
                )
                position.quantity = residual
                try:
                    self.risk_tracker.record_partial_close(
                        (sold_price - position.entry_price) * actual_sold
                    )
                except Exception:
                    self.risk_tracker.record_partial_close(0.0)
                await self._register_unsold_position(token_info, position)
                self._log_trade(
                    "sell",
                    token_info,
                    sold_price,
                    actual_sold,
                    sell_result.tx_signature,
                    quote_received_sol=(
                        sold_price * actual_sold
                        if sold_price
                        and actual_sold
                        and (
                            normalize_quote_mint(token_info.quote_mint)
                            == WSOL_MINT
                        )
                        else None
                    ),
                )
                return ExitSellVerdict.SOLD  # handoff owns the residual

            # Live research capture: the real closing fill, same schema as
            # the shadow sim_close, tagged for calibration. Placed right
            # after success so the strategies ledger finalizes against the
            # actual fill (and both writers share the fee estimates).
            if not self.shadow_mode and self.shadow_capture is not None:
                hold_seconds = (
                    datetime.utcnow() - position.entry_time
                ).total_seconds()
                fee_fields = await self._fee_estimate_fields()
                self.shadow_capture.sim_close(
                    mint=str(token_info.mint),
                    symbol=token_info.symbol,
                    platform=token_info.platform.value,
                    price=sold_price,
                    amount_tokens=actual_sold,
                    quote_received=sold_price * actual_sold,
                    exit_reason=exit_reason.value,
                    pnl_quote=(
                        (sold_price - position.entry_price) * actual_sold
                    ),
                    pnl_pct=(
                        (sold_price - position.entry_price)
                        / position.entry_price
                        * 100
                        if position.entry_price > 0
                        else 0.0
                    ),
                    hold_seconds=hold_seconds,
                    pool_snapshot=await self._pool_snapshot(token_info),
                    **fee_fields,
                    reordering_bps=0,
                    position_id=position.position_id,
                    mode="live",
                    tx_signature=sell_result.tx_signature,
                )
                if self.strategies_ledger is not None:
                    self.strategies_ledger.close_position(
                        mint=str(token_info.mint),
                        close_price=sold_price,
                        exit_reason=exit_reason.value,
                        hold_seconds=hold_seconds,
                        **fee_fields,
                    )
            # Close position with actual exit price
            position.close_position(sold_price, exit_reason)
            # R3-P2-10: track REALIZED PnL for the risk guards — recovered
            # from the actual fill, not the pre-sell estimate (the estimate
            # only reflects the trigger price and can understate the loss).
            # Banked staged legs are included via real_pnl_quote.
            realized_pnl = position.realized_pnl_quote + (
                (sold_price - position.entry_price) * actual_sold
            )
            position.realized_pnl_quote = realized_pnl
            try:
                self.risk_tracker.record_close(realized_pnl)
            except Exception:
                self.risk_tracker.record_close(0.0)

            logger.info(f"Successfully exited position: {exit_reason.value}")
            self._log_trade(
                "sell",
                token_info,
                sold_price,
                actual_sold,
                sell_result.tx_signature,
                quote_received_sol=(
                    sold_price * actual_sold
                    if sold_price
                    and actual_sold
                    and (normalize_quote_mint(token_info.quote_mint) == WSOL_MINT)
                    else None
                ),
            )

            final_pnl = position.get_pnl()
            total_pnl_quote = position.realized_pnl_quote + float(
                final_pnl.get("unrealized_pnl_quote", 0.0)
            )
            logger.info(
                f"Final PnL: {final_pnl['price_change_pct']:.2f}% "
                f"({total_pnl_quote:.6f} "
                f"{quote_symbol(normalize_quote_mint(token_info.quote_mint))}, "
                f"incl. {position.realized_pnl_quote:.6f} banked legs)"
            )

            # C4.8: feed the creator-PnL ledger the whole position's blended
            # result (banked legs + tail leg), guarded by the helper.
            await self._record_creator_pnl(token_info, position)
            await self._cleanup_after_exit(token_info)
            return ExitSellVerdict.SOLD

        logger.error(f"Failed to exit position: {sell_result.error_message}")
        self._log_attempt(
            "exit_sell",
            token_info,
            1,
            sell_result.tx_signature,
            sell_result.error_message,
        )

        verdict = await self._classify_failed_exit_sell(token_info, sell_result)
        if verdict is ExitSellVerdict.LATE_SUCCESS:
            # The sell landed, only its confirmation was late. Close against the
            # price it was floored at; the real fill was never read back.
            position.close_position(price, exit_reason)
            logger.info(f"Position for {token_info.symbol} closed by a late fill")
            # Record the close in the risk tracker with the best PnL evidence
            # available (the floored price; the actual fill was never read).
            # Banked staged legs are included.
            realized_pnl = position.realized_pnl_quote + (
                (price - position.entry_price) * position.quantity
            )
            position.realized_pnl_quote = realized_pnl
            try:
                self.risk_tracker.record_close(realized_pnl)
            except Exception:
                self.risk_tracker.record_close(0.0)
            await self._record_creator_pnl(token_info, position)
            await self._cleanup_after_exit(token_info)
        elif verdict is ExitSellVerdict.STOP:
            logger.error(
                f"Stopping exit attempts for {token_info.symbol} - the last "
                f"sell could not be resolved. Handing the position to the "
                f"persistent exit watcher (durable reconciliation) instead "
                f"of silently abandoning it."
            )
            # P0-4: an unresolved sell must not end monitoring silently. The
            # persistent watcher re-reads the price and retries, and its TTL
            # bounds repeated attempts. This is the durable handoff the old
            # code skipped.
            if position.quantity > 0:
                await self._register_unsold_position(token_info, position)
        return verdict

    async def _record_creator_pnl(self, token_info: TokenInfo, position: Position) -> None:
        """C4.8: feed the creator-PnL ledger with a position's BLENDED result.

        Staged partial legs bank their realized PnL into
        ``position.realized_pnl_quote`` and shrink ``quantity``; a tail close
        therefore reports ``banked + get_pnl()`` — the whole position,
        not just the last leg. Recorded only for SOL-paired coins (the
        ledger's PnL is SOL-denominated) and only when the store exists
        (feature enabled). A missing creator is skipped, never fatal.
        """
        if self.creator_pnl_store is None:
            return
        creator = str(token_info.creator or "") or (
            str(token_info.user) if token_info.user else ""
        )
        if not creator or normalize_quote_mint(token_info.quote_mint) != WSOL_MINT:
            return
        final_pnl = position.get_pnl()
        total_pnl_quote = position.realized_pnl_quote + float(
            final_pnl.get("unrealized_pnl_quote", 0.0)
        )
        try:
            self.creator_pnl_store.record_trade(creator, total_pnl_quote)
        except Exception:
            logger.exception("creator PnL record failed")

    async def _cleanup_after_exit(self, token_info: TokenInfo) -> None:
        """Run the configured post-sell cleanup for a closed position.

        Args:
            token_info: Token whose account may now be closable
        """
        # Shadow mode must be genuinely read-only. The simulated exit path
        # reaches this helper too; without this guard, cleanup.mode=after_sell
        # could still submit an ATA close against the real wallet.
        if not self.shadow_mode:
            await handle_cleanup_after_sell(
                self.solana_client,
                self.wallet,
                token_info.mint,
                token_info.token_program_id,
                self.priority_fee_manager,
                self.cleanup_mode,
                self.cleanup_with_priority_fee,
                self.cleanup_force_close_with_burn,
            )
        clear_state = getattr(self, "_clear_mint_tracking_state", None)
        if clear_state is not None:
            clear_state(str(token_info.mint))
