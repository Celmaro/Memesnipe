"""One-pass param sweep: exits × gates on replay archives, NET of fees.

Reads each hourly archive ONCE and evaluates every (exit config) on each
token's price path with REALISTIC fills (exit at the current observed tick
price, exactly like live — the optimistic same-tick fill that flattered
trails in the official backtester is NOT used). Every cell reports net
expectancy via research.cost_model (R5-P1-3), plus profit factor and
max drawdown.

Usage:
    uv run python -u /tmp/param_sweep.py [hours...]   # default 09..14
"""
from __future__ import annotations

import collections
import json
import statistics
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path("/home/agentuser/Memesnipe")
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from research.cost_model import net_round_trip  # noqa: E402
from tools.replay_backtester import ENTRY  # noqa: E402

BASE = "/home/agentuser/Memesnipe/state/replay_cache/2026-10-26-{}.jsonl.zst"
HOURS = sys.argv[1:] or ["09", "10", "11", "12", "13", "14"]
POSITION_SOL = ENTRY.get("position_sol", 0.01)

# (name, kind, cfg)
#   staged: (tp_share, tp_lvl, arm, trail, hard_stop)
#   trail : (arm, trail, hard_stop)
#   fixed : (tp, sl)
SETS = {}
def _add(name, kind, cfg):
    SETS[name] = (kind, cfg)

def _pct(x):
    return int(round(x * 100))

# ── staged_trail: bank share at +tp, then BE floor + trail ──
for share in (0.30, 0.40, 0.50):
    for tpl in (0.15, 0.20, 0.25, 0.30):
        _add(f"st_{_pct(share)}@{_pct(tpl)}p_arm10_trail8_sl20",
             "staged", (share, tpl, 0.10, 0.08, 0.20))
# trail width / stop tighten around the live winner (40%@20, arm10)
for trail in (0.06, 0.08, 0.10, 0.12):
    for sl in (0.15, 0.20, 0.25):
        _add(f"st_40@20p_arm10_trail{_pct(trail)}_sl{_pct(sl)}",
             "staged", (0.40, 0.20, 0.10, trail, sl))
for arm in (0.08, 0.10, 0.15):
    _add(f"st_40@20p_arm{_pct(arm)}_trail8_sl20", "staged", (0.40, 0.20, arm, 0.08, 0.20))
# ── pure trail10-style ──
for arm in (0.08, 0.10, 0.15):
    for trail in (0.06, 0.08, 0.10):
        _add(f"t10_{_pct(arm)}_{_pct(trail)}_sl20", "trail", (arm, trail, 0.20))
# ── fixed tp/sl ──
for tp in (0.15, 0.20, 0.25, 0.30):
    for sl in (0.10, 0.15, 0.20, 0.25):
        _add(f"tp{_pct(tp)}_sl{_pct(sl)}", "fixed", (tp, sl))
# ── current live (marker) ──
_add("LIVE_st_40@20p_arm10_trail8_sl20", "staged", (0.40, 0.20, 0.10, 0.08, 0.20))


class P:
    __slots__ = ("ep", "peak", "armed", "floor", "tpf", "share", "realized")
    def __init__(self, ep):
        self.ep = ep; self.peak = ep; self.armed = False
        self.floor = ep * 0.8; self.tpf = False; self.share = 1.0; self.realized = 0.0


def tick(p, price, kind, cfg):
    if kind == "trail":
        arm, trail, hs = cfg
        pnl = price / p.ep - 1
        if pnl <= -hs:
            return p.share * pnl
        if price > p.peak:
            p.peak = price
            if not p.armed and p.peak / p.ep - 1 >= arm:
                p.armed = True
                p.floor = max(p.floor, p.ep)
            nf = p.peak * (1 - trail)
            if p.armed and nf > p.floor:
                p.floor = nf
        if p.armed and price <= p.floor:
            return p.share * pnl
        return None
    if kind == "staged":
        sh, tpl, arm, trail, hs = cfg
        p.peak = max(p.peak, price)
        pnl = price / p.ep - 1
        ppnl = p.peak / p.ep - 1
        if pnl <= -hs:
            r = p.share * pnl
            return r
        if not p.tpf and pnl >= tpl:
            p.tpf = True
            p.realized += sh * pnl
            p.share -= sh
        if p.tpf:
            if p.floor < p.ep and ppnl >= arm:
                p.floor = p.ep * 1.0
            nf = p.peak * (1 - trail)
            if nf > p.floor:
                p.floor = nf
        if p.tpf and price <= p.floor:
            return p.realized + p.share * pnl
        return None
    tp, sl = cfg
    pnl = price / p.ep - 1
    if pnl >= tp:
        return pnl
    if pnl <= sl:
        return pnl
    return None


