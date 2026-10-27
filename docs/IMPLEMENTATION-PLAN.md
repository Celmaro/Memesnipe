# Memesnipe Implementation Plan

> Status (26 Oct 2026): Phases 1–7 implemented in `main`; PLUS the T1
> hardening batch from the 12-repo research (commit `c5888aa`) landed and was
> live-validated in shadow mode. Code ships disabled-first and is verified by
> `tests/regression/run_all.py` (40/40). Remaining before live use: an armed
> test run at user-approved size, then the Phase 6 ledger to measure net
> expectancy.

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

## T1–T3 deep-analysis implementation status (from repo deep-dive)

All shipped **disabled-by-default** (safe baseline) unless noted; regression
suite 41/41, config valid.

- **T1.4 Fee auction (enabled):** `dynamic_fee.py` now samples
  `getRecentPrioritizationFees`, bids `sample_max × multiplier`, TTL-caches the
  bid, falls back to fixed. Key: `priority_fees.enable_dynamic`.
- **T2.1 Warm curve reads:** `curve_manager.get_pool_state` TTL-caches pool
  reads (1s), so the gate and the buy share one RPC round-trip.
- **T2.2 Jito bundle executor:** `src/execution/executor.py`
  (TransactionExecutor protocol, DefaultExecutor, JitoBundleExecutor with
  getTipAccounts + tip transfer + sendBundle, FallbackChainExecutor with
  signature dedupe). Wired through buyer/seller; key: `jito.enabled` (off).
- **T2.3 CPI-event decode fallback:** geyser parser now walks
  `meta.inner_instructions` when logs/top-level yield nothing, catching
  router-created coins.
- **T2.4 Confirmation window:** curve gate can require N consecutive passes
  (`filters.consecutive_match_count`, default 1 = unchanged).
- **T2.5 Honeypot gates:** `src/screening/honeypot.py` — freeze-authority
  (mint layout offset 45) + curve supply-hold burn gate. Keys:
  `filters.honeypot_freeze_authority`, `filters.min_curve_hold_fraction`
  (both off).
- **T2.6 RPC catch-up backfill:** `src/backfill/transaction_crawler.py`
  (getSignaturesForAddress cursor pages, bounded concurrency, checkpoint).
  Runs at startup only when `trade.backfill.enabled` (off).
- **T3.1 Copy-target store:** `src/screening/copy_target_store.py` — SQLite
  targets with per-wallet fixed size/slippage/limits; verified by
  `verify_copy_trader_store.py`.
- **T3.2 TradeEvent decoder + mirror engine:** `parse_trade_event_from_logs`
  on the pump.fun parser + `src/copy_trader/engine.py` mirroring leader buys
  through a dedicated buyer. Key: `copy_trader.enabled` (off).
- **T3.3 GMGN wallet-profiling adapter:** `src/copy_trader/gmgn_profiler.py`
  — TTL-cached profile fetch, consulted before mirror (risk filter, not a
  gate).
- **T3.4 SQLAlchemy guard:** behavioral regression
  `tests/regression/verify_copy_trader_store.py` guards the reference
  Python-`and`-drops-filter bug shape.

## Round-2 deep-analysis implementation plan (8 repos, 2026-10-26)

Analysis of 8 repos (4 studied in depth via subagents: Copy-Trading-Bot-Rust,
Solana_Memecoin_Sniper_Bot, solana-copy-sniper-mev-trading-bot,
solana-trading-bot-v3, pump-fun-sdk, ssp-protocol; 2 were README-only scam
repos — Free-Solana-Sniper-Memecoins-Bot-2026 (landing-page bait for
meme-snipe.com) and Trpjan-solana-trading-toolkit (marketing pitch, no code)).
Clones deleted after analysis. Items marked **DONE** were already shipped in
the T1–T3 batch (the confirmation window T2.4 matches v3's
CONSECUTIVE_FILTER_MATCHES and the Jito executor already does endpoint fan-out
+ random tip accounts).

## Round-2 P0 — fee-aware quoting (highest value, correctness)

- **P2-FEE-1 (DONE 2026-10-26): Fee-aware curve math (pump.fun).** The SDK's `bondingCurve.ts:393-606`
  quotes with the PumpFees `fee_config` (tiered by market cap and quote mint:
  SOL-like → feeTiers, USDC → stableFeeTiers, exotic → exoticFlatFees, ceilDiv
  per component). Memesnipe's `calculate_buy_amount_out`/`calculate_sell_amount_out`
  (`curve_manager.py:166-224`) are raw constant-product with NO fee deduction;
  `platform_aware.py` reads `fee_fraction` that never gets set. Consequence:
  sell `min_sol_output` set above what chain pays after fees → slippage reverts;
  buy `max_sol_cost` understates cost → exceeded, reverts. Add fee deduction to
  both calculations + set `fee_fraction` from the curve decode. This single fix
  prevents a class of live revert losses.
- **P2-FEE-2 (DONE 2026-10-26): `maxSafeSellAmount` + chunked sells for the sweeper.** SDK
  `bondingCurve.ts:47-65` bounds sell = `min(u64::MAX, floor(0.9·u128::MAX /
  virtualQuoteReserves))` and refetches per chunk (`onlineSdk.ts:664-720`);
  the reference notes 83% of real mainnet sells exceeded naive u64 limits →
  AnchorError 6024 Overflow. Port a `max_safe_sell_amount` and make the
  stuck-token sweeper chunk. — Shipped the clamp in the seller's sell path
  (PlatformAwareSeller.execute) with `max_safe_sell_amount()` in
  curve_manager.py; the persistent exit watcher drains the remainder on
  later passes.

## Round-2 P1 — copy-trader upgrades (mirrors open-sol-bot/Copy-Trading-Bot-Rust)

- **P2-COP-1 (DONE 2026-10-26): `percent_of_leader` sizing mode on CopyTarget.** Rust bot mirrors
  `amount_in * percent / 100` (`main.rs:122-166`); Memesnipe only supports
  `fixed_buy_sol`. Add percent mode with existing min_notional/max_position as
  clamps. (T3.1 store schema: new column, default 0.0 = fixed mode.) — Shipped
  as `percent_of_leader` column + migration + `_mirror_size()` in the engine.
