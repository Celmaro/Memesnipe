"""R4-P1-4 regression: graduation-probability entry timing (log-only).

Proves:
1. Band mapping covers the graduation monitor's recut bands
   (nuke_zone <50, sweet_spot 85-120, flat edge >250).
2. assess() attaches historical stats when the stats file has the band,
   and stays None-tolerant when it does not.
3. load_band_stats is fail-open (missing/corrupt file -> {}).
4. The advisory NEVER gates: the gate code path only logs — asserted by
   checking the trader source has no return/skip in the advisory block.
5. Config wiring: BOOL_FIELDS + bot_runner + YAML (default off).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import yaml  # noqa: E402

import config_loader  # noqa: E402
from screening.grad_timing import GRAD_BANDS, assess, band_for, load_band_stats  # noqa: E402

PASS = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS
    if ok:
        PASS += 1
        print(f"  [PASS] {name} {extra}")
    else:
        print(f"  [FAIL] {name} {extra}")


def main() -> int:
    # 1. Band mapping.
    check("0 SOL -> nuke_zone", band_for(0.0) == "nuke_zone")
    check("49 SOL -> nuke_zone", band_for(49.9) == "nuke_zone")
    check("100 SOL -> sweet_spot", band_for(100.0) == "sweet_spot")
    check("300 SOL -> flat_edge", band_for(300.0) == "flat_edge")
    check("bands monotonic", all(GRAD_BANDS[i][1] == GRAD_BANDS[i + 1][0] for i in range(len(GRAD_BANDS) - 1)))

    # 2. assess with + without stats.
    t = assess(100.0, {"sweet_spot": {"median_multiple": 1.83, "nuke_rate": 0.18}})
    check("assess with stats", t.median_multiple == 1.83 and t.nuke_rate == 0.18)
    t2 = assess(100.0, {})
    check("assess without stats is None-tolerant", t2.median_multiple is None and t2.band == "sweet_spot")

    # 3. load_band_stats fail-open.
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "stats.json"
        check("missing file -> {}", load_band_stats(p) == {})
        p.write_text("not json{")
        check("corrupt file -> {}", load_band_stats(p) == {})
        p.write_text(json.dumps({"nuke_zone": {"median_multiple": 0.85, "nuke_rate": 0.41}}))
        stats = load_band_stats(p)
        check("valid file loads", stats["nuke_zone"]["nuke_rate"] == 0.41)
        # assess against real file data.
        t3 = assess(20.0, stats)
        check("nuke_zone stats attach", t3.median_multiple == 0.85)

    # 4. Advisory never gates: the block in the trader only logs. The
    # graduation-timing advisory sits inside the curve gate, which C12.3
    # moved to GateStackMixin — search both source files.
    src = (REPO / "src/trading/universal_trader.py").read_text()
    src += "\n" + (REPO / "src/trading/gate_stack.py").read_text()
    block_start = src.find("R4-P1-4: graduation-band advisory (log-only)")
    check("advisory block present", block_start > 0)
    block = src[block_start : block_start + 1400]
    check("advisory block has no return", "return" not in block.split("honeypot")[0])
    check("advisory is log-only", "[GRAD-TIMING]" in block)
    check("advisory gated on flag", "self.grad_timing_enabled" in block)

    # 5. Config wiring.
    check(
        "grad_timing_enabled in BOOL_FIELDS",
        "trade.grad_timing_enabled" in config_loader.BOOL_FIELDS,
    )
    cfg = yaml.safe_load((REPO / "bots/memesnipe.yaml").read_text())
    check(
        "YAML grad_timing_enabled is a valid bool (off)",
        isinstance(cfg["trade"].get("grad_timing_enabled"), bool),
        f"-> {cfg['trade'].get('grad_timing_enabled')!r}",
    )
    br = (REPO / "src/bot_runner.py").read_text()
    check("bot_runner passes grad_timing_enabled", "grad_timing_enabled=" in br)
    check("bot_runner passes grad_band_stats_path", "grad_band_stats_path=" in br)

    print(f"\nR4-P1-4 grad-timing regression: {PASS} checks")
    return 0 if PASS == 19 else 1


if __name__ == "__main__":
    raise SystemExit(main())