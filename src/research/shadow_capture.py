"""Shadow-mode data capture (2026-10-26): append-only JSONL research ledger.

Shadow mode currently simulates a paper fill and runs the exit machinery,
but the ONLY record it leaves is a log line — nothing a backtester can
replay. This module writes a structured, append-only JSONL file
(``shadow_data/YYYY-MM-DD.jsonl``) capturing three classes of events:

- ``detect``: every token the detector saw (creator, mint, platform, quote,
  transfer fee, listener) — the raw opportunity set. Includes the skip
  reason when a filter refused it, so backtests can measure what the
  filters throw away.
- ``sim_open`` / ``sim_close``: a shadow buy (simulated fill at the live
  curve price) and its shadow sell (simulated at the read price), each with
  the pool snapshot (virtual/real reserves, graduated, mayhem, cashback)
  and the position PnL when it closes.
- ``summary``: one row per session end aggregating what was captured.

Every row carries an ISO timestamp and the token's mint as the join key —
``detect`` rows for the SAME mint chain into ``sim_open``/``sim_close`` rows
so a replay can measure "seen" vs "traded" and each trade's full pool
context. Write failures are never fatal: capture is best-effort research
data, and shadow mode must keep working even if the disk is full.

The file lives under ``shadow_data/`` (gitignored, like logs) so a research
run never pollutes the repo.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research.async_jsonl import AsyncJsonlWriter


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ShadowDataCapture:
    """Append-only JSONL writer for shadow-mode research events.

    Args:
        dir_path: Directory holding the daily JSONL files. Created on first
            write. Defaults to ``shadow_data/`` under the working directory.
        enabled: When False, every method is a silent no-op (the off path is
            byte-identical to no capture at all).
    """

    def __init__(
        self,
        dir_path: str | Path = "shadow_data",
        enabled: bool = True,
        *,
        flush_every_rows: int = 100,
        flush_every_ms: int = 1000,
    ) -> None:
        self.dir_path = Path(dir_path)
        self.enabled = enabled
        # C8.5: buffered persistent-handle writer (one open fd per UTC day,
        # flushed every N rows or 1s) instead of open-per-emit writes on the
        # asyncio loop.
        self._writer = AsyncJsonlWriter(
            dir_path=dir_path,
            enabled=enabled,
            flush_every_rows=flush_every_rows,
            flush_every_ms=flush_every_ms,
        )

    def _check(self) -> bool:
        return self.enabled

    def _emit(self, event_type: str, **fields: Any) -> None:
        """Buffer one row; flush policy handled inside the writer."""
        if not self._check():
            return
        # Capture is research data; the writer swallows write failures so a
        # disk/JSON failure must never break the shadow trade itself.
        self._writer.write(event_type, ts=_now(), **fields)

    def flush(self) -> None:
        """Persist buffered rows now (never raises)."""
        self._writer.flush()

    def close(self) -> None:
        """Flush and release the daily file handle (idempotent)."""
        self._writer.close()

    def rotate(self) -> None:
        """Force a daily-file rotation boundary (rows flushed first)."""
        self._writer.rotate()

    def detect(
        self,
        mint: str,
        symbol: str,
        platform: str,
        quote_mint: str,
        creator: str | None,
        transfer_fee_bps: int,
        listener: str,
        skip_reason: str | None = None,
        **extra: Any,
    ) -> None:
        """One token the detector saw, with optional skip reason."""
        self._emit(
            "detect",
            mint=mint,
            symbol=symbol,
            platform=platform,
            quote_mint=quote_mint,
            creator=creator,
            transfer_fee_bps=transfer_fee_bps,
            listener=listener,
            skip_reason=skip_reason,
            **extra,
        )

    def sim_open(
        self,
        mint: str,
        symbol: str,
        platform: str,
        price: float,
        amount_tokens: float,
        quote_spent: float,
        pool_snapshot: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        """A shadow buy filled at the live curve price."""
        self._emit(
            "sim_open",
            mint=mint,
            symbol=symbol,
            platform=platform,
            price=price,
            amount_tokens=amount_tokens,
            quote_spent=quote_spent,
            pool_snapshot=pool_snapshot or {},
            **extra,
        )

    def sim_close(
        self,
        mint: str,
        symbol: str,
        platform: str,
        price: float,
        amount_tokens: float,
        quote_received: float,
        exit_reason: str,
        pnl_quote: float,
        pnl_pct: float,
        hold_seconds: float,
        pool_snapshot: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        """A shadow sell (or exit) at the read price, with realized PnL."""
        self._emit(
            "sim_close",
            mint=mint,
            symbol=symbol,
            platform=platform,
            price=price,
            amount_tokens=amount_tokens,
            quote_received=quote_received,
            exit_reason=exit_reason,
            pnl_quote=pnl_quote,
            pnl_pct=pnl_pct,
            hold_seconds=hold_seconds,
            pool_snapshot=pool_snapshot or {},
            **extra,
        )

    def summary(self, **fields: Any) -> None:
        """One aggregate row at session end (seen/traded/skipped counts)."""
        self._emit("summary", **fields)

    def sweep_zombies(self) -> int:
        """Force-close sims left open by a previous process (restart orphans).

        Restarts wipe the in-memory position registry, so positions from the
        prior run are never marked or closed again ("zombies" — they inflate
        open-position counts). Reads all daily JSONLs, replays sim_open minus
        sim_close per mint, and emits a sim_close at 0% with
        exit_reason=orphan_restart. Returns the number swept.
        """
        if not self._check():
            return 0
        try:
            open_counts: Counter[str] = Counter()
            symbols: dict[str, str] = {}
            for f in sorted(self.dir_path.glob("*.jsonl")):
                with f.open(encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        t = row.get("type")
                        if t == "sim_open":
                            open_counts[row["mint"]] += 1
                            symbols[row["mint"]] = row.get("symbol", "?")
                        elif t == "sim_close":
                            # P0-2: a partial leg leaves the position open —
                            # only a final (non-partial) close decrements, so
                            # a partial-only orphan is still swept below.
                            if not row.get("is_partial"):
                                open_counts[row["mint"]] -= 1
            swept = 0
            for mint, n in open_counts.items():
                for _ in range(max(0, n)):
                    self.sim_close(
                        mint=mint,
                        symbol=symbols.get(mint, "?"),
                        platform="pump_fun",
                        price=0.0,
                        amount_tokens=0.0,
                        quote_received=0.0,
                        exit_reason="orphan_restart",
                        pnl_quote=0.0,
                        pnl_pct=0.0,
                        hold_seconds=0.0,
                    )
                    swept += 1
            if swept:
                self._emit("summary", note="zombie_sweep", swept=swept)
            return swept
        except Exception:
            return 0

    def mark(
        self,
        *,
        mint: str,
        symbol: str,
        platform: str,
        price: float,
        pnl_pct: float,
        pnl_quote: float,
        hold_seconds: float,
    ) -> None:
        """Unrealized-PnL tick for an open shadow position (research data).

        Emitted on a cadence from the monitor loop, so a replay can trace the
        price path between sim_open and sim_close instead of only the
        endpoints. Best-effort like every capture method.
        """
        self._emit(
            "sim_mark",
            mint=mint,
            symbol=symbol,
            platform=platform,
            price=price,
            pnl_pct=pnl_pct,
            pnl_quote=pnl_quote,
            hold_seconds=hold_seconds,
        )