- **P2-COP-2 (DONE 2026-10-26): TradeEvent full-field decode.** The SDK's TradeEvent carries
  `quote_amount`, `creator_fee_unclaimed`, `mayhem_mode` — Memesnipe's
  `parse_trade_event_from_logs` decodes only 6 fields. Fill the rest for
  better leader sizing. — Decoder now maps all 33 IDL fields (trader/user,
  amounts, fee bps, creator, mayhem, quote mint) via `_trade_event_to_dict`.
- **P2-COP-3 (DONE 2026-10-26): Aggregator/two-platform dedupe in the copy feed.** One wallet
  trading via Jupiter or across two venues double-mirrors; add a
  per-(trader,mint) TTL dedupe keyed on event identity, not just mint. —
  `_last_mirrored` keyed on `"{trader}:{mint}"`.

## Round-2 P2 — exit/risk hardening (ssp-protocol, MEV bot)

- **P2-EXIT-1 (DONE 2026-10-26): Absolute depth-floor forced exit.** SSP's backstop fires when
  pool depth drops below a floor (`deploy_backstop.rs:20-26`). Add to the exit
  watcher: when remaining quote reserves cannot absorb a full unwind within the
  slippage budget, dump immediately instead of waiting for TP/SL. — Shipped as
  `min_pool_depth_sol` (0 = off) + `ExitReason.LIQUIDITY` + `_check_depth_floor`
  in the monitor loop; verified by verify_depth_floor_exit.py.
- **P2-EXIT-2 (DONE 2026-10-26): Depth-target unwind sizing.** `amm.rs:38-43` inverts impact:
  `notional * 10_000 / max_impact_bps` = max absorbable size. Split oversized
  exits into chunks (ties into P2-FEE-2). — Shipped as `max_sell_impact_bps`
  (0 = off) on the seller: constant-product impact cap Δ ≤ T·b/(10000−b),
  remainder drains on later monitor passes; verified by
  verify_impact_clamped_sell.py.
- **P2-EXIT-3 (DONE 2026-10-26): Sell-side automatic slippage inflation.** MEV bot adds +10bps
  extra slippage to sells automatically (`swap.rs:55-58`). Cheap, high-value. —
  `sell_slippage_inflation_bps` (default 10) on the seller; wired through
  bot_runner + YAML.
- **P2-EXIT-4 (DONE 2026-10-26): Slot-delta rug heuristics.** `hardcap.rs:62-118`: force-sell if
  <2 unique buyers 12 slots after our buy; early SOL drain within 1 slot of our
  buy = instant sell. Slot-granularity beats second-resolution polling. —
  Shipped as `filters.rug_*` (off by default): entry slot+depth captured at
  buy, buyer counting through the listener trade feed, monitor-loop check via
  `screening/rug_heuristics.py` + `ExitReason.RUG`; verified by
  verify_rug_heuristics.py.
- **P2-EXIT-5 (DONE 2026-10-26): ATA-close inside the sell tx.** v3 appends
  `createCloseAccountInstruction` to every sell (bot.ts:347), reclaiming rent
  atomically instead of as a separate post-sell cleanup step. — Shipped as
  `trade.close_ata_on_sell` (off by default); the close is appended only when
  an RPC balance read proves the ATA holds exactly the sell amount (a close on
  leftover tokens reverts the whole sell); verified by verify_ata_close_on_sell.py.

## Round-2 P3 — structural (lower urgency)

- **P2-STRUC-1 (DONE 2026-10-26): 50ms blockhash refresher.** `tx.rs:37-46` keeps a cached
  blockhash refreshed at 50ms so no send waits on RPC. Align
  `get_cached_blockhash` cadence on the hot path. — Shipped as
  `trade.blockhash_refresh_interval` (default 5.0s for rate-limited public
  RPCs, floor 0.05) driving the background updater, plus a freshness-aware
  `get_cached_blockhash` that refetches synchronously on the hot path when the
  cache is stale (2x interval, 1s floor) and falls back to the last-known hash
  on RPC blips; verified by verify_blockhash_freshness.py.
- **P2-STRUC-2 (DONE 2026-10-26): Creator-PnL memory → persistent creator blacklist.**
  `trade_logger.rs:32-88`: append trade outcomes; creators whose net history is
  negative go into a persistent skip list. — Shipped as
  `filters.creator_pnl_*` (off by default): SQLite ledger
  (`screening/creator_pnl_store.py`) fed at exit, consulted in `_skip_reason`,
  needs BOTH >= min_trades AND net <= max_net_sol to ban; verified by
  verify_creator_pnl_blacklist.py.
- **P2-STRUC-3 (DONE 2026-10-26): Batch-position liquidation via byte-offset memcmp pool
  discovery.** `raydium.rs:148-208` shows the canonical zero-API Raydium pool
  discovery (offsets 400/432, DataSize filter) — blueprint for a
  GraduatedMarket/Raydium extension. — Shipped as
  `platforms/raydium/pool_discovery.py` (dataSize 752 + memcmp 400/432,
  client-side AmmInfo verifier, fail-open discovery) + `get_program_accounts`
  on the client; blueprint-only, not wired into any hot path; verified by
  verify_raydium_pool_discovery.py.
- **P2-STRUC-4 (DONE 2026-10-26): Jito auth-keypair rotation + accepted-signal authority.**
  Rotate JITO_AUTH_KEYPAIR per region and treat the block-engine
  accepted/rejected stream as authoritative for bundle landing instead of a
  second RPC confirm. — Shipped as `jito.auth_keypairs` (empty = unsigned):
  round-robin auth header signing (`x-jito-signing-key` + `Jito-Signature`),
  and sendBundle bundleId = accepted (no RPC confirm) / null|error = rejected
  / malformed = fall back to RPC; verified by verify_jito_auth_signal.py.

## Round-2 RISKS — explicitly NOT to copy (recorded for the record)

- Committed private/API keys and key-append endpoints (Solana_Memecoin_Sniper_Bot
  data.json, WalletRoute no-auth, JWT default "JWT_SECRET"; MEV bot's Shyft key
  in source). Memesnipe `.env`-only + redaction is correct.
