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

from dataclasses import dataclass, field


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

    def record_buy(self, quote_spent_sol: float) -> bool:
        """Register a buy; returns False when a guard blocks it first."""
        reason = self._block_reason()
        if reason:
            self.blocked_reason = reason
            return False
        self.open_positions += 1
        self.session_spend_sol += quote_spent_sol
        self.history.append(f"buy {quote_spent_sol:.6f}")
        return True

    def record_close(self, realized_pnl_sol: float) -> None:
        self.open_positions = max(0, self.open_positions - 1)
        self.realized_pnl_sol += realized_pnl_sol
        self.history.append(f"close {realized_pnl_sol:+.6f}")

    def _block_reason(self) -> str | None:
        if self.max_concurrent_positions > 0 and (
            self.open_positions >= self.max_concurrent_positions
        ):
            return (
                f"max_concurrent_positions {self.max_concurrent_positions} "
                f"reached ({self.open_positions} open)"
            )
        if self.daily_loss_limit_sol > 0 and (
            self.realized_pnl_sol <= -self.daily_loss_limit_sol
        ):
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
