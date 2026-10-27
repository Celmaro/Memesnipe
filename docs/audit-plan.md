# Implementation plan — external audit (staged_trail-tuned focus)

Audit: 251-line review of Memesnipe (research + exit-experiment + copy-trader + fees).
Status: **R5 fully implemented (commits f616794 → c7b33d3).** Shadow mode stays ON;
every item shipped disabled-first with a regression verifier. The P1-4 sweep and
P4-2 held-out validation are the two DATA deliverables run against replay archives
(see README section below); the sweep tool lives in tools/gate_sweep.py.

## Verified-against-code notes (audit claims checked this session)

- **strategy_close re-pricing** — CONFIRMED in `strategies_ledger.py` (close rows use
  one live close price for all strategies, uniform pnl). By design; A/B signal lives
  in `strategy_decide` rows (per-strategy first exit at its own trigger price).
- **_mirror_buy mode flag** — CONFIRMED in `universal_trader.py`: checks
  `self.shadow_capture is not None` instead of `self.shadow_mode`. Latent, not active:
  trader hard-guards `shadow_capture = capture if shadow_mode else None`.
- **_maybe_mirror ignores sells** — CONFIRMED (`engine.py`: `if not trade.get("is_buy"): return`).
- **No time-based exit** — CONFIRMED (`max_hold_time: null`, `stagnation_exit_min: 0`;
  stagnation machinery exists at `universal_trader.py:243` but is disabled).
- **copy_trader.enabled: true** — CONFIRMED in yaml (audit recommends false until lifecycle done).
- **qualify_leader / check_entry_drift unused** — CONFIRMED: exported in `copy_trader/__init__.py`,
  never called by the engine (grep shows only the import).
- **GMGN field skew** — CONFIRMED: profiler emits `total_trades`, engine maps
  `total_trades_30d` from `windows["30d"]["trades"]`; `tokens_traded` vs `tokens_30d`
  adds to the skew (audit: missing fields → neutral score).
- **factory cadence artifact** — CONFIRMED as a config constant in `rug_score.py`
  (`cadence_ceiling: 60.0`); gate off (`factory_deploy_rate: 0`). First-observation
  denominator artifact NOT yet located in code (see A2).

---

## R5-P0 — money-safety / execution (fix measurement before trusting results)