- `minAmountOut: 0` / 100% slippage on swaps — the bot-drainer pattern.
- Pre-send simulation on the hot path (dead latency for sniping).
- Config hot-reload that tears down live subscriptions.
- The fake-volume / LP-burn / sandwich toolkit half (rug tooling).
- Warp-style third-party unsigned-tx relay (trust/MEV-leak vector).
- Multi-user key custody in a shared DB (scam-honeypot pattern).
- Creating a huge new copy/launch tool: several "bots" are gutted teasers
  (empty function bodies) selling a paid version; no validated production code
  to port beyond the structural ideas above.

## Verification protocol for every phase
1. `uv run tests/regression/run_all.py` → 41/41 (add new verifiers for each change).
2. Config dry-run: `load_bot_config` + `resolve_env_vars` + `validate_config`
   — no funds.
3. Live test only with explicit user approval, stated expected cost, burner
   wallet, `enabled: true` for one session, then back to `enabled: false`.
4. Commit + push per phase; never push secrets (`.env` stays ignored).

## Immediate next actions (awaiting user decisions)
1. Commit Phase 0 local fixes (gate guard, x-token, retry keys, config) + push.
2. Pick Phase 1 position size: **0.01 SOL (audit)**, 0.005 SOL (conservative), or keep 0.0001 temporarily.
3. Pick exit strategy for the default config: `tp_sl` (audit trial) vs hardened `time_based`.

---

## Round-3 FULL RE-AUDIT (2026-10-26) — everything re-checked, don't-just-assume edition

Method (evidence-based, per user "recheck ALL the repos / the TWO audits / ALL the articles. Research internet. DOnt just assume."):

- **Repos re-examined: all 20** (round-1 12 + round-2 8). 9 parallel subagents + local deep-dives on the rate-limited repos (pump-fun-sdk, sol-trade-sdk, solana-trading-bot, solana-defi/alpamayo, open-sol-bot). 2 confirmed README-only scam (Free-Solana-Sniper-Memecoins-Bot-2026 funnels to meme-snipe.com phishing; Trpjan-solana-trading-toolkit = zero code). 18 substantive.
- **The two audits re-read from the original attachments** and re-verified claim-by-claim against current code (most P0/P1 items are DONE; the remaining gaps are listed below).
- **Articles re-pulled** (orbitalflare, bolttx, allenhark, yavorovych medium) + arxiv 2608.20271 + bitquery investigations.
- **Internet research** on Mayhem Mode, sandwich/MEV (DontFront), holder-concentration rules of thumb.
- **Full local audit** of the bot: module tree, dead-code, config, exit/monitor paths, copy engine, capture ledger.

Classification: **[NEW]** = not in Memesnipe at all · **[PARTIAL]** = exists in some form but the reference is sharper · **[VERIFY]** = Memesnipe claims it, must be proven against v3 docs.

### Priority-0 (live-money safety / correctness)

| # | Item | Source (file:line) | Type | Plan |
|---|---|---|---|---|
| R3-P0-1 | **Dynamic fee tiers are market-cap-scaled, not flat** | pump-fun-sdk `docs/pump-official/FEE_PROGRAM_README.md`: `computeFeesBps` → `calculateFeeTier(feeTiers, bondingCurveMarketCap)` where MC = `virtualSol·mintSupply/virtualToken`. Post-2026-09-01 fee program | VERIFY | Re-verify `curve_manager` reads the **live `fee_config` account** and applies tiered bps by MC on every quote (not cached flat). P2-FEE-1 landed; this is the "is it actually current" check. Add a verifier that feeds a tiered `fee_config` and asserts the quoted fees match the tier table by MC band. |
| R3-P0-2 | **Mayhem Mode coins are a distinct asset class** | bitquery investigation (`pump-fun-mayhem-mode`): agent mints +1B supply (2B total), trades randomly 24h; **1 in 50 graduations fills the pool**; traders as a group lose. Memesnipe surfaces `is_mayhem_mode` (`event_parser`, `TokenInfo.is_mayhem_mode`) but has **no strategy handling** | NEW | Add `filters.mayhem_mode: trade|skip` (default `trade` = current behavior unchanged). When `skip`, `_skip_reason` rejects mayhem coins with a clear skip_reason for the research ledger. When trading, apply a tighter `max_market_cap_sol` cap and a 15s hold TTL (mayhem coins swing hard on the randomized agent). Verifier proves the skip path and that the fee-recipient difference is preserved. |
| R3-P0-3 | **Holder-rewards coins change dev incentives** | pump-fun-sdk `docs/pump-official/HOLDER_REWARDS_README.md`: `create_v2` trailing `is_holder_reward`; creator fee goes to holders; `is_cashback_enabled` must be false (cashback **deprecated**, `create_v2` rejects it). `BondingCurve.creator` still routes fee, `creator_vault` unchanged | PARTIAL (parse) | Memesnipe parses `is_holder_reward` on events but has no policy. Add `filters.holder_reward_mode: trade|skip` (default trade) + log/ledger field. Document that the old cashback claim cookbook is legacy-only (grandfathered coins). |

### Priority-1 (strategy correctness / edge)

