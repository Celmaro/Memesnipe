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