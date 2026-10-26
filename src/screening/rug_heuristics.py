"""P2-EXIT-4: slot-delta rug heuristics (port of hardcap.rs:62-118).

Reference behavior (solana-trading-bot's hardcap.rs): a coin that shows fewer
than 2 unique buyers a defined number of slots after our entry, or whose pool
is drained right after our buy, is a rug in progress — exit immediately.
Slot-granularity beats second-resolution polling.

Memesnipe's monitor loop polls at price_check_interval (seconds, default 10),
so "within 1 slot" is approximated as "at the first post-entry check": the
early-drain test fires whenever the pool's real quote reserves have dropped
more than drain_ratio by the time the first check runs. The unique-buyer test
fires once enough slots have elapsed (check_slots) and the buyer count is
still below minimum.

Off by default: the trader only consults this when ``filters.rug_heuristics``
is enabled.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RugContext:
    """Everything the heuristic needs to judge one position.

    Args:
        entry_slot: Slot our buy landed in (None = unknown; fail open).
        unique_buyers: Distinct wallets observed buying this token since
            entry (listener trade events, only buys count).
        entry_real_quote_lamports: Pool's real quote reserves right after our
            buy (the depth baseline; 0 = unknown).
    """

    entry_slot: int | None
    unique_buyers: int
    entry_real_quote_lamports: float = 0.0


def rug_exit_reason(
    ctx: RugContext,
    current_slot: int | None,
    current_real_quote_lamports: float,
    *,
    min_unique_buyers: int = 2,
    check_slots: int = 12,
    early_drain_ratio: float = 0.5,
) -> str | None:
    """Why the current observation forces a rug exit, or None.

    Both tests fail OPEN on missing data (None = keep the position), because
    a wrong positive exit is worse than missing a rug.

    Args:
        ctx: Entry-time context (slot, buyer count, depth baseline).
        current_slot: Slot right now (None = unknown).
        current_real_quote_lamports: Pool real quote reserves right now.
        min_unique_buyers: Require at least this many distinct buyers.
        check_slots: Slots after entry before the buyer test fires.
        early_drain_ratio: Fraction of entry depth that must be gone for the
            early-drain test to fire (0.5 = half the pool drained).

    Returns:
        A human-readable reason, or None to keep monitoring.
    """
    if ctx.entry_slot is None or current_slot is None:
        return None
    slot_delta = current_slot - ctx.entry_slot
    if slot_delta < 0:
        return None  # clock skew: never judged, fails open

    # Early drain: half the pool gone at the first post-entry check.
    if (
        ctx.entry_real_quote_lamports > 0
        and current_real_quote_lamports
        < ctx.entry_real_quote_lamports * (1 - early_drain_ratio)
    ):
        return (
            f"early SOL drain: pool {current_real_quote_lamports / 1e9:.4f} "
            f"SOL vs {ctx.entry_real_quote_lamports / 1e9:.4f} SOL at entry "
            f"({slot_delta} slots later)"
        )

    # Too few unique buyers well after entry = dev-only wash.
    if slot_delta >= check_slots and ctx.unique_buyers < min_unique_buyers:
        return (
            f"only {ctx.unique_buyers} unique buyer(s) in "
            f"{slot_delta} slots (min {min_unique_buyers})"
        )
    return None