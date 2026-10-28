"""Analyze per-hour param sweep JSONs: tuning table (09-13), held-out (14),
per-hour stability, and cross-check against the live ledger grid.

Usage: uv run python -u /tmp/analyze_sweep.py
"""
import collections, json, statistics, sys
from pathlib import Path

D = Path("/tmp/param_sweep_hours")
HOURS = ["09", "10", "11", "12", "13"]
HO = "14"

def tbl(hours):
    tot = collections.defaultdict(list)
    for h in hours:
        data = json.loads((D / f"{h}.json").read_text())
        for k, v in data.items():
            tot[k].extend(v)
    rows = []
    for k, v in tot.items():
        if not v: continue
        n = len(v)
        wins = sum(1 for x in v if x > 0)
        wr = wins / n * 100
        med = statistics.median(v)
        avg = statistics.mean(v)
        totp = sum(v)
        rows.append((avg, k, n, wr, med, totp))
    return rows

def show(title, rows, top=18):
    print(f"\n=== {title} ===")
    print(f"{'set':<40}{'n':>6}{'WR':>6}{'med%':>8}{'avg%':>8}{'tot%':>10}")
    for avg, k, n, wr, med, totp in sorted(rows, reverse=True)[:top]:
        print(f"{k:<40}{n:>6}{wr:>5.0f}%{med:>+7.1f}{avg:>+7.2f}{totp:>+9.0f}")

tune = tbl(HOURS)
show("TUNING aggregate hours 09-13", tune)
held = tbl([HO])
show("HELD-OUT hour 14 only", held)

# per-hour best set: how often does each set win?
print("\n=== per-hour frequency (how often each set is the hour's best avg) ===")
hourly_best = collections.Counter()
for h in ["09","10","11","12","13",HO]:
    data = json.loads((D / f"{h}.json").read_text())
    avgs = {k: statistics.mean(v) for k, v in data.items() if v}
    if avgs:
        best = max(avgs, key=avgs.get)
        hourly_best[best] += 1
        print(f"  hour {h}: best = {best} (avg {avgs[best]:+.2f}%)")
print("\nwin counts:", dict(hourly_best.most_common(8)))

# consistency: for top-10 tuning sets, show each hour's avg (robustness view)
print("\n=== top-10 tuning sets, per-hour avg ===")
top10 = [k for _, k, *_ in sorted(tune, reverse=True)[:10]]
hdrs = "  " + "".join(f"{h:>9}" for h in ["09","10","11","12","13",HO])
print(f"{'set':<40}{hdrs}")
for k in top10:
    per = []
    for h in ["09","10","11","12","13",HO]:
        data = json.loads((D / f"{h}.json").read_text())
        v = data.get(k, [])
        per.append(statistics.mean(v) if v else float("nan"))
    print(f"{k:<40}" + "".join(f"{x:>+9.2f}" for x in per))