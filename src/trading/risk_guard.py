"""Session risk guards (R3-P2-10).

Three independent gates, all off by default (0 = off):

- ``max_concurrent_positions``: cap how many open positions the bot may hold
  at once (default 1 keeps the single-token run shape).
- ``daily_loss_limit_sol``: once realized losses for the session exceed this,
  NEW buys are blocked while sells/exits still run (a loss limit must never
  trap money in a falling position).
- ``budget_sol``: cap on total quote spent on buys for the session.

The tracker is pure and offline-testable; the trader wires it into
``_handle_token`` (before the buy) and ``stop()`` (reset).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RiskTracker:
    """Tracks open positions, realized PnL, and session spend."""

    max_concurrent_positions: int = 0  # 0 = off
    daily_loss_limit_sol: float = 0.0  # 0 = off
    budget_sol: float = 0.0  # 0 = off
    realized_pnl_sol: float = 0.0
    session_spend_sol: float = 0.0
    open_positions: int = 0
    blocked_reason: str | None = None
    history: list[str] = field(default_factory=list)

    # R5-P0-3: gross SOL recovered from closes (WSOL quote only). The loss
    # guard used to check realized_pnl_sol alone, which stays positive while
    # buys are still held — the wallet drained 19→12 USD with realized
    # +0.0038 (live evidence 2026-10-30). Net unrecovered spend
    # (session_spend - gross_recovered) is the truthful "portfolio down $X"
    # cut: it counts money out vs money back regardless of whether a losing
    # position was sold yet.
    gross_recovered_sol: float = 0.0

    def record_buy(self, quote_spent_sol: float) -> bool:
        """Register a buy; returns False when a guard blocks it first."""
        reason = self._block_reason()
        if reason:
            self.blocked_reason = reason
            return False
        self.open_positions += 1
        self.session_spend_sol += quote_spent_sol
        self.history.append(f"buy {quote_spent_sol:.6f}")
        self._maybe_persist()
        return True

    def record_recovery(self, quote_received_sol: float) -> None:
        """Bank SOL actually received back from a close (WSOL quote only).

        Called alongside record_close / record_liquidation_close /
        record_partial_close with the GROSS proceeds (sold_price * amount),
        not the PnL — the unrecovered-spend guard needs money-back, not the
        realized result, so held-but-dumped positions still count as spend
        until their value actually returns to the wallet.
        """
        if not quote_received_sol or quote_received_sol <= 0:
            return
        self.gross_recovered_sol += quote_received_sol
        self.history.append(f"recover {quote_received_sol:.6f}")
        self._maybe_persist()

    def record_close(self, realized_pnl_sol: float) -> None:
        self.open_positions = max(0, self.open_positions - 1)
        self.realized_pnl_sol += realized_pnl_sol
        self.history.append(f"close {realized_pnl_sol:+.6f}")
        self._maybe_persist()

    def record_partial_close(self, realized_pnl_sol: float) -> None:
        """Bank a staged/partial leg's realized PnL WITHOUT releasing a slot.

        A partial TP/SL leg realizes PnL but the position stays open, so the
        concurrency count must not decrement; the final close releases it.
        Keeping the loss limit live as legs realize prevents the guard from
        understating damage while money is still being extracted.
        """
        self.realized_pnl_sol += realized_pnl_sol
        self.history.append(f"partial-close {realized_pnl_sol:+.6f}")
        self._maybe_persist()

    def record_liquidation_close(self, realized_pnl_sol: float) -> None:
        """Bank a liquidation-queue sell's realized PnL WITHOUT a slot release.

        Rehydrated and sweeper-found orphans never took a concurrency slot
        this session (they are recovery inventory, not live buys), so their
        final sell must bank PnL — the loss limit stays truthful — but must
        NOT decrement open_positions. Decrementing would either go negative
        (clamped) or, worse, release a LIVE position's slot and let the bot
        exceed the concurrent cap (the live-deadlock seen on 2026-10-30 was
        the opposite error: inventory counted against the cap wedged every
        buy until the stale orphans cleared).
        """
        self.realized_pnl_sol += realized_pnl_sol
        self.history.append(f"liquidation-close {realized_pnl_sol:+.6f}")
        self._maybe_persist()

    def record_spend(self, quote_spent_sol: float) -> None:
        """Register quote spent WITHOUT opening a tracked position.

        Bumped for mirror (copy-trader) buys: they spend the wallet's SOL so
        they must count toward ``budget_sol``, but the exit machinery never
        closes them, so they must not inflate ``open_positions`` (that would
        wedge ``max_concurrent_positions`` forever). Mirrors are still gated
        by ``buy_allowed`` first — the loss limit and budget apply to copies.
        """
        self.session_spend_sol += quote_spent_sol
        self.history.append(f"mirror-spend {quote_spent_sol:.6f}")
        self._maybe_persist()

    # ------------------------------------------------------------------
    # Durable state (restart reconciliation): the tracker's session
    # counters are persisted on every mutation so a process restart does
    # not reset the concurrency/budget/loss guards to zero while the
    # wallet still holds open positions (the P0 restart gap).
    # ------------------------------------------------------------------
    _state_path: str | None = None  # set explicitly by the trader (opt-in)

    def _maybe_persist(self) -> None:
        if not self._state_path:
            return
        try:
            p = Path(self._state_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self.to_dict(), indent=2))
        except OSError:
            pass  # best-effort; guards still work in-memory

    def to_dict(self) -> dict:
        return {
            "max_concurrent_positions": self.max_concurrent_positions,
            "daily_loss_limit_sol": self.daily_loss_limit_sol,
            "budget_sol": self.budget_sol,
            "realized_pnl_sol": self.realized_pnl_sol,
            "session_spend_sol": self.session_spend_sol,
            "gross_recovered_sol": self.gross_recovered_sol,
            "open_positions": self.open_positions,
            "blocked_reason": self.blocked_reason,
            "history": list(self.history[-500:]),
        }

    @classmethod
    def load_from(cls, path: str | None) -> "RiskTracker":
        """Restore a tracker from persisted state (missing/corrupt = fresh)."""
        tracker = cls()
        if not path:
            return tracker
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return tracker
        tracker.max_concurrent_positions = int(
            data.get("max_concurrent_positions", 0) or 0
        )
        tracker.daily_loss_limit_sol = float(data.get("daily_loss_limit_sol", 0.0) or 0.0)
        tracker.budget_sol = float(data.get("budget_sol", 0.0) or 0.0)
        tracker.realized_pnl_sol = float(data.get("realized_pnl_sol", 0.0) or 0.0)
        tracker.session_spend_sol = float(data.get("session_spend_sol", 0.0) or 0.0)
        tracker.gross_recovered_sol = float(
            data.get("gross_recovered_sol", 0.0) or 0.0
        )
        tracker.open_positions = int(data.get("open_positions", 0) or 0)
        tracker.blocked_reason = data.get("blocked_reason")
        tracker.history = list(data.get("history", []) or [])
        return tracker

    def _block_reason(self) -> str | None:
        if self.max_concurrent_positions > 0 and (
            self.open_positions >= self.max_concurrent_positions
        ):
            return (
                f"max_concurrent_positions {self.max_concurrent_positions} "
                f"reached ({self.open_positions} open)"
            )
        if self.daily_loss_limit_sol > 0:
            # R5-P0-3: net unrecovered spend is the truthful portfolio-down
            # cut. realized_pnl_sol alone stays positive while buys are held
            # (live evidence 2026-10-30: 19→12 USD with realized +0.0038).
            # Money out minus money back counts the damage whether or not
            # the losing position was sold yet.
            net_unrecovered = self.session_spend_sol - self.gross_recovered_sol
            if net_unrecovered >= self.daily_loss_limit_sol:
                return (
                    f"daily loss limit {-self.daily_loss_limit_sol:.4f} SOL hit "
                    f"(net unrecovered spend {net_unrecovered:.4f}: spent "
                    f"{self.session_spend_sol:.4f}, recovered "
                    f"{self.gross_recovered_sol:.4f})"
                )
            if self.realized_pnl_sol <= -self.daily_loss_limit_sol:
                return (
                    f"daily loss limit {-self.daily_loss_limit_sol:.4f} SOL hit "
                    f"(realized {self.realized_pnl_sol:+.4f})"
                )
        if self.budget_sol > 0 and self.session_spend_sol >= self.budget_sol:
            return (
                f"budget {self.budget_sol:.4f} SOL spent "
                f"({self.session_spend_sol:.4f})"
            )
        return None

    @property
    def buy_allowed(self) -> bool:
        return self._block_reason() is None
