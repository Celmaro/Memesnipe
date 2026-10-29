"""C7.2: unsold-registry serialize/rehydrate round-trip must be lossless.

The durable unsold registry (schema 1) is the restart safety net for tokens
whose exit could not be completed in the monitor loop. Any field whose value
is lost or altered in the serialize -> rehydrate round trip strands a
position with wrong exit prices or a wrong quantity, and the stuck-token
sweeper is a last-ditch backstop, not a substitute for a correct registry.
This verifier pins the round trip across every field the current row schema
writes, so the schema-driven refactor cannot silently drop a field.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from interfaces.core import Platform, TokenInfo  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from trading.position import Position  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

MINT = Pubkey.from_bytes(b"mint" + bytes(28))


def _pk(tag: bytes) -> Pubkey:
    """A deterministic valid 32-byte pubkey tagged for readability."""
    return Pubkey.from_bytes(tag.ljust(32, b"\0"))


class _DummyWatcher:
    """`done()` False so rehydrate sees a live watcher and never spawns one."""

    def done(self) -> bool:
        return False


def _harness() -> SimpleNamespace:
    return SimpleNamespace(
        _unsold_positions={},
        _unsold_persist_path=None,
        _unsold_watcher_task=_DummyWatcher(),
    )


def _full_token() -> TokenInfo:
    """A TokenInfo with every serialized field set, incl. LetsBonk fields."""
    return TokenInfo(
        name="FullTest",
        symbol="FULL",
        uri="https://example.test/meta.json",
        mint=MINT,
        platform=Platform.LETS_BONK,
        bonding_curve=_pk(b"bond"),
        associated_bonding_curve=_pk(b"assoc-curve"),
        pool_state=_pk(b"pool-state-000"),
        base_vault=_pk(b"base-vault-000"),
        quote_vault=_pk(b"quote-vault-00"),
        global_config=_pk(b"global-config-0"),
        platform_config=_pk(b"platform-cfg-0"),
        user=_pk(b"user-key-00000"),
        creator=_pk(b"creator-key-000"),
        creator_vault=_pk(b"creator-vault-0"),
        token_program_id=_pk(b"token-program-0"),
        is_mayhem_mode=True,
        create_slot=987654321,
        is_cashback_coin=True,
        is_holder_reward=True,
        creator_fee_bps=500,
        quote_mint=_pk(b"wsol"),
        quote_token_program_id=_pk(b"quote-tk-prog-0"),
    )


def _full_position() -> Position:
    """A Position with every serialized field set (C4.8 blended PnL too)."""
    return Position(
        mint=MINT,
        symbol="FULL",
        entry_price=0.000123456789,
        quantity=98765.4321,
        entry_time=datetime(2026, 10, 29, 8, 17, 42, 123456),
        take_profit_price=0.0002,
        stop_loss_price=0.00009,
        max_hold_time=600,
        position_id="abc123def4567890",
        realized_pnl_quote=42.5,
    )


def _roundtrip(rows: list[dict]) -> dict[str, tuple[TokenInfo, Position]]:
    """Run the real `_rehydrate_unsold` over rows via a temp file."""
    h = _harness()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"schema": 1, "positions": rows}, f)
        h._unsold_persist_path = f.name  # noqa: SLF001
    UniversalTrader._rehydrate_unsold(h)
    Path(f.name).unlink(missing_ok=True)
    return h._unsold_positions  # noqa: SLF001


def main() -> None:
    fails: list[str] = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            fails.append(label)

    info, pos = _full_token(), _full_position()
    h = _harness()
    h._unsold_positions = {str(MINT): (info, pos)}  # noqa: SLF001
    rows = UniversalTrader._serialize_unsold(h)
    check("serialize emits exactly one row", len(rows) == 1, f"n={len(rows)}")
    row = rows[0]

    for field in ("mint", "symbol", "platform", "entry_time", "position_id",
                  "realized_pnl_quote", "entry_price", "quantity"):
        check(f"row carries {field}", field in row, f"-> {row.get(field)!r}")

    recovered = _roundtrip(rows)
    check("rehydrate recovers one position", len(recovered) == 1, f"n={len(recovered)}")
    if len(recovered) != 1:
        print("FAIL: no position recovered; aborting field checks")
        raise SystemExit(1)

    (rinfo, rpos) = recovered[str(MINT)]

    tok_fields = [
        "symbol", "platform", "bonding_curve", "associated_bonding_curve",
        "pool_state", "base_vault", "quote_vault", "global_config",
        "platform_config", "user", "creator", "creator_vault",
        "token_program_id", "quote_mint", "quote_token_program_id",
        "is_mayhem_mode", "is_cashback_coin", "is_holder_reward",
        "creator_fee_bps", "create_slot",
    ]
    for f in tok_fields:
        expected, got = getattr(info, f), getattr(rinfo, f)
        check(f"TokenInfo.{f} survives", expected == got, f"{expected!r} -> {got!r}")

    pos_fields = [
        "entry_price", "quantity", "take_profit_price", "stop_loss_price",
        "max_hold_time", "position_id", "realized_pnl_quote",
    ]
    for f in pos_fields:
        expected, got = getattr(pos, f), getattr(rpos, f)
        check(f"Position.{f} survives", expected == got, f"{expected!r} -> {got!r}")

    check(
        "entry_time survives (iso round trip)",
        rpos.entry_time == pos.entry_time,
        f"{pos.entry_time.isoformat()} -> {rpos.entry_time.isoformat()}",
    )

    # A row without the C4.8 field must default realized_pnl_quote to 0.0.
    legacy_row = dict(row)
    legacy_row.pop("realized_pnl_quote")
    legacy = _roundtrip([legacy_row])
    (_, lpos) = legacy[str(MINT)]
    check("legacy row without realized_pnl_quote defaults 0.0",
          lpos.realized_pnl_quote == 0.0, f"-> {lpos.realized_pnl_quote}")

    print("\n" + ("C7.2 unsold round-trip verifier: PASS" if not fails else f"FAIL: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())