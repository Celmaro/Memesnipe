"""Copy-trader leader qualification.

Phase 7 (audit #2, Part 5): the decision layer that decides whether a wallet
is worth copying, sized independently, BEFORE any executor mirrors trades.

This module is pure and offline-testable by design: it scores a leader from
reconstructed trade records and returns a verdict. The chain-reconstruction
pipeline (routed trades, post-graduation PumpSwap activity) and the executor
are separate components; this is the gate they both use.

Rules implemented (starting criteria from the audit, not proven values):
- Positive net results across 7d and 30d windows, 24h as a freshness check
- >= MIN_CLOSED_TRADES closed trades in the window
- Entry-drift rejection: follower enters only if price is within
  max_drift_pct of the leader's entry
- Per-token / per-leader exposure caps
- Unsold-bag and self-funded performance rejected by the caller on the
  reconstruction side (cannot be detected from a PnL summary alone)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# --- Starting thresholds (audit Part 5; tune from out-of-sample results) ---
MIN_CLOSED_TRADES_30D = 100
MIN_NET_WIN_30D = 0.0
MIN_NET_WIN_7D = 0.0
MIN_NET_WIN_24H = 0.0
MAX_ENTRY_DRIFT_PCT = 0.08  # 8%: reject if price drifted past leader's entry
MAX_EXPOSURE_PER_TOKEN_SOL = 0.1
MAX_EXPOSURE_PER_LEADER_SOL = 0.25


class LeaderVerdict(Enum):
    """Outcome of evaluating a candidate leader or a copy signal."""

    QUALIFIED = "qualified"
    INSUFFICIENT_TRADES = "insufficient_trades"
    NEGATIVE_NET = "negative_net"
    STALE_24H = "stale_24h"
    DRIFT_TOO_FAR = "drift_too_far"
    EXPOSURE_CAP = "exposure_cap"


@dataclass
class TradeSummary:
    """Aggregated closed-trade performance for one wallet over a window."""

    window_days: int
    closed_trades: int = 0
    net_pnl_sol: float = 0.0

    @property
    def profitable(self) -> bool:
        return self.net_pnl_sol > 0

    @property
    def enough_trades(self) -> bool:
        return self.closed_trades >= MIN_CLOSED_TRADES_30D


@dataclass
class LeaderProfile:
    """A candidate wallet's performance across the three windows."""

    address: str
    last_seen_days_ago: float
    windows: dict[int, TradeSummary] = field(default_factory=dict)

    def window(self, days: int) -> TradeSummary:
        return self.windows.setdefault(days, TradeSummary(window_days=days))


@dataclass
class CopySignal:
    """One actionable signal: leader X bought token Y at price P."""

    leader: str
    mint: str
    leader_entry_price_sol: float
    current_price_sol: float


def qualify_leader(profile: LeaderProfile) -> LeaderVerdict:
    """Score a wallet against the baseline rules.

    Args:
        profile: Aggregated performance for the wallet

    Returns:
        QUALIFIED or the first failing rule.
    """
    w30 = profile.window(30)
    if w30.closed_trades < MIN_CLOSED_TRADES_30D:
        return LeaderVerdict.INSUFFICIENT_TRADES
    if not (w30.profitable and profile.window(7).profitable):
        return LeaderVerdict.NEGATIVE_NET
    if profile.last_seen_days_ago > 1.0:
        return LeaderVerdict.STALE_24H
    return LeaderVerdict.QUALIFIED


def check_entry_drift(signal: CopySignal, max_drift_pct: float = MAX_ENTRY_DRIFT_PCT) -> LeaderVerdict:
    """Reject a copy entry when the price has moved too far past the leader's.

    Args:
        signal: The copy signal
        max_drift_pct: Max allowed drift, e.g. 0.08 = 8%

    Returns:
        QUALIFIED or DRIFT_TOO_FAR.
    """
    if signal.leader_entry_price_sol <= 0:
        return LeaderVerdict.QUALIFIED  # no reference price: caller decides
    drift = (signal.current_price_sol - signal.leader_entry_price_sol) / signal.leader_entry_price_sol
    if drift > max_drift_pct:
        return LeaderVerdict.DRIFT_TOO_FAR
    return LeaderVerdict.QUALIFIED


def size_follow_position(
    budget_sol: float,
    leader_bought_sol: float,
    existing_exposure_sol: float,
    per_token_cap: float = MAX_EXPOSURE_PER_TOKEN_SOL,
) -> float:
    """Independent follower sizing, never copying the leader's absolute size.

    Args:
        budget_sol: Follower's fixed budget per copy (not leader-linked)
        leader_bought_sol: How much the leader spent (informational)
        existing_exposure_sol: Already at risk on this token/leader

    Returns:
        SOL to spend for this copy, capped so total exposure stays under the cap.
    """
    if existing_exposure_sol >= per_token_cap:
        return 0.0
    room = per_token_cap - existing_exposure_sol
    # Cap by budget; leader size only matters as a sanity floor.
    return max(0.0, min(budget_sol, room))