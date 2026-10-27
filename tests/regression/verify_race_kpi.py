"""R4-P0-1/2/4 regression: race KPIs + per-path CU budget.

Three execution-side items from the code-ground-truth audit:

- R4-P0-4 landed-slot KPI: TokenInfo carries ``create_slot`` (set by the
  geyser listener from the create frame); the trader's ``_race_kpi_fields``
  computes ``landed_slot_offset`` (decision slot - create slot) into the
  shadow ledger's sim_open row; net_metrics surfaces the median.
- R4-P0-1 reordering slippage: sim_close rows carry ``reordering_bps`` (0
  for shadow fills at the observed price — the field is the live-fill hook);
  net_metrics surfaces the average.
- R4-P0-2 per-path CU budget: buy and sell get their own PriorityFeeManager
  (YAML keys uncommented; bot_runner passes them; config_loader validates).
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import yaml  # noqa: E402

import config_loader  # noqa: E402
from interfaces.core import TokenInfo  # noqa: E402
from research.net_metrics import compute_metrics  # noqa: E402
from research.shadow_capture import ShadowDataCapture  # noqa: E402


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R4-P0-1/2/4: race KPIs + per-path CU budget")

    # 1. TokenInfo.create_slot exists and defaults None (fallback listeners).
    ti = TokenInfo(name="T", symbol="T", uri="", mint=None, platform="pump_fun")  # type: ignore[arg-type]
    check(
        "TokenInfo.create_slot field",
        hasattr(ti, "create_slot") and ti.create_slot is None,
        f"-> {ti.create_slot!r}",
    )

    # 2. Source wiring: geyser listener stamps create_slot on the token.
    src = Path("src/monitoring/universal_geyser_listener.py").read_text()
    check(
        "listener stamps token_info.create_slot",
        "token_info.create_slot = update_slot" in src,
    )

    # 3. _race_kpi_fields returns the full KPI set.
    from trading.universal_trader import UniversalTrader

    t = object.__new__(UniversalTrader)
    t.shadow_mode = True
    t.solana_client = type(
        "C", (), {"get_slot": lambda self: _AsyncResult(1234)}
    )()  # type: ignore[assignment]
    ti2 = TokenInfo(  # type: ignore[arg-type]
        name="T",
        symbol="T",
        uri="",
        mint=None,
        platform="pump_fun",
        create_slot=1230,
    )

    class _AsyncResult:
        def __init__(self, v):
            self.v = v

        async def __call__(self):
            return self.v

    t.solana_client = type("C", (), {"get_slot": _AsyncResult(1234)})()  # type: ignore[assignment]
    import asyncio

    fields = asyncio.run(t._race_kpi_fields(ti2))
    check(
        "landed_slot_offset = decision - create",
        fields.get("landed_slot_offset") == 4,
        f"-> {fields.get('landed_slot_offset')!r}",
    )
    check(
        "create_slot carried through",
        fields.get("create_slot") == 1230,
        f"-> {fields.get('create_slot')!r}",
    )
    check(
        "reordering_bps present (0 for shadow)",
        fields.get("reordering_bps") == 0,
        f"-> {fields.get('reordering_bps')!r}",
    )

    # 4. Shadow ledger row carries the KPIs end-to-end.
    with tempfile.TemporaryDirectory() as tmp:
        cap = ShadowDataCapture(dir_path=tmp, enabled=True)
        cap.sim_open(
            mint="M",
            symbol="T",
            platform="pump_fun",
            price=1.0,
            amount_tokens=1.0,
            quote_spent=1.0,
            create_slot=1230,
            decision_slot=1234,
            landed_slot_offset=4,
            entry_tx_idx_in_block=None,
            reordering_bps=0,
        )
        cap.sim_close(
            mint="M",
            symbol="T",
            platform="pump_fun",
            price=1.2,
            amount_tokens=1.0,
            quote_received=1.2,
            exit_reason="take_profit",
            pnl_quote=0.2,
            pnl_pct=0.2,
            hold_seconds=60.0,
            reordering_bps=0,
        )
        metrics = compute_metrics(ledgers=[next(Path(tmp).glob("*.jsonl"))])
        check(
            "net_metrics median landed-slot offset",
            metrics.median_landed_slot_offset == 4,
            f"-> {metrics.median_landed_slot_offset!r}",
        )
        check(
            "net_metrics avg reordering bps",
            metrics.avg_reordering_bps == 0.0,
            f"-> {metrics.avg_reordering_bps!r}",
        )

    # 5. Per-path CU budget: two fee managers with the split values.
    import inspect

    from trading.universal_trader import UniversalTrader as UT

    sig = inspect.signature(UT.__init__)
    check(
        "trader ctor has buy_priority_fee",
        "buy_priority_fee" in sig.parameters,
    )
    check(
        "trader ctor has sell_priority_fee",
        "sell_priority_fee" in sig.parameters,
    )
    runner_src = Path("src/bot_runner.py").read_text()
    check(
        "bot_runner passes buy_priority_fee",
        "buy_priority_fee=cfg" in runner_src,
    )
    check(
        "bot_runner passes sell_priority_fee",
        "sell_priority_fee=cfg" in runner_src,
    )
    check(
        "config_loader validates buy_fixed_amount",
        "priority_fees.buy_fixed_amount" in config_loader.CONFIG_VALIDATION_RULES[0]
        or any(
            r[0] == "priority_fees.buy_fixed_amount"
            for r in config_loader.CONFIG_VALIDATION_RULES
        ),
    )

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    pf = cfg["priority_fees"]
    check(
        "YAML buy_fixed_amount live",
        pf.get("buy_fixed_amount") == 150_000,
        f"-> {pf.get('buy_fixed_amount')!r}",
    )
    check(
        "YAML sell_fixed_amount live",
        pf.get("sell_fixed_amount") == 50_000,
        f"-> {pf.get('sell_fixed_amount')!r}",
    )

    if failures:
        print(f"\nR4-P0-1/2/4 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nRace KPIs + per-path CU OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())