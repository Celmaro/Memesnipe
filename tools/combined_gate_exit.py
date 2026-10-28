"""Loop-3 combined validation on held-out H14: mc<35 gate x best exits.

Entry gate and exit knobs were tuned independently (gate sweep + param
sweep). This verifies the COMBINATION (mc<35 buys x staged trail6/sl25 and
pure-trail cells) still beats the old config on the never-tuned hour 14.
"""
from __future__ import annotations

import collections
import json
import statistics
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/home/agentuser/Memesnipe")
sys.path.insert(0, "/home/agentuser/Memesnipe/src")

from research.cost_model import net_round_trip  # noqa: E402

ARCHIVE = "/home/agentuser/Memesnipe/state/replay_cache/2026-10-26-14.jsonl.zst"
POS_SOL = 0.01

SETS = {
    "OLD_st_trail8_sl20": ("staged", (0.40, 0.20, 0.10, 0.08, 0.20)),
    "NEW_st_trail6_sl25": ("staged", (0.40, 0.20, 0.10, 0.06, 0.25)),
    "t10_15_6": ("trail", (0.15, 0.06, 0.20)),
    "t10_15_8": ("trail", (0.15, 0.08, 0.20)),
}

MC_GATES = {"no_gate": None, "mc<40": 40.0, "mc<35": 35.0, "mc<30": 30.0}


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
    sh, tpl, arm, trail, hs = cfg
    p.peak = max(p.peak, price)
    pnl = price / p.ep - 1
    ppnl = p.peak / p.ep - 1
    if pnl <= -hs:
        return p.realized + p.share * pnl
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


proc = subprocess.Popen(["zstdcat", ARCHIVE], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
tokens = {}
res = collections.defaultdict(lambda: collections.defaultdict(list))
pos = {}
done = {}
mc_now = {}
for raw in proc.stdout:
    try:
        ev = json.loads(raw)
    except Exception:
        continue
    a = ev.get("action")
    mint = ev.get("mint")
    if a == "create":
        if mint:
            tokens[mint] = [ev.get("price") or 0, ev.get("marketCapQuote") or 0, False]
            mc_now[mint] = ev.get("marketCapQuote") or 0
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
        mc_now[mint] = mc
        if a == "buy" and not t[2] and t[1] >= 0.25:
            t[2] = True  # curve progress baseline
        for gname, gmc in MC_GATES.items():
            for name, (kind, cfg) in SETS.items():
                key = (gname, name, mint)
                pobj = pos.get(key)
                if pobj is None:
                    if a == "buy" and t[2] and not done.get(key):
                        if gmc is None or mc <= gmc:
                            pos[key] = P(price)
                            done[key] = True
                    continue
                r = tick(pobj, price, kind, cfg)
                if r is not None:
                    res[gname][name].append(r * 100)
                    del pos[key]
    elif a in ("migrate", "createPool"):
        for key in [k for k in pos if k[2] == mint]:
            gname, name, mint2 = key
            pobj = pos.pop(key)
            last = tokens[mint][0] if tokens.get(mint) else pobj.ep
            r = tick(pobj, last, SETS[name][0], SETS[name][1])
            res[gname][name].append((r or 0) * 100)
proc.wait()

print(f"{'gate':<8}{'exit':<22}{'n':>6}{'WR':>6}{'med%':>8}{'avg%':>8}{'tot%':>10}")
print("-" * 66)
for gname in MC_GATES:
    for name in SETS:
        v = res[gname][name]
        if not v:
            continue
        n = len(v)
        net = [net_round_trip(x / 100.0, POS_SOL) * 100 for x in v]
        wr = sum(1 for x in net if x > 0) / n * 100
        print(f"{gname:<8}{name:<22}{n:>6}{wr:>5.0f}%{statistics.median(net):>+7.1f}"
              f"{statistics.mean(net):>+7.2f}{sum(net):>+9.0f}")