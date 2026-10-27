"""R4-P2-3: copy-leader quality scoring.

The copy engine mirrors wallets from state/copy_targets.db. Leader selection
evidence (Dune leaderboard v2, Baldwin's bot filter, 2601.08641 KOL
selection) says profitable copy candidates share properties a simple score
can capture from a profile or observation window:

- realized PnL positive (and mostly realized, not paper)
- low trade frequency (a wallet doing 472 tokens/week is a market-maker
  bot, not a discerning trader)
- spread PnL (not one-hit wonders; low concentration preferred)
- reasonable hold times

`score_leader` maps a stat dict to 0..1. The engine gates mirroring on
`copy_trader.min_leader_score` when set (0 = off). Pure math — the caller
gathers the stats (profiler, on-chain crawl, or the store's own records).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LeaderStats:
    """Inputs for the quality score; None = unknown (neutral)."""

    realized_pnl_quote: float | None = None
    total_trades_30d: int | None = None
    tokens_30d: int | None = None
    win_rate: float | None = None  # 0..1
    pnl_concentration: float | None = None  # Herfindahl 0..1 (high = one hit)
    realization_ratio: float | None = None  # realized / (realized+paper)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _pnl_score(pnl: float | None) -> float:
    if pnl is None:
        return 0.5  # unknown neutral
    # Scale: +1 SOL of realized profit over 30d = meaningful at our test
    # sizes; saturate at +10 SOL. Negative is a hard bad.
    if pnl <= 0:
        return 0.0
    return _clamp01(pnl / 10.0)


def _frequency_score(tokens_30d: int | None, total_trades_30d: int | None) -> float:
    """Reject bot-frequency wallets; prefer low-frequency consistent traders.

    Baldwin: 472 tokens/week (≈67/day) is a bot. 2601.08641: KOL = low
    frequency. Score is 1.0 under ~20 tokens/30d, decays to 0 by ~300.
    """
    if tokens_30d is None:
        return 0.5
    if tokens_30d <= 20:
        return 1.0
    return _clamp01(1.0 - (tokens_30d - 20) / 280.0)


def _concentration_score(hhi: float | None) -> float:
    """Herfindahl over PnL by token; low = spread (preferred)."""
    if hhi is None:
        return 0.5
    return _clamp01(1.0 - hhi)


def _realization_score(ratio: float | None) -> float:
    """Realized share of PnL; >= 0.5 required by Dune leaderboard v2."""
    if ratio is None:
        return 0.5
    if ratio < 0.5:
        return _clamp01(ratio / 0.5)  # below 0.5 = suspect (paper gains)
    return 1.0


def score_leader(stats: LeaderStats) -> float:
    """Composite 0..1 leader quality score (equal weights, mean)."""
    parts = [
        _pnl_score(stats.realized_pnl_quote),
        _frequency_score(stats.tokens_30d, stats.total_trades_30d),
        _concentration_score(stats.pnl_concentration),
        _realization_score(stats.realization_ratio),
    ]
    return sum(parts) / len(parts)