"""R4-P4-3 / R4-P1-5 / R4-P4-1 regression: startup latency self-test,
entry-hour research logging, rejection-stream coverage metric.

- R4-P4-3: `node.startup_latency_test` (BOOL_FIELDS + YAML default off);
  bot_runner probes getSlot x3 at boot and warns > 200 ms.
- R4-P1-5 (corrected): entry-hour is LOGGED on shadow fills
  (entry_hour_utc), never gated — the paper's hour finding is
  non-confirmatory (p=0.56).
- R4-P4-1: compute_coverage() yields Jaccard/Dice over the shadow ledger's
  detect rows (seen vs filter-rejected), the blind-spot check motivated by
  the 0.8% cross-pipeline overlap result.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import yaml  # noqa: E402

import config_loader  # noqa: E402
from research.net_metrics import compute_coverage  # noqa: E402
from research.shadow_capture import ShadowDataCapture  # noqa: E402


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R4-P4-3 / R4-P1-5 / R4-P4-1: latency test + hour log + coverage")

    # 1. Latency self-test wiring.
    check(
        "BOOL_FIELDS has node.startup_latency_test",
        "node.startup_latency_test" in config_loader.BOOL_FIELDS,
    )
    src = Path("src/bot_runner.py").read_text()
    check(
        "bot_runner probes get_slot x3",
        "for _ in range(3):" in src
        and "await probe.get_slot()" in src,
    )
    check(
        "warns above 200 ms",
        "> 200 ms" in src,
    )
    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML default off",
        cfg["node"].get("startup_latency_test") is False,
        f"-> {cfg['node'].get('startup_latency_test')!r}",
    )

    # 2. Hour logging (no gate) — _race_kpi_fields carries entry_hour_utc.
    from trading.universal_trader import UniversalTrader

    t = object.__new__(UniversalTrader)
    t.stagnation_exit_min = 0  # R4-P1-9 mock default (off)
    t.stagnation_band_pct = 0.02  # R4-P1-9 mock default
    t.max_wash_share = 0.0  # R4-P1-6 mock default
    from screening.wash_lpi import WashLpiDetector as _WLD  # R4-P1-6 mock default
    t._wash_lpi = _WLD()  # R4-P1-6 mock default
    t.max_gini_coefficient = 0.0  # R4-P1-10 mock default
    t.shadow_mode = True

    class _Slot:
        def __init__(self, v):
            self.v = v

        async def __call__(self):
            return self.v

    t.solana_client = type("C", (), {"get_slot": _Slot(100)})()  # type: ignore[assignment]
    import asyncio

    fields = asyncio.run(t._race_kpi_fields(None))  # type: ignore[arg-type]
    check(
        "entry_hour_utc logged on fills",
        isinstance(fields.get("entry_hour_utc"), int)
        and 0 <= fields["entry_hour_utc"] <= 23,
        f"-> {fields.get('entry_hour_utc')!r}",
    )
    check(
        "no hour GATE key in YAML filters (research-only per R4-CORR-1)",
        "blocked_entry_hours_utc" not in cfg.get("filters", {}),
    )

    # 3. Coverage metric over a synthetic ledger.
    with tempfile.TemporaryDirectory() as tmp:
        cap = ShadowDataCapture(dir_path=tmp, enabled=True)
        # 4 detected; 2 of them rejected by filters; 1 traded.
        for mint, skip in [
            ("A", None),
            ("B", "fail_closed"),
            ("C", "max_cap"),
            ("D", None),
        ]:
            cap.detect(
                mint=mint,
                symbol="S",
                platform="pump_fun",
                quote_mint="sol",
                creator="creator",
                transfer_fee_bps=0,
                listener="geyser",
                skip_reason=skip,
            )
        cov = compute_coverage(ledgers=[next(Path(tmp).glob("*.jsonl"))])
        check("4 detected", cov["detected"] == 4, f"-> {cov['detected']}")
        check("2 rejected", cov["rejected"] == 2, f"-> {cov['rejected']}")
        check("2 overlap", cov["overlap"] == 2, f"-> {cov['overlap']}")
        check(
            "jaccard 0.5",
            cov["jaccard"] is not None and abs(cov["jaccard"] - 0.5) < 1e-9,
            f"-> {cov['jaccard']!r}",
        )
        empty = compute_coverage(ledgers=[])
        check(
            "empty ledger -> none, no divide-by-zero",
            empty["jaccard"] is None and empty["detected"] == 0,
            f"-> {empty['jaccard']!r}",
        )

    if failures:
        print(f"\nBATCH-B FAILED ({len(failures)}): {failures}")
        return 1
    print("\nLatency test + hour log + coverage metric OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())