| # | Item | Source | Type | Plan |
|---|---|---|---|---|
| R3-P1-1 | **Execution-ledger net-metrics module** | Audit #1 §6 (build execution ledger: PnL/trade, profit factor, drawdown, fill rate, median hold, % unsellable); audit #2 diagnostic scripts; confirmed ABSENT from Memesnipe (no profit_factor/drawdown/fill_rate anywhere) | NEW | `src/research/net_metrics.py` — read `shadow_data/*.jsonl` (and real `trades/` when armed), compute the audit's metric set, emit a report after each session. Verifier: feed a small captured ledger, assert computed PnL/PP/drawdown. |
| R3-P1-2 | **Staged TP/SL (range-based partial exits)** | allenhark MC-breakout docs: `sell_strategy.tp_ranges` / `sl_ranges` — sell a share per step up (TP) / per step down (protect-gains), everything at band bottom; a range start==end is a single sell and still fires if price jumps past | NEW | `trade.tp_ranges: [{from_pct, to_pct, step, share}]` + `sl_ranges` (0 = current single TP/SL unchanged). Monitor loop computes partial-sell sizes; engine mirrors via existing sell path. Disabled-first (0 = off). Verifier: range math + jump-past behavior. |
| R3-P1-3 | **Aggregator sell fallback (Jupiter) when the pool is gone / won't take the sell** | allenhark docs `exits.aggregator_fallback` + `aggregator_slippage_bps` (500 = 5%) + `aggregator_max_price_impact_pct`; bolttx "no-fill / pool gone" failure mode | NEW | `trade.aggregator_fallback: false` (default off). When the platform sell path returns pool-gone/liquidity error and the flag is on, route through Jupiter `swap` with quoted slippage cap. Needs a Jupiter quote/send client (`src/execution/jupiter_client.py`). Verifier: mock Jupiter quote + failure-path selection. |
| R3-P1-4 | **Sell-when-creator-sells exit** | allenhark docs `exit_on_creator_sell` (not set by default) | NEW | `filters.exit_on_creator_sell: false`. Monitor loop watches listener trade feed for a sell from `token_info.creator` (pump.fun creator vault / creator wallet); force-exit on trigger. Reuses `_init_rug_trade_feed` buyer/seller counting. Verifier: feed creator sell event → exit_reason fires. |
| R3-P1-5 | **Holder-concentration gate (top-N share + unique-holder floor)** | solanatools 2026 playbook: <300 holders in 5 min = sniper-only; top-10 >50% supply = distribution risk. arxiv 2608.20271: early trades (unique traders, buy/sell ratio, big buys ≥1 SOL, dev trades) from first 5 min predict rugs; XGBoost + multi-source fusion best | NEW | `filters.min_unique_holders`, `filters.max_top10_concentration` (0 = off). Background holder-snapshot refresher (mirrors sol-trade-sdk pre-buy risk gate pattern — see R3-P2-1): a periodic `getTokenLargestAccounts` pull publishing an immutable snapshot; `_skip_reason` does a local hash lookup, never RPC on the hot path. Verifier: concentration math + fail-open on stale snapshot. |
| R3-P1-6 | **Anti-bundler extreme-fast calibration** | Audit #2 §2.1 table: at block-0, 0.1 SOL buys 3.56M tokens (clean) vs 2.62M (5 SOL dev bundle). Set `extreme_fast_token_amount` + slippage so a >2.5 SOL dev bundle reverts on-chain | PARTIAL | When/if `extreme_fast_mode` is ever enabled, ship the calibrated pairs table in `bots/README` + a config validator asserting the pair is internally consistent (amount × price ≤ max MC). No code change needed now; document + verifier. |

### Priority-2 (structural / infra / edge)

