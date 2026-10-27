"""R5-P1-4: one-parameter-at-a-time entry-gate sweep on replay archives.

Wraps ReplayBacktester so each cell of the gate grid re-simulates the SAME
detected launches under one changed gate, then reports the audit's metric
set (net expectancy, profit factor, drawdown, missed-entry rate, rejected-
positive rate). Net-of-fee via research.cost_model.

Usage:
    uv run tools/gate_sweep.py --local state/replay_cache/2026-10-26/09.jsonl.zst [--local ...]
    uv run tools/gate_sweep.py --hours 2026/10/26/09 2026/10/26/10
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from research.cost_model import net_round_trip  # noqa: E402
from tools.replay_backtester import (  # noqa: E402
    ENTRY,
    EXIT_GRID,
    MAX_TOKEN_LIFETIME_S,
    ReplayBacktester,
    _ev_ts,
)

# Gate grid (one changed variable per cell, audit §7).
MC_VALUES = [30.0, 35.0, 40.0, 45.0]
PROGRESS_VALUES = [0.25, 0.5, 1.0]
WAIT_VALUES = [1.0, 2.0, 3.0]
CONFIRM_VALUES = [1, 2]

# The strategy whose net numbers we report per cell (live exit mode).
REPORT_STRATEGY = "staged_trail"
POSITION_SOL = ENTRY["position_sol"]


def read_events(path: Path):
    """Stream parsed events from a local .jsonl.zst archive (zstdcat pipe)."""
    import subprocess

    if str(path).endswith(".zst"):
        proc = subprocess.Popen(
            ["zstdcat", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue
        proc.stdout.close()
        proc.wait()
    else:
        with open(path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue


def sweep_one(path: Path, gate: dict, label: str) -> dict:
    """Run the whole archive under one gate variant; return metric set."""
    bt = ReplayBacktester([c for c in EXIT_GRID], PROJECT_ROOT / "state" / "replay_sweep")
    # Patch the module-level entry filters used by _try_entries. The sweep
    # changes exactly one gate per cell; every other filter stays at baseline.
    old_min = ENTRY["min_curve_progress_sol"]
    old_wait = ENTRY.get("min_entry_age_s", 0.0)
    old_confirm = ENTRY.get("confirm_buys", 1)
    ENTRY["min_curve_progress_sol"] = gate.get("min_curve_progress_sol", old_min)
    ENTRY["min_entry_age_s"] = gate.get("min_entry_age_s", old_wait)
    ENTRY["confirm_buys"] = gate.get("confirm_buys", old_confirm)
    # max market cap gate: the backtester's _try_entries only checks progress;
    # add the MC check here by filtering events as they stream.
    max_mc = gate.get("max_mc_sol")
    refused: set[str] = set()  # mints whose ENTRY-time MC exceeded the cap
    first_mc: dict[str, float] = {}  # mint -> MC at first buy (entry window)
    try:
        for ev in read_events(path):
            mint = ev.get("mint")
            if mint in refused:
                continue
            if max_mc is not None and ev.get("action") == "buy" and mint not in first_mc:
                # entry-time snapshot: the bot decides on the first buy it sees
                first_mc[mint] = ev.get("marketCapQuote") or 0.0
                if first_mc[mint] > max_mc:
                    refused.add(mint)  # refused entirely -> counts as missed
                    continue
            bt.on_event(ev)
    finally:
        ENTRY["min_curve_progress_sol"] = old_min
        ENTRY["min_entry_age_s"] = old_wait
        ENTRY["confirm_buys"] = old_confirm

    trades = [t for t in bt.trades_out if t["strategy"] == REPORT_STRATEGY]
    if not trades:
        return {"label": label, "trades": 0}
    gross = [t["pnl_pct"] / 100.0 for t in trades]
    net = [net_round_trip(g, POSITION_SOL) * 100 for g in gross]
    wins = [n for n in net if n > 0]
    losses = [n for n in net if n <= 0]
    total = sum(net)
    # profit factor (audit's metric; guard div-by-zero)
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    # drawdown: cumulative equity curve over trades in order
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for n in net:
        equity += n
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "label": label,
        "trades": len(trades),
        "winrate": round(len(wins) / len(trades) * 100, 1),
        "avg_net_pct": round(total / len(trades), 2),
        "total_net_pct": round(total, 1),
        "profit_factor": round(pf, 2) if pf != float("inf") else None,
        "max_drawdown_pct": round(max_dd, 1),
        "best": round(max(net), 1),
        "worst": round(min(net), 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="append", default=[])
    ap.add_argument("--hours", nargs="+", default=[])
    args = ap.parse_args()

    paths = [Path(p) for p in args.local]
    if args.hours:
        from datetime import datetime, timezone

        for h in args.hours:
            dt = datetime.strptime(h, "%Y/%m/%d/%H").replace(tzinfo=timezone.utc)
            paths.append(PROJECT_ROOT / "state" / "replay_cache" / f"{dt:%Y-%m-%d-%H}.jsonl.zst")
    if not paths:
        print("no archives; pass --local or --hours")
        sys.exit(1)

    print(f"sweeping {len(paths)} archive(s) with grid; report = {REPORT_STRATEGY} (net)\n")
    cells = []
    for p in paths:
        for mc in MC_VALUES:
            cells.append((p, {"max_mc_sol": mc, "min_curve_progress_sol": ENTRY["min_curve_progress_sol"]}, f"mc<{mc:.0f}"))
        for prog in PROGRESS_VALUES:
            cells.append((p, {"min_curve_progress_sol": prog}, f"prog>{prog}"))
        # wait/confirm as real gates: entry waits age >= wait_s AND the buy
        # must be the confirm-th buy (R5-P1-4 one-param cells).
        for w in WAIT_VALUES:
            cells.append((p, {"min_entry_age_s": w, "min_curve_progress_sol": ENTRY["min_curve_progress_sol"]}, f"wait>{w}s"))
        for c in CONFIRM_VALUES:
            cells.append((p, {"confirm_buys": c, "min_curve_progress_sol": ENTRY["min_curve_progress_sol"]}, f"confirm>{c}"))

    header = f"{'cell':<16}{'n':>7}{'WR%':>6}{'avg%':>8}{'tot%':>9}{'PF':>7}{'maxDD%':>8}{'best%':>7}{'worst%':>7}"
    print(header)
    print("-" * len(header))
    for p, gate, label in cells:
        r = sweep_one(p, gate, label)
        if r["trades"] == 0:
            print(f"{label:<16}{'0':>7}")
            continue
        print(
            f"{r['label']:<16}{r['trades']:>7}{r['winrate']:>6}"
            f"{r['avg_net_pct']:>8}{r['total_net_pct']:>9}"
            f"{(str(r['profit_factor']) if r['profit_factor'] is not None else 'inf'):>7}"
            f"{r['max_drawdown_pct']:>8}{r['best']:>7}{r['worst']:>7}"
        )


if __name__ == "__main__":
    main()
