"""Execution-ledger net metrics (R3-P1-1).

Reads the research JSONL ledger (`shadow_data/YYYY-MM-DD.jsonl`) — and,
when `real_trades_dir` is given, the bot's real `trades/` files — and
computes the audit's metric set:

- realized PnL per closed trade (quote units) and % per trade
- profit factor (gross wins / gross losses)
- max drawdown on the cumulative equity curve
- fill rate (closed / opened)
- median hold time (seconds)
- unsellable share (opens with no matching close, e.g. dust / stuck sells)

Every metric is computed from the ledger's own `sim_open`/`sim_close`
events (or the real-trade equivalents); nothing is guessed. A ledger with
zero closed trades returns an explicit "insufficient data" state instead of
dividing by zero.

Real-trade format expected when `real_trades_dir` is used: JSONL rows with
`type` in {"open","close"} and the same fields as the sim events (the
trades/ writer emits them when armed). Rows that fail to parse are skipped
and counted; a wholly-unparseable file is reported, not raised on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median


@dataclass
class TradeMetrics:
    """Net-metric set for one ledger slice (session / day / all)."""

    source: str = ""
    opened: int = 0
    closed: int = 0
    unsellable: int = 0
    unparseable_rows: int = 0
    total_pnl_quote: float = 0.0
    gross_wins: float = 0.0
    gross_losses: float = 0.0
    winning_trades: int = 0
    losing_trades: int = 0
    equity_curve: list[float] = field(default_factory=list)
    hold_seconds: list[float] = field(default_factory=list)
    # R4-P0-4/1: race KPIs collected from the shadow ledger.
    landed_slot_offsets: list[int] = field(default_factory=list)
    reordering_bps_values: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def profit_factor(self) -> float | None:
        if self.gross_losses <= 0:
            return None if self.gross_wins <= 0 else float("inf")
        return self.gross_wins / self.gross_losses

    @property
    def fill_rate(self) -> float | None:
        if self.opened <= 0:
            return None
        return self.closed / self.opened

    @property
    def median_hold_seconds(self) -> float | None:
        if not self.hold_seconds:
            return None
        return float(median(self.hold_seconds))

    @property
    def max_drawdown(self) -> float:
        """Max peak-to-trough drop on the cumulative equity curve (quote)."""
        peak = 0.0
        worst = 0.0
        for v in self.equity_curve:
            peak = max(peak, v)
            if peak > 0:
                worst = max(worst, peak - v)
        return worst

    @property
    def avg_pnl_quote_per_trade(self) -> float | None:
        if self.closed <= 0:
            return None
        return self.total_pnl_quote / self.closed

    @property
    def win_rate(self) -> float | None:
        if self.closed <= 0:
            return None
        return self.winning_trades / self.closed

    @property
    def median_landed_slot_offset(self) -> int | None:
        """Median decision-lag in slots (0-2 = winning the race)."""
        if not self.landed_slot_offsets:
            return None
        return int(median(self.landed_slot_offsets))

    @property
    def avg_reordering_bps(self) -> float | None:
        """Mean reordering-slippage over closed fills (bps)."""
        if not self.reordering_bps_values:
            return None
        return sum(self.reordering_bps_values) / len(self.reordering_bps_values)

    def to_report(self) -> str:
        lines = [f"Execution metrics: {self.source}"]
        lines.append(f"  opened {self.opened}, closed {self.closed}, unsellable {self.unsellable}")
        if self.closed == 0:
            lines.append("  insufficient closed trades for PnL metrics")
        else:
            lines.append(f"  total PnL {self.total_pnl_quote:+.6f} quote")
            lines.append(
                f"  avg PnL/trade {self.avg_pnl_quote_per_trade:+.6f} quote"
            )
            lines.append(
                f"  profit factor {self.profit_factor:.2f}"
                if self.profit_factor is not None
                else "  profit factor inf (no losses)"
            )
            lines.append(f"  win rate {self.win_rate:.1%}" if self.win_rate is not None else "  win rate n/a")
            lines.append(f"  max drawdown {self.max_drawdown:.6f} quote")
            lines.append(
                f"  fill rate {self.fill_rate:.1%}"
                if self.fill_rate is not None
                else "  fill rate n/a"
            )
            lines.append(
                f"  median hold {self.median_hold_seconds:.1f}s"
                if self.median_hold_seconds is not None
                else "  median hold n/a"
            )
        lines.append(
            f"  median landed-slot offset {self.median_landed_slot_offset}"
            if self.median_landed_slot_offset is not None
            else "  median landed-slot offset n/a"
        )
        lines.append(
            f"  avg reordering slippage {self.avg_reordering_bps:.1f} bps"
            if self.avg_reordering_bps is not None
            else "  avg reordering slippage n/a"
        )
        if self.unparseable_rows:
            lines.append(f"  {self.unparseable_rows} unparseable rows skipped")
        for err in self.errors:
            lines.append(f"  ERROR {err}")
        return "\n".join(lines)


def _iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                yield None


def compute_metrics(
    ledgers: list[Path] | None = None,
    *,
    real_trades_dir: str | Path | None = None,
    source: str = "ledger",
) -> TradeMetrics:
    """Compute net metrics from shadow JSONL files and/or real trades dir.

    Args:
        ledgers: Shadow JSONL files to scan (defaults to all
            ``shadow_data/*.jsonl`` under the working directory).
        real_trades_dir: Optional dir of real trade JSONL rows
            (``type`` open/close). When given, its rows are merged.
        source: Label for the report.

    Returns:
        TradeMetrics with the audit metric set. Never raises for a missing
        or corrupt ledger; parse failures are counted, not fatal.
    """
    m = TradeMetrics(source=source)
    opened: dict[str, float] = {}  # mint -> quote_spent at open
    hold_start: dict[str, float] = {}
    equity = 0.0
    # P0-2: per-position partial-leg accumulation. Keyed by position_id when
    # present, else mint (legacy rows). A partial leg must not count as a
    # full close; only the final close of a position (no is_partial flag or
    # is_partial falsy) increments closed and pops opened.
    leg_pnl: dict[str, float] = {}
    leg_hold: dict[str, float] = {}

    files: list[Path] = []
    if ledgers is not None:
        files.extend(ledgers)
    elif Path("shadow_data").is_dir():
        files.extend(sorted(Path("shadow_data").glob("*.jsonl")))
    if real_trades_dir:
        rd = Path(real_trades_dir)
        if rd.is_dir():
            files.extend(sorted(rd.glob("*.jsonl")))

    for path in files:
        for row in _iter_jsonl(path):
            if row is None:
                m.unparseable_rows += 1
                continue
            etype = row.get("type")
            mint = str(row.get("mint", ""))
            if etype == "sim_open" or etype == "open":
                m.opened += 1
                opened[mint] = float(row.get("quote_spent", 0.0) or 0.0)
                ts = row.get("ts", 0.0) or 0.0
                # R4-P0-4: landed-slot offset on the open row (None for
                # non-geyser listeners / live rows without the field).
                lso = row.get("landed_slot_offset")
                if isinstance(lso, (int, float)):
                    m.landed_slot_offsets.append(int(lso))
                try:
                    hold_start[mint] = float(ts)
                except (TypeError, ValueError):
                    # ISO-8601 timestamp from the research ledger: unused
                    # downstream (hold_seconds rides on the row), so park a
                    # sentinel rather than crashing the metrics report.
                    hold_start[mint] = 0.0
            elif etype == "sim_close" or etype == "close":
                # P0-2: identify the position. New rows carry position_id;
                # legacy rows fall back to mint (mint-unique per position in
                # the common case).
                pkey = row.get("position_id") or mint
                is_partial = bool(row.get("is_partial", False))
                leg = float(row.get("pnl_quote", 0.0) or 0.0)
                if is_partial:
                    # Accumulate the leg; do NOT count as a close yet.
                    leg_pnl[pkey] = leg_pnl.get(pkey, 0.0) + leg
                    hs = row.get("hold_seconds")
                    if hs is not None:
                        try:
                            leg_hold[pkey] = float(hs)
                        except (TypeError, ValueError):
                            pass
                    continue
                # Final close: realize accumulated partial legs + this leg.
                pnl = leg_pnl.pop(pkey, 0.0) + leg
                leg_hold.pop(pkey, None)
                m.closed += 1
                # R4-P0-1: reordering-slippage basis on the close row.
                rb = row.get("reordering_bps")
                if isinstance(rb, (int, float)):
                    m.reordering_bps_values.append(float(rb))
                m.total_pnl_quote += pnl
                if pnl >= 0:
                    m.gross_wins += pnl
                    m.winning_trades += 1
                else:
                    m.gross_losses += -pnl
                    m.losing_trades += 1
                equity += pnl
                m.equity_curve.append(equity)
                hs = row.get("hold_seconds")
                if hs is not None:
                    try:
                        m.hold_seconds.append(float(hs))
                    except (TypeError, ValueError):
                        pass
                opened.pop(mint, None)
                hold_start.pop(mint, None)
            elif etype == "summary":
                continue
            else:
                # Unknown event type: count it as noise, keep going.
                m.unparseable_rows += 1

    m.unsellable = len(opened)
    return m


def compute_coverage(
    ledgers: list[Path] | None = None,
) -> dict[str, float | None]:
    """R4-P4-1: rejection-stream coverage metric over the shadow ledger.

    Two observation pipelines (the snipe path and the copy/monitor paths)
    can see disjoint mint sets — the 2609.18975 result showed a 0.8%
    overlap between two well-built pipelines. This computes Jaccard and
    Dice overlap between:

    - ``detected``: every mint the snipe path saw (detect rows)
    - ``rejected``: mints the detectors saw but filters refused
      (detect rows with skip_reason set) — what the filters throw away

    A high ``rejected_mint_share`` + low overlap means the filter stack is
    discarding most of what an alternative observer might see; it is the
    quickest blind-spot check on the shadow ledger.
    """
    detected: set[str] = set()
    rejected: set[str] = set()
    files: list[Path] = []
    if ledgers is not None:
        files.extend(ledgers)
    elif Path("shadow_data").is_dir():
        files.extend(sorted(Path("shadow_data").glob("*.jsonl")))
    for path in files:
        for row in _iter_jsonl(path):
            if row is None or row.get("type") != "detect":
                continue
            mint = str(row.get("mint", ""))
            if not mint:
                continue
            detected.add(mint)
            if row.get("skip_reason"):
                rejected.add(mint)
    overlap = detected & rejected
    jaccard = (
        len(overlap) / len(detected | rejected)
        if (detected | rejected)
        else None
    )
    dice = (
        2 * len(overlap) / (len(detected) + len(rejected))
        if (len(detected) + len(rejected)) > 0
        else None
    )
    return {
        "detected": len(detected),
        "rejected": len(rejected),
        "overlap": len(overlap),
        "jaccard": jaccard,
        "dice": dice,
    }


if __name__ == "__main__":
    import sys

    metrics = compute_metrics(
        real_trades_dir=sys.argv[1] if len(sys.argv) > 1 else None
    )
    print(metrics.to_report())