| ID | Item | Evidence | Current | Action |
|---|---|---|---|---|
| R5-P0-1 | Ledger records each strategy's OWN first exit (price/time/share/net) | audit §2 P0-1; code: decide rows carry trigger price, close rows re-price | `strategy_decide` = first-exit signal per strategy (price, reason, hold_seconds); `strategy_close` = live-close re-price for all | Add `decide_first` fields: store each strategy's first fire in the position dict; on close emit `first_exit_price`, `first_exit_reason`, `first_exit_hold_s`, `partial_share`, `net_pnl_quote` (fee-adjusted in shadow via config fee model). Keep `strategy_close` as-is for completeness. Verifier: `verify_ledger_first_exit.py` — open sim, feed two marks (strategy A fires at mark 1, B at mark 2), assert A's first_exit = mark 1, B's = mark 2, close rows unchanged |
| R5-P0-2 | Static vs dynamic pre-buy gates split; re-evaluate dynamic right before buy | audit §2 P0-2; code: `_skip_reason()` (static, has creator-cadence side effects) → `wait_after_creation` → `_curve_gate_reason()` → buy | single gate pass | Split: static checks (quote mint, transfer fee, mayhem, blocked creator, blacklist, age) run on first detection; dynamic checks (curve progress, MC, wash/cofire/holder snapshot) re-run immediately before buy submission. Do NOT call `_skip_reason()` twice (double-counts creator timestamps — side effect confirmed at `universal_trader.py:1975`'s cadence bookkeeping). Verifier: `verify_prebuy_gate_order.py` — assert static fires without RPC, dynamic fires after wait, creator cadence counted once |
| R5-P0-3 | Creator-PnL blacklist evidence: net-of-cost, sufficient sample | audit §2 P1; config `creator_pnl_min_trades: 3`, threshold `<= 0.0` | shadow closes feed blacklist (gross of fees) | Change ingestion to net-of-fees (subtract modeled round-trip cost before cumulating); raise min_trades to ≥ 10; persist timestamped source. Keep enabled (already on) but recalibrate only after R5-P0-1's net field exists. Verifier: `verify_blacklist_net.py` — feed 3 gross-positive/net-negative shadow closes, assert blacklisted only after net calc + min_trades |
| R5-P0-4 | Copy-trader lifecycle: mode flag, sells, qualification wiring | audit §2 P0-3 (code-verified above) | `enabled: true` but no sell-follow, no qualification call, latent wrong mode flag | (a) `_mirror_buy` → check `self.shadow_mode`; (b) `_maybe_mirror` → handle sell events (reconcile position, exit on leader sell); (c) call `qualify_leader()` + `check_entry_drift()` in the mirror path; (d) keep `copy_trader.enabled: false` until (a)-(c) land. Verifier: `verify_copy_hardening.py` updated — sell event closes mirrored position; mode flag drives branch |

## R5-P1 — strategy / profitability

| ID | Item | Evidence | Current | Action |
|---|---|---|---|---|
| R5-P1-1 | Aligned exit experiment matrix (ledger + live) | audit §4 matrix; grid now aligned 0.20 | grid: tp_sl, tp20_sl10, tp30_sl20, staged_trail(0.20), trail8, hold | Add to ledger strategies: `st_tp20_sl10` (staged + tighter stop 0.10), `st_tp30_sl20` (later profit 0.30), `trail10` (pure trail, trail 0.10), time-control variants after R5-P1-3 lands. Verifier: `verify_exit_matrix.py` — spec parse + one ladder per variant |
| R5-P1-2 | Time-based/ stagnation exit (strand capital protection) | audit §4; code: stagnation machinery exists, off | `max_hold_time: null`, `stagnation_exit_min: 0` | Wire stagnation exit as ledger strategy first (120/300/600s hold), then tune live value. Do NOT change live gates yet — user set them for sample throughput. Verifier: `verify_stagnation_exit.py` — flat price path triggers exit after configured minutes |
| R5-P1-3 | Net-of-cost model in backtester/ledger | audit §6; pump.fun fee 1.25%/side; priority ~0.28% round-trip | backtest gross | Add cost model: buy fee + sell fee (1.25% each side by default) + priority fee (from config) + modeled impact (order size vs pool depth). Shadow ledger deducts the same. Verifier: `verify_cost_model.py` — known price path yields expected net PnL |
| R5-P1-4 | One-parameter-at-a-time gate sweeps on net results | audit §7 | MC 40 SOL, progress 0.25 SOL, wait 1s, confirm 1 pass | Sweep MC 30/35/40/45, progress 0.25/0.5/1.0, wait 1/2/3s, confirm 1/2 passes on the archived replay hours; report net expectancy/profit factor/drawdown/missed-entry rate per cell. Verifier: `verify_sweep_report.py` — sweep runner emits the metric set |

## R5-P2 — edge / hardening

| ID | Item | Evidence | Current | Action |
|---|---|---|---|---|
| R5-P2-1 | GMGN profile field mapping + qualification wiring | audit §5; profiler `total_trades` vs engine `trades`, `tokens_traded` vs `tokens_30d` | min_leader_score: 0.0 (off) | Normalize profiler payload keys (`total_trades`→`trades`, `tokens_traded`→`tokens_30d`) and fetch windows in parallel (not sequential 1d→7d→30d). Cache profile in background process, decision path reads local record. Always gate off until R5-P0-4 lands. Verifier: `verify_copy_trader_qualification.py` (exists) extended — field mapping + cached path |
| R5-P2-2 | Factory cadence artifact (first-observation = 60/h) | audit §2 P1; `cadence_ceiling: 60.0` in rug_score.py (gate off) | `factory_deploy_rate: 0` | Locate cadence computation (per-creator launch timestamps at universal_trader.py:773) and fix denominator floor; require ≥2 observations before cadence is meaningful. Keep gate off until calibrated. Verifier: `verify_factory_artifact.py` — one observed launch → cadence None/flag, not 60/h |
| R5-P2-3 | Post-entry protection as shadow experiments | audit §3-B | exit_on_creator_sell/rug_heuristics/fragility all off | Log-only variants: creator-sell exit, pool-drain exit, buyer-growth check, wash/cofire post-entry advisory. No hard gates until ledger shows persistent effect. Verifier: each ships log-only guard + `verify_*` ladder |

## R5-P4 — research methodology

| ID | Item | Evidence | Current | Action |
|---|---|---|---|---|
| R5-P4-1 | Fee-corrected expectation in every report | audit §6-7 | reports quote gross avg/total | All "show results" tables carry net column once R5-P1-3 lands; flag grid verdicts only at ≥100 closes/strategy (unchanged rule) |
| R5-P4-2 | Chronologically-separated validation | audit §7 | backtests on one day (hour 09–13) | Reserve later archived hours (e.g. hour 14–16, once cached) as held-out test set after any threshold change |

---

## RISKS — explicitly NOT to copy

- **Paper thresholds wholesale** (audit itself flags): universal 0.25-SOL progress, exact Gini 0.8 cutoff, first-5-minute rug classifiers — evidence is directional only; calibrate on our own replay + ledger before arming any gate.
- **Absolute numbers from other snipers** (skill pitfall): fee/tip/latency figures from a 9-SOL-position operator do not transfer to 0.01 SOL test size — adopt the mechanism, scale fees as fraction of position.
- **Trailing-fill optimism** (already learned live): the replay backtester fills trails at the exact trigger price; live fills gap past it (med −21…−27% vs nominal −20%). Any backtest number quoted as live-expected must carry this discount.
- **Copy-trader live arm** before R5-P0-4 completes — the audit's whole §5 is about not doing this.

## Verification protocol

1. `uv run tests/regression/run_all.py` — full offline suite before/after each change (moves no funds).
2. Per-item verifier listed in the table; new items ship the verifier with the change.
3. Shadow mode stays ON; no real-mode item ships enabled.
4. Each item lands as its own commit (e.g. `feat(plan): …`), plan file committed first.
5. Exit-ledger and gate thresholds calibrate on replay archives before any live arm; thresholds ship conservative.
6. Secret scan before any commit (`.env`/`state/`/`shadow_data/` gitignored).

## Suggested order

R5-P0-1 (ledger first-exit) → R5-P0-3 (net blacklist) → R5-P1-3 (cost model) →
R5-P0-2 (gate split) → R5-P0-4 (copy lifecycle) → R5-P1-1/2/4 (matrix + time + sweeps) →
R5-P2-1/2/3 → R5-P4-1/2 (reporting + held-out). P0 items are prerequisites for trusting
any P1 measurement.