def run_hour(hour):
    p = subprocess.Popen(["zstdcat", BASE.format(hour)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    tokens = {}
    res = collections.defaultdict(list)
    pos = {}
    done = {}
    for raw in p.stdout:
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        a = ev.get("action")
        mint = ev.get("mint")
        if a == "create":
            if mint:
                tokens[mint] = [ev.get("price") or 0, ev.get("marketCapQuote") or 0, False]
        elif a in ("buy", "sell"):
            t = tokens.get(mint)
            if not t:
                continue
            price = ev.get("price")
            if not price:
                continue
            t[0] = price
            t[1] = ev.get("marketCapQuote") or t[1]
            if a == "buy" and not t[2] and t[1] >= ENTRY["min_curve_progress_sol"]:
                t[2] = True
            for name, (kind, cfg) in SETS.items():
                key = (name, mint)
                pobj = pos.get(key)
                if pobj is None:
                    if a == "buy" and t[2] and not done.get(key):
                        pos[key] = P(price)
                        done[key] = True
                    continue
                r = tick(pobj, price, kind, cfg)
                if r is not None:
                    res[name].append(r * 100)  # pnl fraction -> percent
                    del pos[key]
        elif a in ("migrate", "createPool"):
            for key in [k for k in pos if k[1] == mint]:
                pobj = pos.pop(key)
                last = tokens[mint][0] if tokens.get(mint) else pobj.ep
                r = tick(pobj, last, SETS[key[0]][0], SETS[key[0]][1])
                res[key[0]].append((r or 0) * 100)
    p.wait()
    return res


total = collections.defaultdict(list)
JSON_DIR = Path("/tmp/param_sweep_hours")
JSON_DIR.mkdir(exist_ok=True)
for h in HOURS:
    r = run_hour(h)
    with open(JSON_DIR / f"{h}.json", "w") as f:
        json.dump({k: v for k, v in r.items()}, f)
    for k, v in r.items():
        total[k].extend(v)
    print(f"hour {h} done -> {JSON_DIR / f'{h}.json'}", file=sys.stderr)

# net-of-fee per trade (percent pnl -> fraction -> net)
rows = []
for k, v in total.items():
    if not v:
        continue
    n = len(v)
    gross_pnl = [g / 100.0 for g in v]  # percent -> pnl fraction
    net_pct = [net_round_trip(g, POSITION_SOL) * 100 for g in gross_pnl]
    wins = [x for x in net_pct if x > 0]
    losses = [x for x in net_pct if x <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    equity = 0.0
    peak = 0.0
    dd = 0.0
    for x in net_pct:
        equity += x
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    rows.append((statistics.mean(net_pct), k, n,
                 len(wins) / n * 100, statistics.median(net_pct), sum(net_pct),
                 pf if pf != float("inf") else None, dd))

print(f"\n{'set':<40}{'n':>6}{'WR':>6}{'med%':>8}{'avg%':>8}{'tot%':>10}{'PF':>7}{'maxDD%':>8}")
print("-" * 92)
for avg, k, n, wr, med, tot, pf, dd in sorted(rows, reverse=True):
    print(f"{k:<40}{n:>6}{wr:>5.0f}%{med:>+7.1f}{avg:>+7.2f}{tot:>+9.0f}"
          f"{(str(round(pf,1)) if pf is not None else 'inf'):>7}{dd:>8.1f}")