| # | Item | Source | Type | Plan |
|---|---|---|---|---|
| R3-P2-1 | **Pre-buy risk gate (synchronous local-snapshot, background-refreshed)** | sol-trade-sdk `docs/PRE_BUY_RISK_GATE.md` + `LOW_LATENCY_BOTS.md`: gate runs after validation, before build/sign; risk data fetched in background into an immutable snapshot (arc-swap); **never RPC from the gate** | PARTIAL | Refactor `_skip_reason`/`_curve_gate_reason` toward snapshot-backed lookups (creator-PnL store, holder concentration, honeypot cache) where the data refreshes off the hot path. Keep async curve reads for live state. Disabled-first; verifier proves the gate is sync-lookup with no RPC when snapshots are warm. |
| R3-P2-2 | **Durable nonce pool** | sol-trade-sdk `docs/NONCE_CACHE.md`: replay protection, extended validity, offline pre-signed txs | NEW | `trade.use_durable_nonce: false`. Optional nonce account pair (advance authorized + nonce) used when set; extends the blockhash window beyond 150 blocks and prevents signature replay. Verifier: nonce-account tx layout. |
| R3-P2-3 | **Address Lookup Tables (ALTs)** | sol-trade-sdk `docs/ADDRESS_LOOKUP_TABLE.md`: smaller txs, lower fees, faster validation | NEW | Optional `trade.alt_addresses: []`. When set, compress static keys into a versioned tx with ALTs (used on the buy path). Verifier: tx builds with ALT and identical instruction data. |
| R3-P2-4 | **SWQoS / multi-provider tip capture + fee ground truth** | sol-parser-sdk `src/transaction_cost.rs:255-279,402-451`: confirmation-gated cost parse across 17 tip-account providers; carbon pump-fees decoder | NEW (research) | Port the tip-account registry into research capture (`shadow_capture.sim_close` gains paid_fee_sol from parsed cost when available) to feed the dynamic fee auction with observed ground truth. Disabled-first; verifier on the cost parser. |
| R3-P2-5 | **PumpSwap graduation watcher (migrate instruction)** | audit #1 copy-trader gap ("cookbook watcher explicitly excludes PumpSwap"); solana-defi `Shredstream/Pumpfun/stream_pumpfun_to_pump_swap_amm_migration_transactions` detects `migrate` real-time; bolttx "graduation events" as a signal | PARTIAL | Copy-trader engine already tracks graduated sells (`pumpswap_market`), but the wallet-trades watcher misses post-graduation. Add a `migrate`-event flag on the watcher + copy feed so a leader's post-graduation exit is seen. Verifier: migration event → watcher state. |
| R3-P2-6 | **Sandwich/MEV protection (DontFront) on real mode** | solana.com MEV docs; bitquery sandwich investigation ($29.7M taken; 1-3% slippage is the room bots use; 39% of take from bot wallets); Memesnipe present: jito + tight slippage; absent: DontFront/DCA shield | NEW (real-mode) | `jito.dontfront: false`. When real mode is armed, optionally route through Jito DontFront RPC (`mainnet.block-engine.jito.wtf` DontFront variant) for buy/sell. Shadow-mode unaffected. Verifier: executor selection with flag. |
| R3-P2-7 | **Cross-window (24h/7d/30d) leaderboard qualification** | audit #1 §5 qualification table ("simultaneous presence in 24h/7d/30d leaderboards as a candidate filter") | PARTIAL | `gmgn_profiler` currently reads single-window profile payload. Add 7d/30d window fetch + keep-all-windows-positive qualification in `copy_trader/qualification.py` `qualify_leader` (already has drift + min-trades). Verifier: 7d-negative rejects. |
| R3-P2-8 | **Metadata socials gate (≥1 non-empty metadata extensions entry)** | solana-trading-bot-v3 `filters/mutable.filter.ts:61-65` (task-1 subagent verified absent in Memesnipe); honeypot.py already notes the mutable-metadata check "needs its own decoder" | NEW | `filters.min_metadata_extensions: 0`. When >0, `_skip_reason` requires ≥1 non-empty metadata `extensions` entry (metadata account read off hot path via background refresher). Verifier: metadata with zero extensions rejected when enabled. |
| R3-P2-9 | **Consensus-reset filter semantics** | solana-trading-bot-v3 `bot.ts:357-391` + README:100-101: on any failed check `matchCount` resets to 0; only N **consecutive** passes allow the buy (stale-filter guard) | VERIFY | Check `consecutive_match_count` implementation resets on failure. If it does not (or if a failed gate is sticky), port the reset-on-failure semantics. Verifier: fail-then-pass sequence requires N fresh passes. |
| R3-P2-10 | **Daily-loss / budget / max-concurrent guards** | allenhark docs: `global.daily_loss_limit_sol`, `global.budget_sol`, `global.max_concurrent_tokens` | NEW | `risk.max_concurrent_positions: 1`, `risk.daily_loss_limit_sol: 0` (0 = off), `risk.budget_sol: 0` (0 = off). Tracker + gate in `_handle_token`/monitor. Disabled-first. Verifier: loss-limit blocks new buys, sells continue. |
| R3-P2-11 | **CPI outer-signer attribution for copy/rug paths** | carbon `crates/core/src/instruction.rs:36-41` (CPI `absolute_path` + `stack_height`). Memesnipe's listener forwards **only log strings** to the copy engine and discards `message.account_keys` (`universal_geyser_listener.py:406-412`); `event_parser.py:224` attributes via `TradeEvent.user` → router wallet on aggregator/CPI buys | NEW | Pass the tx's real signer / CPI path along with the trade dict so copy-trader and the rug-buyer heuristic match **human wallets, not router contracts**. Small diff, high correctness value; disabled-first. Verifier: CPI-routed buy attributes to outer signer. |
| R3-P2-12 | **Event-driven sell-fill trigger via own-wallet log subscription** | Solana_Memecoin_Sniper_Bot `track/raydium/raydium.ts:815-821` + `249-350`: subscribe program logs for the bot's OWN wallet; on matched SWAP drive the exit immediately | NEW | Wire the bot's own wallet key into the existing logs listener; on a matched SWAP evaluate tp/sl/graduation exit immediately instead of waiting for the monitor loop's next tick. Pure wiring, protects the exit side of every trade; disabled-first (off = current polling). Verifier: own-wallet SWAP event → immediate exit evaluation. |
| R3-P2-13 | **Strict buy/sell serialization** | solana-trading-bot-v3 `bot.ts:118-128` `async-mutex` + `sellExecutionCount` guard (skip new buy while a sell is in flight, release in `finally` `bot.ts:192-196`). Memesnipe's `universal_trader.py`/`executor.py` have no asyncio.Lock/Semaphore — WSOL-wrap balance race risk | NEW | Audit buy/sell interleaving; add an asyncio lock guarding buy-vs-sell overlap (WSOL wrap/unwrap balance races). Disabled-first. Verifier: concurrent buy/sell requests serialize. |
| R3-P2-14 | **Slot-ordered event delivery for monitor/copy path** | sol-parser-sdk `src/grpc/buffers.rs:44-59,118-175` (SlotBuffer watermark + late-drop + MicroBatch/StreamingOrdered `types.rs:10-27`) | PARTIAL | Port a per-slot/tx_index ordering buffer for the exit monitor + copy-trader fill consumption (out-of-order fill corrupts position state); keep the snipe hot path unordered. Disabled-first. Verifier: out-of-order events → ordered consumption. |
| R3-P2-15 | **In-stream filter update + continuity revision counter** | solana-streamer `yellowstone_grpc.rs:378-428` (`update_subscription` — change watched program IDs / copy targets without reconnect gap); sol-parser-sdk `client.rs:49-56,81-89` (generation/continuity_revision — discard cached state built before a disconnect/drop) | PARTIAL | Add in-stream `updateSubscription` on the geyser half-stream + a continuity revision consumed by curve-cache/shadow-capture to invalidate pre-gap state. Disabled-first. Verifier: filter update with no reconnect; revision bump discards stale cache. |
| R3-P2-16 | **`real_token_reserves` buy-output cap + rounding** | pumpdotfun-sdk `src/bondingCurveAccount.ts:46-53` (`getBuyPrice` caps tokens at `real_token_reserves`, product-form rounding `n/i + 1n`). Memesnipe's `buy_amount_out` (`curve_manager.py:59-62`) neither rounds up nor caps at real reserves — near-completion curve could quote output beyond actual holdings | VERIFY | Cross-check + align `buy_amount_out` with the on-chain cap/rounding semantics; add verifier feeding a near-completion curve and asserting the quote ≤ real reserves. |

### Round-3 RISKS — explicitly NOT to copy (evidence)

