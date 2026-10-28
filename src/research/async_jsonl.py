"""Buffered persistent-handle JSONL line writer for research ledgers (C8.5).

The pre-C8.5 shadow capture and strategies ledger opened the daily JSONL
file on EVERY emit and wrote synchronously on the asyncio loop (see
shadow_capture._writer / strategies_ledger._emit). universal_trader emits
several rows per tick, so each tick paid an open/close syscall pair plus a
blocking write on the loop. This module replaces that pattern with a
single append-mode handle held open per UTC day, an in-memory buffer, and a
lock-guarded flush policy:

- flush when the buffer holds ``flush_every_rows`` rows, OR
- flush every ``flush_every_ms`` via a background asyncio task (started
  lazily on the first write that observes a running event loop), OR
- flush() / close() called explicitly.

The API is fully synchronous (the caller's emit methods are sync — the
trader calls sim_open/detect/decide without awaiting) so it works both on
the asyncio loop and in plain script context (regression verifiers run it
without a loop). When NO event loop is running the writer degrades to
write-through (every row hits disk immediately, exactly like the
pre-C8.5 behavior), so sync callers that never call flush() still see
their rows. A :class:`threading.RLock` makes it safe for concurrent tasks
(asyncio tasks never interleave inside a held section, and the lock also
covers threads).

Rotation is handled per-day lazily: the filename embeds the UTC date, and
the writer reopens ``<dir>/<date>.jsonl`` whenever the date changes; an
explicit :meth:`AsyncJsonlWriter.rotate` forces the cycle early. Write
failures are swallowed (research-ledger contract: capture is best-effort
and must never raise into the trade path).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, TextIO


class AsyncJsonlWriter:
    """Append-mode JSONL writer with a persistent handle and buffered flush.

    Args:
        dir_path: Directory that receives ``<UTC date>.jsonl`` files
            (created on first write).
        enabled: When False every method is a silent no-op.
        flush_every_rows: Flush after this many buffered rows.
        flush_every_ms: Flush at most this stale (checked lazily on write).
    """

    def __init__(
        self,
        dir_path: str | Path,
        enabled: bool = True,
        flush_every_rows: int = 100,
        flush_every_ms: int = 1000,
    ) -> None:
        self.dir_path = Path(dir_path)
        self.enabled = enabled
        self.flush_every_rows = max(1, int(flush_every_rows))
        self.flush_every_ms = max(1, int(flush_every_ms))
        self._lock = RLock()
        self._handle: TextIO | None = None
        self._day: str | None = None  # UTC date the open handle belongs to
        self._buffer: list[str] = []
        self._flush_task: asyncio.Task[None] | None = None
        self._closed = False

    # -- internal -----------------------------------------------------------

    def _today(self) -> str:
        return datetime.now(UTC).date().isoformat()

    def _open_for(self, day: str) -> TextIO:
        """Open (or reopen) the append handle for ``day``'s daily file."""
        if self._handle is not None and self._day == day:
            return self._handle
        if self._handle is not None:
            try:
                self._handle.close()
            except Exception:
                pass
        self.dir_path.mkdir(parents=True, exist_ok=True)
        self._handle = (self.dir_path / f"{day}.jsonl").open(
            "a", encoding="utf-8"
        )
        self._day = day
        return self._handle

    def _flush_locked(self) -> None:
        """Write buffered lines to the current day's handle (lock held)."""
        if not self._buffer or self._handle is None:
            return
        try:
            self._handle.write("".join(self._buffer))
            self._handle.flush()
        finally:
            self._buffer.clear()

    def _running_loop(self) -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _ensure_flush_task(self) -> None:
        """Start the periodic 1s flush task once (loop context only).

        Must be called with the lock held; safe to call repeatedly.
        """
        if self._flush_task is not None or self._closed:
            return
        loop = self._running_loop()
        if loop is None:
            return  # sync context: write() degrades to write-through
        self._flush_task = loop.create_task(self._periodic_flush())

    async def _periodic_flush(self) -> None:
        """Background task: persist buffered rows every flush_every_ms."""
        try:
            while not self._closed:
                await asyncio.sleep(self.flush_every_ms / 1000.0)
                self.flush()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    # -- public API ---------------------------------------------------------

    def write(self, event_type: str, **fields: Any) -> None:
        """Serialize one JSON row into the buffer; flush per policy.

        On the asyncio loop rows are buffered (flushed every
        ``flush_every_rows`` rows or every ``flush_every_ms`` via the
        background task). With no running loop the writer degrades to
        write-through so sync callers see every row immediately. Never
        raises (best-effort research data).
        """
        if not self.enabled or self._closed:
            return
        try:
            line = json.dumps(
                {"type": event_type, **fields}, default=str
            ) + "\n"
        except Exception:
            return
        with self._lock:
            if self._closed:
                return
            day = self._today()
            if self._day is None:
                # Lazy per-day open: first write decides today's file.
                self._open_for(day)
            elif self._day != day:
                # Date rollover: flush to the old day's file, reopen today's.
                self._flush_locked()
                self._open_for(day)
            self._buffer.append(line)
            if self._running_loop() is None:
                # Sync context (verifiers / non-async callers): no periodic
                # task exists, so persist now to keep rows observable.
                self._flush_locked()
                return
            self._ensure_flush_task()
            if len(self._buffer) >= self.flush_every_rows:
                self._flush_locked()

    def flush(self) -> None:
        """Persist buffered rows to disk now (never raises)."""
        if not self.enabled:
            return
        with self._lock:
            self._flush_locked()

    def rotate(self) -> None:
        """Close the current handle so the next write reopens today's file.

        The per-day lazy open already handles date rollover automatically;
        this forces the cycle for callers that need an explicit boundary
        (e.g. tests or day-end jobs). Buffered rows are flushed first.
        """
        if not self.enabled:
            return
        with self._lock:
            self._flush_locked()
            if self._handle is not None:
                try:
                    self._handle.close()
                except Exception:
                    pass
                self._handle = None
                self._day = None

    def close(self) -> None:
        """Flush and close the persistent handle (idempotent)."""
        if not self.enabled:
            return
        with self._lock:
            if self._closed:
                return
            task = self._flush_task
            self._flush_task = None
            if task is not None:
                task.cancel()
            self._flush_locked()
            self._closed = True
            if self._handle is not None:
                try:
                    self._handle.close()
                except Exception:
                    pass
                self._handle = None
                self._day = None

    def __enter__(self) -> "AsyncJsonlWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()