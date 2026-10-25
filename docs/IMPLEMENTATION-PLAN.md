# Memesnipe Implementation Plan

> Status (25 Oct 2026): Phases 1–7 implemented in `main`; code ships
> disabled-first and is verified by `tests/regression/run_all.py` (38/38).
> Remaining before live use: an armed test run at user-approved size, then the
> Phase 6 ledger to measure net expectancy.

Derived from the audit of `Celmaro/Memesnipe` (25 Oct 2026 snapshot, `d1b19ef`)
and live verification runs (PASTALANA, DICE, Doin). Priorities: P0 = live-money
safety, P1 = strategy correctness, P2 = optional edge.

Principle: a faster entry is not automatically a more profitable entry. For a
low-liquidity token, avoiding a bad trade is worth more than winning a race to
buy it. Every change below ships disabled-first and is verified offline
(`tests/regression/run_all.py`) before any live run.

---

## Phase 0 — Already true / already fixed (verify, keep, commit)

| Item | State | Action |
|---|---|---|
| All 6 bot configs `enabled: false` (multi-bot trap neutralized) | Done in working tree & GitHub | Keep; re-check before every arm (`grep -H "^enabled" bots/*.yaml`) |
| `extreme_fast_mode: false` (20-token dust bug caused 6024 Overflow on DICE) | Done | Keep off until Phase 2 sizing model lands |
| Quality gates only on non-extreme path (`_curve_gate_reason` guarded) | **Local only, NOT committed** | Commit + push |
| `auth_type: "x-token"` (PublicNode gRPC; audit's `${GEYSER_AUTH_TYPE}` = startup crash) | Local config | Commit + push |
| Correct retry keys (`wait_after_creation`, not `wait_time_after_creation`) | Local config | Commit + push |
| Regression suite 37/37 | Yes | Re-run after every phase |

## Phase 1 — P0: Fee-to-position-size sanity (must land before any live trade)

Audit P0-1 confirmed by live data: committed config (0.0001 SOL trade +
1,000,000 microlamports/CU priority) paid **0.00031 SOL in network fees per
round trip — 3× the position**, not the audit's 170% estimate. Wallet went
0.331 → 0.326 across two cycles mostly on fees.

Decisions to make (config only, defaults proposed):
1. **Position size:** use audit's starting 0.01 SOL (≈3% of a 0.33 SOL wallet,
   ~30 losing trades to drain) — NOT the first audit's 0.05 SOL (15% per trade).
   Alternative intermediate: 0.005 SOL.
2. **Priority fee:** `fixed_amount: 100_000` microlamports/CU (audit's own
   table: 0.000026 SOL network round trip ≈ 0.26% of a 0.01 SOL order).
   `hard_cap: 200_000`. Keep `enable_dynamic: false`.
3. **Compute units:** leave defaults (buy 100K / sell 60K documented; code
   default 85K) — verify the buyer passes its own limits (platform_aware
   already passes `get_buy_compute_unit_limit()`).

Acceptance: with 0.01 SOL buy + 100k fee, simulated round-trip network cost
≤ 0.3% of order (0.00003 SOL), and regression suite green.

File: `bots/memesnipe.yaml` (values), `docs/` note on the sizing rule:
"size the trade and the priority fee together; never default both to extremes".

## Phase 2 — P0/P1: Correct extreme-fast sizing model (or keep it off)

Current `extreme_fast_token_amount: 20` is a dust trap. Two options:

- **Option A (recommended): keep `extreme_fast_mode: false`**, buy by SOL
  amount through the normal curve-read path. Simple, verified live (Doin
  bought+sold cleanly). Cost: one pre-buy RPC read; ~15s wait already configured.
- **Option B: proper fast path** — compute token quantity from launch data
  (CreateEvent virtual reserves) respecting a hard max quote spend, i.e.
  "buy min(max_quote_sol / launch_price, N tokens)". This is a code change in
  `platform_aware.py` extreme-fast branch, not a config value. Do NOT ship a
  fixed token count as the only knob.

Acceptance (Option B): offline verifier proving that a 2.5-SOL-bundled launch
reverts (slippage cap) while a clean launch fills, without the 20-token dust
case.

## Phase 3 — P1: Make it a real continuous sniper

Audit P1-1 confirmed: single-token mode processes one token then exits;
`yolo_mode: true` is sequential (one hold+sell+cooldown at a time).