- **Free-Solana-Sniper-Memecoins-Bot-2026 / meme-snipe.com** — README-only bait; third-party phishing verdict; funnel asks users to send SOL. Nothing to adopt. (task-8 verified)
- **Trpjan-solana-trading-toolkit** — zero code, marketing pitch. Nothing to adopt.
- **Solana_Memecoin_Sniper_Bot FE** — sends `privateKey` to a backend `/wallet/privatekey` endpoint = key-custody honeypot (task-1 verified). Memesnipe `.env`-only is correct.
- **carbon (7Labs)** — real indexer framework but Rust/tokio/mpsc/Postgres/GraphQL is overkill for an asyncio sniper; port ideas (fee decoder, pipeline metrics), not code.
- **warp-id "snipe-list" / hosted relay** — third-party unsigned-tx relay (trust/MEV-leak vector), keep out.
- **alpamayo self-hosted RPC stack** — heavy infra; only the frozen-data backfill angle transfers, and Memesnipe's `transaction_crawler` already covers it.
- **Pre-send simulation on the hot path** — pump-fun-sdk FAQ explicitly says static CU 100k beats per-tx simulation because simulation adds slippage risk on snipes; Memesnipe's static CU default is correct.
- **jito-shredstream-install** — the main service is **already dead** (upstream disabled ShredStream 2026-09-05; every startup script exits on the epoch guard). Worse: it ships a **real committed 64-byte `shred_keypair.json`** — a repo-wide shared operator identity; never reuse shipped key material.
- **Solana-Sniper-Rust-Bot** — README advertises a production multi-DEX sniper but the repo is **one non-compiling 116-line stub** (`mod processors;` references a missing file; `sniper_bot::` crate doesn't exist; `cargo build` fails). Premium-scam packaging; nothing adoptable from source.
- **Solana_Memecoin_Sniper_Bot mint half** — the "Meme Coin Bot" (airdrop farming, fake volume, liquidity withdrawal) is a rug-playbook; plus `minAmountOut: 0` on every swap and plaintext private keys uploaded to a backend + appended to a `priv` file. Copy nothing from its key paths.
- **Mayhem "free volume" selling** — a mayhem coin's 24h AI agent is randomized and the pump bot is *not* a directional edge; do not build a "trade the agent" strategy.

## Round-4 — Improvement batch (speed / strategy / profitability / fees)

> Derived from a 19-paper research pass (arXiv batch, Oct 2026): pump.fun
> manipulation forensics, rug-pull early detection, copy-trade manipulation
> resistance, graduation prediction, fragility scoring, MEV/slippage
> decomposition, and a 15-day live-deployment risk study. Every item ships
> disabled-first, shadow mode stays ON, one regression verifier per item.
> Counts below are relative to the Round-3 suite (77/77).

### R4-P0 — Money-safety & execution (must land before any live arm)

| ID | Item | Evidence | Current | Action |
|----|------|----------|---------|--------|
| R4-P0-1 | **Reordering-slippage accounting in the shadow ledger** | 2309.13648 (Uniswap MEV cost decomposition: price-impact + adversarial reordering; reordering slippage is measurable without mempool data) | Shadow ledger records sim_open/sim_close PnL but no slippage decomposition | Record per-fill: quote price vs executed price, price-impact estimate, and inferred reordering delta; surface `avg_reordering_bps` in the daily summary. Lets us measure how much the snipe race actually costs vs naive fills. Verifier: a shadow close row carries `reordering_bps`. |
| R4-P0-2 | **Per-path CU budget (buy vs sell) from the cost model** | 2309.13648 (small swaps = gas-dominated; large = impact-dominated) + our P0-1 fee table | One `fixed_amount` for both paths; buy/sell split already commented but unused | Wire `priority_fees.buy_fixed_amount` / `sell_fixed_amount` (already in YAML comments) through the executor; buys keep the race-worthy budget, sells use the cheaper tier. Off by default = current single fixed_amount. Verifier: buy path uses buy budget, sell path uses sell budget. |

### R4-P1 — Entry strategy (profitability)

| ID | Item | Evidence | Current | Action |
|----|------|----------|---------|--------|
| R4-P1-1 | **Multi-dimensional rug-risk score at entry** | 2608.20271 (6.4M Solana tokens; classic ML detects rugs with behavioral features; most rugs within 1h) + 2608.01609 (multi-dimensional feature design) | Rug heuristics = slot-delta drain + buyer-count only (`rug_heuristics_enabled`, off) | Add a lightweight scored gate (logistic-style weighted sum over: creator cadence, holder churn in first N slots, early-buyer concentration, liquidity depth at entry) computed offline from the shadow ledger's observed features; `rug_score_threshold` config, off by default. Pure shadow first — calibrate threshold on replay, then arm. Verifier: known-rug replay mint scores above threshold; clean replay mint scores below. |
| R4-P1-2 | **Creator "factory" / multi-coin deployment filter** | 2609.10246 (pump.fun factories: strategic actors mass-deploy and pump signals) | `blocked_creators` static list only | Track per-creator deployment cadence (mints/hour from the create feed); flag creators above `factory_deploy_rate` (config) as low-quality. Off by default. Verifier: simulated factory creator (5 mints/min) blocked; normal creator passes. |
| R4-P1-3 | **Insider-coordination supply gate** | 2602.13480 (MELT: 36.5% of supply held by coordinated accounts via bundle traces) | `max_top10_concentration` measures raw top-10, not coordination | Add co-spend clustering over the first-block buyer set (union-find on shared funded-by / co-fire), flag coins where coordinated share > `max_coordinated_supply_share`. Off by default. Verifier: synthetic coordinated cluster flagged; independent buyers pass. |
| R4-P1-4 | **Graduation-probability entry timing** | 2602.14860 (P(graduate | curve SOL + structural/behavioral features) conditions the launch outcome) | `min_curve_progress_sol` static 0.25 | Fit/encode the graduation curve from our own replay archives (`state/replay_cache`, tools/replay_backtester): entry sweet-spot MC band + progress-rate bands. Expose `graduation_score_*` knobs (off = current behavior). Verifier: replay mints in sweet spot pass, outside-band rejects. |
| R4-P1-5 | **Hour-aware entry gating** | 2606.08232 (15-day Solana DEX deployment: worst entry hours mean −11.60% vs others; filter stack net-positive; 40.5% WR, +0.62%/trade) | No time-of-day logic | `filters.blocked_entry_hours_utc: []` — when non-empty, skip entries in those UTC hours. Verifier: entry attempted at blocked hour rejected; at open hour passes. |
| R4-P1-6 | **Wash-trade / LPI manipulation detector** | 2507.01963 (82.8% of >100% tokens show artificial growth; LPI = small buys → dramatic price jumps) | None | Detect same-wallet round trips and small-buy-large-jump patterns in the pre-entry window; flag mint as manipulated (`lpi_flag`). Off by default. Verifier: synthetic wash pattern flags. |

### R4-P2 — Copy-trader hardening

| ID | Item | Evidence | Current | Action |
|----|------|----------|---------|--------|
| R4-P2-1 | **Leader cohort vetting (persistent-ring detection)** | 2607.02795 (1,012 persistent wallet rings co-fire as early buyers; union-find co-occurrence on first-buyer windows — method sound; the paper's numerical results are withdrawn pending correction) | Copy targets are a static 5-wallet DB | Run the union-find first-buyer co-occurrence over the copy feed's own observations; when a leader's buys co-fire with a persistent ring beyond `ring_cofire_threshold`, mark that leader `ring_vetted=false` and stop mirroring. Off by default. Verifier: synthetic ring co-fire marks leader; independent leader unaffected. |
| R4-P2-2 | **Anti-front-run mirror jitter + concealment** | 2601.08641 (adversaries front-run copiers, conceal positions, fabricate sentiment) | `min_buy_interval` only rate-guards | Add `mirror_jitter_ms` (random delay range) + optional `mirror_split_quanta` (split mirrored size into 2-3 fills so a leader's followers can't front-run the full amount). Off by default. Verifier: jitter applied, split preserves total size. |

### R4-P3 — Exit / sizing

| ID | Item | Evidence | Current | Action |
|----|------|----------|---------|--------|
| R4-P3-1 | **Fragility-scaled position sizing** | 2512.00377 (ME2F: volatility dynamics + whale dominance + sentiment amplification → fragility not evenly distributed) | Fixed 0.01 SOL per trade; `max_top10_concentration` is a pass/fail gate | Compute a fragility index per candidate (whale dominance + volatility persistence from replay); scale `buy_amount` down when fragile (config `fragility_scale_min`/`max`, off = 1.0 flat). Verifier: fragile synthetic coin sizes down, stable coin sizes full. |
| R4-P3-2 | **Graduation-label validation before using it as an exit signal** | 2607.02823 (collector-generated graduation labels carry measurement error; logistic model fails temporal generalization) | `pumpswap_migrate_watcher` treats the migrate event as authoritative | Before any graduation-based exit/leader logic uses the label, confirm the migrate event against a direct pool-state read (the watcher's own curve/pool check); log `grad_label_verified` in shadow. Verifier: verified label passes, unverifiable label skipped. |

### R4-P4 — Research methodology (shadow-data quality)

| ID | Item | Evidence | Current | Action |
|----|------|----------|---------|--------|
| R4-P4-1 | **Rejection-stream coverage metric** | 2609.18975 (two observation pipelines see different mint sets; Jaccard/Dice quantify coverage) | Shadow capture logs every detect + skip reason, but nothing measures set overlap | Compute nightly `detected_vs_rejected` Jaccard/Dice over the shadow ledger (mints seen by the snipe path vs mints seen by copy/monitor paths) so filter-stack blind spots are visible. Verifier: synthetic two-set ledger yields expected Jaccard. |
| R4-P4-2 | **Cross-pipeline graduation evidence from replay** | 2512.11850 (<2% of pump.fun tokens graduate; graduation is the rare event that matters) | Replay backtester exists; graduation edge (MC $100-200 at grad, 50% >2x) in memory notes | Add a replay report counting graduates vs total and the graduation-probability-by-entry-band table (feeds R4-P1-4). Verifier: report rows parse from replay cache. |

### Round-4 RISKS — explicitly NOT to copy (evidence)

- **2405.15288 (EVM inscription surge)** — Ethereum/rollup gas dynamics; Solana is a fee-per-CU venue and the inscription result does not transfer. Nothing to adopt.
- **2412.04913 (Coin-Meme cultural clustering) / 2412.07591 (CoinCLIP/CoinVibe)** — CLIP/vision + comment-likes viability models require a social-data ingestion pipeline (comments, images) the bot does not collect; the marginal signal does not justify the infra. Note only; revisit if a social feed is ever added.
- **2410.23794 (Zerebro AI-meme hyperstition)** — narrative analysis of AI-agent coins; our `allow_mayhem_mode` already treats AI mints as a distinct class. No code adoption.
- **2601.08641's full multi-agent LLM + CoT stack** — a multi-modal LLM on the copy hot path adds latency and cost; only its defensive mechanisms (front-run jitter, position concealment, ring detection) transfer, as R4-P2.
- **2608.20271's full ML pipeline (6.4M-token training)** — we do not train a model in-repo; R4-P1-1 encodes the *features*, calibrated against our replay archives.
- **2607.02795's numerical claims** — the Oct 2026 correction states buyer records include sells and miss many buys; all effect sizes are withdrawn. Only the union-find *method* is used, on our own feed data.
- **2607.02823's generalization claim** — the paper's own temporal holdout fails; treat any graduation label as noisy until R4-P3-2 verifies it.

### Round-4 Addendum — operational research (beyond the 19 papers)

> Extension pass (Oct 2026): full-text of the 8 most actionable papers plus
> field research — Kaggle reverse-engineering of the largest live pump.fun
> sniper, priority-fee vs Jito operations guides, a 53-position paper-trade
> exit cohort, and copy-leader selection data. Two Round-4 items above are
> corrected below with the primary-text numbers. Items are numbered
> continuing from the R4 tables.

| ID | Item | Evidence | Action |
|----|------|----------|--------|
| **R4-CORR-1** | Hour-aware gating is **directional only** — do NOT gate on it yet | 2606.08232 primary text: worst hours (2, 13, 23 UTC; n=17, mean −11.60%) vs others (+1.82%) give Mann-Whitney U p=0.5634, rank-biserial r=0.085 — non-confirmatory. The paper itself flags small-sample stats as fragile (excess kurtosis 6.61) | Downgrade R4-P1-5: keep entry-hour **logging** (hour field in shadow ledger) but ship NO hour gate until our own ledger shows a persistent effect. This is a research item, not a strategy item. |
| **R4-CORR-2** | Persistent-ring numbers are withdrawn; method only | 2607.02795 correction: 43% of "buy" labels were sells; records missed 77% of buys; the +16.1% lift and 1,012 cohorts are withdrawn | R4-P2-1 stays as written (union-find on OUR OWN feed, where buy/sell labels come from our parser, not a third-party collector). Add a feed-quality check: monthly label audit on our shadow ledger. |
| R4-P0-3 | **Contention-mode fee racing (dynamic on-competition bump)** | tierzero priority-fees-vs-Jito: plain fee landing rate collapses to 20–40% under launch competition even at "generous" fees; Kaggle sniper benchmark: median 0.05 SOL (p90 0.37) per entry, 21% of gross spent on fees, still nets +39,545 SOL because 8.5:1 win/loss asymmetry | Add `priority_fees.contention_mode`: when the hot path detects launch competition (create-feed flood or `getRecentPrioritizationFees` p90 rising), bump `bid_multiplier` toward a configurable `contention_multiplier` (e.g. 2–3×), capped at `hard_cap` AND at `max_fee_fraction_of_position` (default e.g. 0.5%) so fees can never eat a 0.01-SOL test position. Off by default = current static/dynamic behavior. Verifier: contention flag raises the bid, cap holds. |
| R4-P0-4 | **Landed-slot KPI (measure the race, don't guess it)** | Kaggle reverse-engineering: top bot lands 79.6% of first buys in the deploy slot, 98.5% within 2 slots, median position +118 tx after create; pays for placement, not collusion | Record per shadow fill: `landed_slot_offset` (buy tx slot − create slot) + `entry_tx_idx_in_block`. Daily summary reports median offset vs the 0–2 benchmark so we know whether our current fee is winning the race or silently drifting. Verifier: shadow row carries both fields. |
| R4-P1-7 | **Staged-then-trail exit mode (asymmetric exits)** | 53-position paper cohort: fixed TP +11.2% avg vs **momentum-fade trailing +52.7% avg** (+24.6% median); hard SL −40.1%; combined rule "take 40% at +25%, arm break-even floor at +10%, trail the rest" | Add `trade.exit_mode: "tp_sl" | "staged_trail"` — staged_trail layers the existing R3-P1-2 staged TP/SL with a trailing floor: sell fraction at +25%, then arm a break-even floor at +10% unrealized, trail N% behind each new high, exit on floor touch or rug signal. Disabled-first = current pure TP/SL. Backtest both modes on replay archives before arming (in the spirit of the paper's own cohorts). Verifier: synthetic ladder arms floor after +10%, trails highs, exits on floor. |
| R4-P1-8 | **Volume/momentum-fade exit signal (log-only first)** | 53-trade cohort: volume −50% from peak while price near top = distribution, exit is the norm; median hold <15 min, 90% resolved <3 h; top sniper: median hold **6 s**, 4.4 sells/token, 96.6% partial-exit ladders | Track trade-rate (fills/min from our own trade feed) per open position; when rate drops >50% from its peak without a new high, log `momentum_fade` hint into shadow (advisory field the exit monitor can read later). No behavior change while off. Verifier: synthetic volume profile yields the flag. |
| R4-P1-9 | **Stagnation exit (opt-in)** | 53-trade cohort: flat 30–60 min = hype window passed; "flat can be a signal rather than a waiting room" | `trade.stagnation_exit_min: 0` + `stagnation_band_pct` (e.g. 2%): if price stays within band of entry after N minutes, exit. Off by default (0). Verifier: flat position exits at N min, moving position holds. |
| R4-P2-3 | **Copy-leader quality score (replace static watch with scored watch)** | Dune leaderboard v2: alpha_score composite, `pnl_realization_ratio ≥ 0.5` required, low pnl concentration preferred; medium (Nathan Baldwin): 472 tokens/week is a bot, human traders are picky; 2601.08641: KOL = consistent profitability + **low-frequency trading**, 70–73% precision | Extend `state/copy_targets.db` refresh tool with a scoring pass: per candidate compute 30d realized PnL, trade frequency (reject >N tokens/week bots), realization ratio, hold-time median; maintain a `quality_score` column; `copy_trader.min_leader_score` gate (off by default = current 5 static wallets unchanged). Verifier: scoring function ranks a synthetic good/bot/wash candidate correctly. |
| R4-P4-3 | **RPC latency self-test at startup** | FRB latency study: HTTP RPC 50–150 ms, Yellowstone gRPC co-located 5–15 ms, geyser push ~300 ms ahead of polling; latency is the #1 sniper variable | `node.startup_latency_test: false` — when on, ping configured RPC + WSS at boot (getSlot round-trip ×3), log tiers, warn if >200 ms; recommend lower-latency endpoints in the log. Off by default. Verifier: fake RPC with injected latency produces the expected warning. |

### Round-4 Addendum — RISKS (operational claims checked)

- **"Jito shredstream is 100–150 ms faster than gRPC" (crypdev007 README)** — upstream disabled ShredStream 2026-09-05 (verified in Round-3 do-NOT-copy list); the claim is marketing on top of an already-dead service. NOT adoptable regardless of speed.
- **Copy the top sniper's exact fee numbers (0.05 SOL median/fill)** — that bot trades ~9.34 SOL mean positions and spends 21% of gross on fees with an 8.5:1 asymmetry. At our 0.01 SOL test size the same fee profile would be 5× the position. Adopt the *mechanism* (contention racing, landed-slot KPI), never the absolute numbers; scale fees as a fraction of position (0.5–1% per tierzero guidance).
- **Exit cohort figures are paper-trade gross** (53-trade, one strategy, Jun 25–Aug 2 window) — directional evidence for R4-P1-7/8/9, not expected net returns; every mode ships off and is backtested on our own replay archives before arming.

### Verification protocol (updated count)
1. `uv run tests/regression/run_all.py` → 77/77 today; each R4 item adds a verifier; target stays green before any merge.
2. Config dry-run (`load_bot_config` + `resolve_env_vars` + `validate_config`) every change.
3. Live test only with explicit user approval + stated expected cost + burner wallet + `enabled: true` for one session, then back to `enabled: false`.
4. Shadow mode stays ON for all of R4 implementation; real-mode-only items (per-path CU, DontFront, ALTs, durable nonce) ship disabled and are only ever enabled by explicit user approval.
5. Commit + push per item; never push secrets (`.env` ignored, `state/` ignored, `shadow_data/` ignored).
6. Every scored gate (R4-P1-1/3/4, R4-P3-1) is calibrated on replay archives FIRST; thresholds ship conservative and are only tightened by evidence.