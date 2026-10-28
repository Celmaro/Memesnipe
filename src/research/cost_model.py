"""Net-of-cost model for shadow/research PnL (R5-P1-3).

Pump.fun charges a trading fee per side (base tier ~1.25% per trade); the
Solana network adds the transaction fee + priority fee. A backtest or shadow
close that ignores these overstates every exit outcome, so every PnL that
leaves research must pass through :func:`net_round_trip` before it is trusted
for ranking or blacklist evidence.

``gross_pnl`` is a fraction (0.20 = +20% price move on the full position).
Round-trip net = gross scaled by the two (1 - fee) legs, minus the network
cost as a fraction of the quote size. ``sol_quote`` is the position size in
SOL (0.01 shadow default); the absolute network cost from
:func:`network_fee_sol` is divided by it so the fraction is meaningful at any
test size.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    """One set of fee assumptions. Defaults follow the audit §6 base tier."""

    buy_fee: float = 0.0125  # pump.fun protocol+creator fee on the buy side
    sell_fee: float = 0.0125  # pump.fun fee on the sell side
    network_fee_sol: float = 0.000028  # round-trip txn + priority (~0.28% @ 0.01)

    def round_trip_retained(self) -> float:
        """Fraction of gross proceeds kept after both trading fees."""
        return (1.0 - self.buy_fee) * (1.0 - self.sell_fee)

    def net_round_trip(self, gross_pnl: float, sol_quote: float) -> float:
        """Net PnL fraction for a gross return on a position of ``sol_quote``.

        A +20% gross move yields 1.2 * retained - 1 - network/quote; the
        network term shrinks as the position grows (0.28% at 0.01 SOL).
        """
        gross_mult = 1.0 + gross_pnl
        net = gross_mult * self.round_trip_retained() - 1.0
        net -= self.network_fee_sol / sol_quote if sol_quote > 0 else 0.0
        return net


_DEFAULT = CostModel()


def net_round_trip(
    gross_pnl: float,
    sol_quote: float,
    *,
    buy_fee: float | None = None,
    sell_fee: float | None = None,
    network_fee_sol: float | None = None,
) -> float:
    """Net PnL fraction for a gross return, on the default cost model.

    Shorthand for the production shadow size. C4.8: per-run parameterization
    — pass ``buy_fee``/``sell_fee``/``network_fee_sol`` to override the
    corresponding default (``CostModel`` defaults keep current values);
    omitted overrides fall back, so existing call sites are unchanged.
    """
    if buy_fee is None and sell_fee is None and network_fee_sol is None:
        return _DEFAULT.net_round_trip(gross_pnl, sol_quote)
    model = CostModel(
        buy_fee=_DEFAULT.buy_fee if buy_fee is None else buy_fee,
        sell_fee=_DEFAULT.sell_fee if sell_fee is None else sell_fee,
        network_fee_sol=(
            _DEFAULT.network_fee_sol if network_fee_sol is None else network_fee_sol
        ),
    )
    return model.net_round_trip(gross_pnl, sol_quote)