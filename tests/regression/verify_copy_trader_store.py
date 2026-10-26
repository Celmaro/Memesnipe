"""T3.4/P2-COP-1 regression: copy-target store filters + percent sizing.

Reference bug (open-sol-bot's gmgn adapter): the active-wallets query built its
WHERE clause with Python ``and`` on SQLAlchemy expressions — Python ``and``
short-circuits on truthy expression objects, so the wallet-equality filter was
silently dropped and the store returned EVERY wallet, mirroring trades for
leaders the user never opted into.

Our store is plain sqlite3 (no SQLAlchemy), so the guard here is behavioral:
- upsert + get_active_wallets round-trips with correct filters (enabled=1)
- disabled targets are never returned by the active-wallets query
- get_by_wallet returns exactly the requested row (metadata, incl. enabled=False)
- a disabled leader never appears in the active set (the exact bug shape)
- P2-COP-1: percent_of_leader column round-trips, and pre-migration DBs get
  the column ALTERed in (schema migration guard)
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from copy_trader.engine import _mirror_size  # noqa: E402
from screening.copy_target_store import CopyTarget, CopyTargetStore  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "copy_targets.db"
        store = CopyTargetStore(db)

        # Seed: two enabled, one disabled.
        store.upsert(
            CopyTarget(wallet="walletAAA", alias="alpha", enabled=True, mode="full")
        )
        store.upsert(
            CopyTarget(wallet="walletBBB", alias="beta", enabled=True, mode="buy_only")
        )
        store.upsert(
            CopyTarget(wallet="walletCCC", alias="gamma", enabled=False, mode="full")
        )

        active = store.get_active_wallets()
        check(
            "active wallets exclude disabled",
            [t.wallet for t in active] == ["walletAAA", "walletBBB"],
            f"got {[t.wallet for t in active]}",
        )

        check(
            "active wallets carry full mode/limits",
            next(t for t in active if t.wallet == "walletAAA").mode == "full",
        )

        by_wallet = store.get_by_wallet("walletBBB")
        check(
            "get_by_wallet returns exactly one row",
            by_wallet is not None and by_wallet.alias == "beta",
            f"got {by_wallet}",
        )

        check(
            "get_by_wallet for absent wallet returns None",
            store.get_by_wallet("nope") is None,
        )

        # The exact reference-bug shape: active-wallets must never include a
        # leader the user disabled. If the WHERE clause were dropped (Python
        # `and` bug), get_active_wallets would return all three rows and mirror
        # an un-opted-in leader.
        targeted = store.get_by_wallet("walletCCC")
        check(
            "disabled leader row carries enabled=False",
            targeted is not None and targeted.enabled is False,
            f"got {targeted}",
        )
        check(
            "disabled leader absent from active set",
            all(t.wallet != "walletCCC" for t in store.get_active_wallets()),
        )

        # Persistence across reopen.
        store2 = CopyTargetStore(db)
        check(
            "rows persist across store instances",
            len(store2.get_active_wallets()) == 2,
        )

    # P2-COP-1: percent_of_leader round-trip + migration on an old-schema DB.
    with tempfile.TemporaryDirectory() as td:
        legacy = Path(td) / "legacy.db"
        conn = sqlite3.connect(str(legacy))
        conn.execute(
            """
            CREATE TABLE copy_targets (
                wallet TEXT PRIMARY KEY,
                alias TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                mode TEXT NOT NULL DEFAULT 'buy_only',
                fixed_buy_sol REAL NOT NULL DEFAULT 0,
                max_position_sol REAL NOT NULL DEFAULT 0,
                slippage_bps INTEGER NOT NULL DEFAULT 500,
                priority_fee INTEGER,
                min_notional_sol REAL NOT NULL DEFAULT 0.01,
                require_confirmation INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            "INSERT INTO copy_targets (wallet, alias) VALUES ('oldWallet', 'old')"
        )
        conn.commit()
        conn.close()

        migrated = CopyTargetStore(legacy)
        check(
            "pre-migration DB column is ALTERed in",
            "percent_of_leader" in migrated.get_active_wallets()[0].__dict__,
        )
        check(
            "migrated DB defaults percent_of_leader to 0",
            migrated.get_active_wallets()[0].percent_of_leader == 0.0,
        )

        migrated.upsert(
            CopyTarget(wallet="pctWallet", percent_of_leader=50.0)
        )
        pct_row = migrated.get_by_wallet("pctWallet")
        check(
            "percent_of_leader round-trips",
            pct_row is not None and pct_row.percent_of_leader == 50.0,
        )

    # Mirror sizing: fixed vs percent modes with the position cap.
    fixed = CopyTarget(wallet="w", fixed_buy_sol=0.02)
    check(
        "fixed mode uses fixed_buy_sol",
        _mirror_size(1.0, fixed) == 0.02,
    )
    pct = CopyTarget(wallet="w", percent_of_leader=10.0)
    check(
        "percent mode mirrors fraction of leader notional",
        abs(_mirror_size(1.5, pct) - 0.15) < 1e-9,
    )
    capped = CopyTarget(wallet="w", percent_of_leader=10.0, max_position_sol=0.05)
    check(
        "percent mode clamps to max_position_sol",
        abs(_mirror_size(1.5, capped) - 0.05) < 1e-9,
    )
    check(
        "no size configured mirrors nothing",
        _mirror_size(1.5, CopyTarget(wallet="w")) == 0.0,
    )

    print()
    if FAILURES:
        print(f"T3.4 FAILED: {len(FAILURES)} check(s) — {FAILURES}")
        return 1
    print("T3.4 PASSED: copy-target store filters + percent sizing verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())