1. **Queue freshness bug (High, code):** `max_token_age` measures **queue
   wait time** (`token_timestamps[key] = monotonic()` at enqueue), not the
   token's on-chain launch age. Fix: derive launch age from the CreateEvent
   slot/timestamp; keep queue-wait as a separate metric. In continuous mode,
   a token waiting >1ms in the queue is currently discarded even if it is 0s
   old on-chain.
2. **Enable `yolo_mode: true`** only after the freshness fix; accept that
   trades are sequential (that is a strategy property, not a bug — document
   it). Set `wait_before_new_token: 0-2`.
3. **Persistent exit monitoring (High, code):** failed sells currently stop
   after `max_exit_sell_attempts` and log "position stays open and is no
   longer monitored" (happened live with DICE). Change: unsold positions are
   re-queued into a persistent watcher that keeps re-pricing and retrying
   until sold or a manual stop — bounded by a hard TTL, never silently
   abandoned.

Acceptance: regression verifier proving (a) stale-launch-age vs queue-wait
are distinct, (b) a never-sellable position is still monitored (logged every
N seconds) instead of dropped.

## Phase 4 — Exit strategy hardening (audit Highs)

1. **Stop-loss on time-based path:** `exit_strategy: "time_based"` has no
   active SL today (`sell_slippage` is tx tolerance, not a trigger). Either
   switch default config to `tp_sl` (take_profit 20%, stop_loss 20%,
   max_hold_time 30, price_check_interval 1 — audit's trial values) or add a
   mandatory SL to the time-based path.
2. **Interval/hold coherence:** `price_check_interval` (default 10s) must be
   << `max_hold_time`; validate at config load (`price_check_interval <=
   max_hold_time/4` or similar), else the exit can never fire on schedule.
3. **Slippage:** buy 0.10 / sell 0.15–0.20 (audit trial), not 0.30/0.30 —
   sliced entry cap + exit floor that won't revert on a dump.

## Phase 5 — Token-quality model beyond the gates (P2, optional)

Audit: current filtering (blocked creators, MC cap, curve progress) is
single-signal; research says combined signals beat any one. Candidate
additional gates, each a config knob + `_curve_gate_reason` extension:
- buyer concentration (top-N wallet share of curve) — reject > threshold
- dev/holder distribution pressure (creator selling within first N blocks)
- quote liquidity sanity (already implicitly via min_curve_progress)
- optional `shreds` listener benchmark vs `geyser` (audit suggests not
  assuming geyser wins; `tools/compare_listeners.py` exists)

## Phase 6 — Profitability measurement (enabling condition, not optional)

Audit's strongest point: nothing in the repo measures net expectancy. Before
scaling beyond 0.01 SOL:
1. **Execution ledger** (`trades/` already exists): per trade record
   launch-slot, detect-time, buy-land-time, qty, quote spent, network fees,
   protocol fees, sell proceeds, failed attempts, final inventory, SOL-denominated PnL.
2. **Net metrics:** PnL/trade, profit factor, drawdown, fill rate, median hold,
   % unsellable. Report after every session — no tuning by feel.
3. **Out-of-sample:** tune on one week, test on another. Compare geyser vs
   shreds, fast vs filtered, raw vs wallet-following.

## Phase 7 — Copy-trader (P2, large, separate PR)

Audit P1-2 confirmed: only an observation watcher exists, and it excludes
PumpSwap. Build as a separate signal+risk path reusing platform-aware
buyer/seller:
1. Leader discovery: reconstruct trades (incl. routed/post-graduation), net
   PnL after all fees, reject unsold-bag or self-funded performance.
2. Qualification: 7d + 30d positive net, ≥100 closed trades/30d, entry-drift
   gate 5–8%, per-wallet/per-token exposure caps.
3. Position ledger + sell mirroring with follower inventory; restart recovery;
   PumpSwap graduation tracking (the cookbook watcher misses it).
4. Ship disabled; shadow-mode first.

---

## Verification protocol for every phase
1. `uv run tests/regression/run_all.py` → 37/37 (add new verifiers for each change).
2. Config dry-run: `load_bot_config` + `resolve_env_vars` + `validate_config`
   — no funds.
3. Live test only with explicit user approval, stated expected cost, burner
   wallet, `enabled: true` for one session, then back to `enabled: false`.
4. Commit + push per phase; never push secrets (`.env` stays ignored).

## Immediate next actions (awaiting user decisions)
1. Commit Phase 0 local fixes (gate guard, x-token, retry keys, config) + push.
2. Pick Phase 1 position size: **0.01 SOL (audit)**, 0.005 SOL (conservative), or keep 0.0001 temporarily.
3. Pick exit strategy for the default config: `tp_sl` (audit trial) vs hardened `time_based`.