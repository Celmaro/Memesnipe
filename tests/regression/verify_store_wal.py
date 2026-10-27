"""A2-P1-1: SQLite stores run in WAL + synchronous=NORMAL.

The audit §3 flagged that creator_pnl_store / copy_target_store / the ledger
did not configure WAL, so a concurrent writer (migrate_watcher, shadow
capture, the stores themselves) could block the hot-path reader mid-slot.
creator_pnl_store and copy_target_store are SQLite and now set
PRAGMA journal_mode=WAL + synchronous=NORMAL on every connection;
strategies_ledger is append-only JSONL (no SQLite — its analyzer
tools/strategies_report.py already sets WAL).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from screening.copy_target_store import CopyTargetStore  # noqa: E402
from screening.creator_pnl_store import CreatorPnlStore  # noqa: E402


def main() -> int:
    fails = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            fails.append(label)

    with tempfile.TemporaryDirectory() as td:
        store = CreatorPnlStore(Path(td) / "creator.db")
        conn = store._connect()  # noqa: SLF001
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        conn.close()
        check(
            "creator_pnl_store journal_mode=wal",
            str(mode).lower() == "wal",
            f"-> {mode}",
        )
        check(
            "creator_pnl_store synchronous=normal(1)",
            int(sync) == 1,
            f"-> {sync}",
        )

    with tempfile.TemporaryDirectory() as td:
        target = CopyTargetStore(Path(td) / "targets.db")
        conn = target._connect()  # noqa: SLF001
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        conn.close()
        check(
            "copy_target_store journal_mode=wal",
            str(mode).lower() == "wal",
            f"-> {mode}",
        )
        check(
            "copy_target_store synchronous=normal(1)",
            int(sync) == 1,
            f"-> {sync}",
        )

    print("\n" + ("A2-P1-1 store WAL verifier: PASS" if not fails else f"FAIL: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())