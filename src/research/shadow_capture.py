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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


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

    def __init__(self, dir_path: str | Path = "shadow_data", enabled: bool = True):
        self.dir_path = Path(dir_path)
        self.enabled = enabled
        self._file: Path | None = None

    def _check(self) -> bool:
        return self.enabled

    def _writer(self):
        """Yield a text-mode file handle for today's JSONL (create parents)."""
        if not self._check():
            return None
        self.dir_path.mkdir(parents=True, exist_ok=True)
        self._file = self.dir_path / f"{datetime.now(UTC):%Y-%m-%d}.jsonl"
        return self._file.open("a", encoding="utf-8")

    def _emit(self, event_type: str, **fields: Any) -> None:
        if not self._check():
            return
        try:
            fh = self._writer()
            if fh is None:
                return
            row = {"type": event_type, "ts": _now(), **fields}
            with fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except Exception:
            # Capture is research data; a disk/JSON failure must never break
            # the shadow trade itself.
            return

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