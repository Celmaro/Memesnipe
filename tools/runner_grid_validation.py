"""Loop-5: validate the pruned 4-cell grid x mc<35 gate on tuning + held-out.

The grid was pruned to tp_sl / staged_trail(live) / st_trail6_sl25 /
st_trail10_sl25. This validates each cell against the LIVE mc<35 entry gate
across tuning hours 09-13, then checks the ranking survives held-out H14.
Adds the runner-capture metric (user priority): of positions that banked the
initial +20% TP, how often did the remaining 60% leg ride to +50%/+100%
before exiting (vs trailing out flat/negative).

Usage:
    uv run python -u tools/runner_grid_validation.py
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

BASE = "/home/agentuser/Memesnipe/state/replay_cache/2026-10-26-{}.jsonl.zst"
TUNING = ["09", "10", "11", "12", "13"]
HELDOUT = ["14"]
POSITION_SOL = 0.01
MC_GATE = 35.0  # live entry gate

# (name, kind, cfg) — runner-riding grid (user directive 2026-10-29)
#   staged: (tp_share, tp_lvl, arm, trail, hard_stop)
SETS = {
    "tp_sl": ("fixed", (0.20, 0.20)),  # research baseline only (removed from live grid)
    "staged_trail(live trail12/sl25)": ("staged", (0.40, 0.20, 0.10, 0.12, 0.25)),
    "st_trail6_sl25": ("staged", (0.40, 0.20, 0.10, 0.06, 0.25)),
    "st_trail10_sl25": ("staged", (0.40, 0.20, 0.10, 0.10, 0.25)),
}


class P:
    __slots__ = ("ep", "peak", "armed", "floor", "tpf", "share", "realized",
                 "post_tp_peak_pnl")

    def __init__(self, ep):
        self.ep = ep
        self.peak = ep
        self.armed = False
        self.floor = ep * 0.8
        self.tpf = False
        self.share = 1.0
        self.realized = 0.0
        self.post_tp_peak_pnl = None  # highest pnl of the remaining leg


def tick(p, price, kind, cfg):
    """Return exit pnl (fraction) or None. Mirrors the live evaluator."""
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
            r = p.realized + p.share * pnl
            if p.tpf and p.post_tp_peak_pnl is None:
                p.post_tp_peak_pnl = pnl
            return r
        if not p.tpf and pnl >= tpl:
            p.tpf = True
            p.realized += sh * pnl
            p.share -= sh
        if p.tpf:
            # track the remaining leg's best pnl for runner-capture stats
            if p.post_tp_peak_pnl is None or pnl > p.post_tp_peak_pnl:
                p.post_tp_peak_pnl = pnl
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


def run_hour(hour: str) -> dict[str, list[dict]]:
    """Per-strategy outcome rows: {pnl_pct, tp_hit, post_tp_peak_pnl}."""
    p = subprocess.Popen(
        ["zstdcat", BASE.format(hour)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    tokens: dict[str, list] = {}
    res: dict[str, list[dict]] = collections.defaultdict(list)
    pos: dict[tuple, P] = {}
    done: dict[tuple, bool] = {}
    for raw in p.stdout:
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        a = ev.get("action")
        mint = ev.get("mint")
        if a == "create":
            if mint:
                # [price, marketCap, real_quote_sol, armed]
                tokens[mint] = [ev.get("price") or 0, ev.get("marketCapQuote") or 0, ev.get("quoteInPool") or 0.0, False]
        elif a in ("buy", "sell"):
            t = tokens.get(mint)
            if not t:
                continue
            price = ev.get("price")
            if not price:
                continue
            t[0] = price
            mc = ev.get("marketCapQuote") or t[1]
            t[1] = mc
            # C8.1 fix: the live curve gate measures curve progress by the SOL
            # actually paid into the pool (quoteInPool / real quote reserves),
            # not market cap. Mirror that: arm on quoteInPool >= 0.25 instead
            # of MC >= 0.25 — the loop-5 sweep used MC and was contaminated.
            rq = ev.get("quoteInPool")
            if rq is not None:
                t[2] = rq
            if not t[3] and t[2] >= 0.25:
                t[3] = True  # curve progress baseline (real SOL in pool)
            for name, (kind, cfg) in SETS.items():
                key = (name, mint)
                pobj = pos.get(key)
                if pobj is None:
                    if a == "buy" and t[3] and not done.get(key) and mc <= MC_GATE:
                        pos[key] = P(price)
                        done[key] = True
                    continue
                r = tick(pobj, price, kind, cfg)
                if r is not None:
                    res[name].append({
                        "pnl_pct": r * 100,
                        "tp_hit": pobj.tpf if kind == "staged" else pnl_ge_tp(price, pobj.ep, cfg),
                        "post_tp_peak_pnl": (
                            (pobj.post_tp_peak_pnl or 0) * 100 if kind == "staged" else None
                        ),
                    })
                    del pos[key]
        elif a in ("migrate", "createPool"):
            for key in [k for k in pos if k[1] == mint]:
                name, _mint = key
                pobj = pos.pop(key)
                last = tokens[mint][0] if tokens.get(mint) else pobj.ep
                r = tick(pobj, last, SETS[name][0], SETS[name][1])
                if r is not None:
                    res[name].append({
                        "pnl_pct": (r or 0) * 100,
                        "tp_hit": pobj.tpf if SETS[name][0] == "staged" else False,
                        "post_tp_peak_pnl": (
                            (pobj.post_tp_peak_pnl or 0) * 100 if SETS[name][0] == "staged" else None
                        ),
                    })
    p.wait()
    return res


def pnl_ge_tp(price, ep, cfg):
    tp, _sl = cfg
    return price / ep - 1 >= tp


def report(rows: dict[str, list[dict]], label: str) -> None:
    # Sane band (standing convention, matches show-results tables): a pnl
    # beyond ±2000% is an archive price artifact (entry near 1e-13 SOL then
    # a normal tick), not a trade the grid can bank. Exclude before stats.
    SANE = 2000.0
    print(f"\n=== {label} (mc<{MC_GATE} gate, net-of-fee, |pnl|<=2000 band) ===")
    print(f"{'exit':<32}{'n':>6}{'WR':>6}{'med%':>8}{'avg%':>8}{'tot%':>10}{'PF':>7}")
    print("-" * 78)
    for name, rs in rows.items():
        if not rs:
            continue
        rs = [x for x in rs if isinstance(x, dict) and abs(x.get("pnl_pct", 0)) <= SANE]
        if not rs:
            continue
        n = len(rs)
        net = [net_round_trip(x["pnl_pct"] / 100.0, POSITION_SOL) * 100 for x in rs]
        wr = sum(1 for x in net if x > 0) / n * 100
        wins = [x for x in net if x > 0]
        losses = [x for x in net if x <= 0]
        pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")
        print(f"{name:<32}{n:>6}{wr:>5.0f}%{statistics.median(net):>+7.1f}"
              f"{statistics.mean(net):>+7.2f}{sum(net):>+9.0f}"
              f"{(str(round(pf, 1)) if pf != float('inf') else 'inf'):>7}")
    # runner capture: of staged TP-hitters, how many rode the 60% leg to +50/+100?
    print("\nrunner capture (staged cells): of positions that banked +20% TP,")
    print("  how far did the remaining leg ride before exit? (sane band)")
    for name, rs in rows.items():
        if SETS[name][0] != "staged":
            continue
        rs = [x for x in rs if isinstance(x, dict) and abs(x.get("pnl_pct", 0)) <= 2000.0]
        # the leg-peak metric is contaminated the same way (enter 1e-13, spike,
        # crash to sane exit) — band it too so the runner stats describe
        # real runners, not price artifacts.
        hits = [
            x for x in rs
            if x["tp_hit"] and x["post_tp_peak_pnl"] is not None
            and abs(x.get("post_tp_peak_pnl", 0)) <= 2000.0
        ]
        if not hits:
            continue
        p50 = sum(1 for x in hits if x["post_tp_peak_pnl"] >= 50) / len(hits) * 100
        p100 = sum(1 for x in hits if x["post_tp_peak_pnl"] >= 100) / len(hits) * 100
        avg_peak = statistics.mean(x["post_tp_peak_pnl"] for x in hits)
        print(f"  {name:<32} TP-hits={len(hits):>4}  leg-peak>+50%: {p50:>4.0f}%  "
              f">+100%: {p100:>4.0f}%  avg-leg-peak {avg_peak:+.1f}%")


def main() -> None:
    tuning: dict[str, list[dict]] = collections.defaultdict(list)
    heldout: dict[str, list[dict]] = collections.defaultdict(list)
    for h in TUNING:
        for k, v in run_hour(h).items():
            tuning[k].extend(v)
        print(f"hour {h} done", file=sys.stderr)
    for h in HELDOUT:
        for k, v in run_hour(h).items():
            heldout[k].extend(v)
        print(f"hour {h} done", file=sys.stderr)
    report(dict(tuning), f"TUNING hours {','.join(TUNING)}")
    report(dict(heldout), f"HELD-OUT hours {','.join(HELDOUT)}")
    # ranking stability
    def rank(d):
        out = {}
        for k, v in d.items():
            v = [x for x in v if isinstance(x, dict) and abs(x.get("pnl_pct", 0)) <= 2000.0]
            if v:
                out[k] = statistics.mean(
                    net_round_trip(x["pnl_pct"] / 100.0, POSITION_SOL) * 100 for x in v
                )
        return out
    rt, rh = rank(tuning), rank(heldout)
    print("\nranking: tuning avg -> held-out avg")
    for k in sorted(rt, key=lambda kk: rt[kk], reverse=True):
        print(f"  {k:<32} {rt[k]:+7.2f}  ->  {rh.get(k, float('nan')):+7.2f}")


if __name__ == "__main__":
    main()
