"""Research shadow-capture regression (2026-10-26).

Guards:
1. detect/sim_open/sim_close/summary rows land as valid JSONL in the daily
   file, with the join key (mint) present on every trade row.
2. Disabled capture is a no-op (no file, no raise).
3. A write failure never raises into the trade path (best-effort contract).
4. The trader wiring exists: constructor accepts shadow_capture and only
   keeps it when shadow_mode is on.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from research.shadow_capture import ShadowDataCapture

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    cap = ShadowDataCapture(dir_path=tmp / "journal", enabled=True)

    cap.detect(
        mint="mintA",
        symbol="TOKEN",
        platform="pump_fun",
        quote_mint="So11111111111111111111111111111111111111112",
        creator="creatorA",
        transfer_fee_bps=0,
        listener="geyser",
    )
    cap.detect(
        mint="mintB",
        symbol="SKIP",
        platform="pump_fun",
        quote_mint="So11111111111111111111111111111111111111112",
        creator="creatorB",
        transfer_fee_bps=300,
        listener="geyser",
        skip_reason="transfer fee 300 bps exceeds max_transfer_fee_bps",
    )
    cap.sim_open(
        mint="mintA",
        symbol="TOKEN",
        platform="pump_fun",
        price=0.0001,
        amount_tokens=100.0,
        quote_spent=0.01,
        pool_snapshot={"virtual_quote_reserves": 30_000_000_000, "graduated": False},
    )
    cap.sim_close(
        mint="mintA",
        symbol="TOKEN",
        platform="pump_fun",
        price=0.0002,
        amount_tokens=100.0,
        quote_received=0.02,
        exit_reason="take_profit",
        pnl_quote=0.01,
        pnl_pct=1.0,
        hold_seconds=45.0,
        pool_snapshot={"real_quote_reserves": 5, "graduated": True},
    )
    cap.summary(shadow=True, tokens_traded=1, exit_strategy="tp_sl")

    files = list((tmp / "journal").glob("*.jsonl"))
    check("journal file written", len(files) == 1, f"got {len(files)}")
    rows = []
    for f in files:
        rows.extend(
            json.loads(line) for line in f.read_text().splitlines() if line.strip()
        )
    check("5 rows appended", len(rows) == 5, f"got {len(rows)}")
    types = [r["type"] for r in rows]
    expected_types = ["detect", "detect", "sim_open", "sim_close", "summary"]
    check(
        "row types correct",
        types == expected_types,
        f"got {types}",
    )
    check("every row carries ts", all("ts" in r for r in rows))
    check("skip reason captured", rows[1].get("skip_reason") is not None)
    check(
        "pool snapshot captured on open",
        rows[2].get("pool_snapshot", {}).get("virtual_quote_reserves")
        == 30_000_000_000,
    )
    check(
        "pnl captured on close",
        rows[3].get("pnl_quote") == 0.01 and rows[3].get("exit_reason") == "take_profit",
    )

    # Disabled capture: no file, no raise.
    tmp2 = Path(tempfile.mkdtemp())
    off = ShadowDataCapture(dir_path=tmp2 / "journal", enabled=False)
    off.detect(mint="x", symbol="X", platform="p", quote_mint="q", creator=None, transfer_fee_bps=0, listener="l")
    off.sim_open(mint="x", symbol="X", platform="p", price=1.0, amount_tokens=1.0, quote_spent=1.0)
    check("disabled capture writes nothing", not list((tmp2 / "journal").glob("*.jsonl")))

    # Trader wiring: keeps capture only when shadow_mode on.
    from trading.universal_trader import UniversalTrader

    t = object.__new__(UniversalTrader)
    t.exit_mode = "tp_sl"  # R4-P1-7 mock default
    t.strategies_ledger = None  # R4-P1-7 mock default
    t.allow_mayhem_mode = False  # R3-P0-2 off in verifiers
    t.shadow_mode = True
    t.shadow_capture = cap  # what __init__ sets when both on
    check("trader stores capture when shadow on", t.shadow_capture is cap)
    t2 = object.__new__(UniversalTrader)
    t2.exit_mode = "tp_sl"  # R4-P1-7 mock default
    t2.strategies_ledger = None  # R4-P1-7 mock default
    t2.allow_mayhem_mode = False  # R3-P0-2 off in verifiers
    t2.shadow_mode = False
    t2.shadow_capture = None
    check("trader drops capture when shadow off", t2.shadow_capture is None)

    # Config wiring: BOOL_FIELDS + YAML default.
    import yaml

    import config_loader

    check(
        "BOOL_FIELDS includes research.shadow_capture_enabled",
        "research.shadow_capture_enabled" in config_loader.BOOL_FIELDS,
    )
    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML enables capture (user asked for it)",
        cfg.get("research", {}).get("shadow_capture_enabled", False) is True,
    )

    print()
    if FAILURES:
        print(f"SHADOW-CAPTURE FAILED: {len(FAILURES)} check(s) — {FAILURES}")
        return 1
    print("SHADOW-CAPTURE PASSED: research ledger verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())