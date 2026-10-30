# Incident 2026-10-30 — Live loss investigation (~$10 perceived loss)

**Date:** 2026-10-30 (trading window 02:46→06:33 CST = 18:46→22:33 UTC Oct 29)
**Bot:** Memesnipe (`bots/memesnipe.yaml`), pump.fun live mode
**Severity:** High (real funds, uncapped exposure)
**Commits in play:** `93f3da0` (live mode + $10 cut + capture/ledger decoupling), `f0378d5` (grad_timing import fix), `7875e48` (sweeper capture + risk caps), `f751dbf` (lint)

---

## TL;DR

The wallet went from **0.31878 SOL free → 0.007115 SOL free** during a 3h47m live window. Forensic reconcile against on-chain truth shows **no mystery loss**: 31 buys deployed 0.3567 SOL (0.31 quote + **0.04693 ATA rent** + fees), 8 sell legs returned 0.0449 SOL, fees were <$0.25 total. The "lost $10" was **uncapped concurrent exposure** (97% of the wallet deployed into 31 dead-floor positions at ~1 buy/2min), not runaway fees. The $10 realized-loss cut never tripped because it only gates on *realized* PnL (−$0.80) while the bleed was exposure + unrealized.

---

## 1. Ledger reconciliation (on-chain verified)

| Item | SOL | $ (≈$102/SOL) | Evidence |
|---|---|---|---|
| Wallet at window start | +0.31878 | +$32.50 | measured pre-live |
| 31 buys on-chain (incl. rent+fees) | **−0.35673** | −$36.39 | 39 txs fetched with full meta |
| 8 sell legs returned | +0.04492 | +$4.58 | on-chain deltas + token deltas |
| Journaled-tx fees (39) | −0.00088 | −$0.09 | tx fee field |
| Failed-tx fees (57 confirmed-but-failed) | ≈−0.0012 | −$0.12 | 57 sigs × base fee |
| **Expected free** | **≈0.005–0.0071** | | |
| **Measured free at freeze** | **0.007115** | ✔ | raw JSON-RPC balance scan |

Key arithmetic: each buy cost **0.01151 SOL avg** = 0.01 quote + **0.00151384 ATA rent** + fee. 31 × 0.00151384 = **0.04693 SOL locked as rent** in 31 Token-2022 ATAs — **recoverable by closing accounts**, not burned. The earlier "~0.047 unrecorded" alarm was a mis-assumption that buys cost exactly 0.01; on-chain data closed the gap.

---

## 2. Where the money actually is (at freeze)

- **0.047 SOL ($4.79)** — rent locked in 31 ATAs (`close_ata_on_sell` off by default). Reclaimable.
- **~0.25 SOL** — tokens still held in **29 nonempty token accounts** (27 open-ish positions at last marks, mostly −2% to −12%).
- **−0.0079 SOL (−$0.80)** — realized losses (6 closes, 4 stop-outs).
- **−0.0124 SOL (−$1.27)** — unrealized at freeze.
- **<0.002 SOL** — all fees, journaled + failed txs.

---

## 3. Root causes (evidence-backed, no assumptions)

### RC-1: Uncapped exposure — the real killer
`max_concurrent_positions: 0` (unlimited) + `budget_sol: 0` (off) let the bot deploy 97% of the wallet into 31 concurrent positions in 3h47m. The $10 cut (`daily_loss_limit_sol: 0.098`) **never tripped** — it gates on realized PnL only (−$0.80 vs −0.098 floor). Exposure is not a realized loss, so the guard can't catch it. **Fix:** caps 10 concurrent / 0.15 SOL now in place (`7875e48`).

### RC-2: Free RPC 429 storm all window
36,864 geyser errors per log. Consequences, all verified in code + logs:
- `getRecentPrioritizationFees` returns `None` under load → **every journal row: `paid_fee_sol: 0.0, fee_source: "unknown"`** (fee capture silently zeroed → recorded P&L unverifiable; on-chain reconcile was the only ground truth).
- Buy refreshes starved → 15 buys built on stale curve PDAs reverted **`Custom 2006` (ConstraintSeeds)** — AGENTS.md's documented listener-guessed-default failure mode. 42 later failures `Custom 1` (drained-wallet era).
- No failover endpoint in the free-tier path.

### RC-3: Sweeper crash stranded value
`bytes.get` crash (base64-encoded token data) → orphan scan failed **366 times during the window** → POORGOAT's 60% remainder (201,662 tokens) sat unsold; rent never reclaimed. **Fix:** `_token_accounts_raw` jsonParsed + Token-2022 coverage (`7875e48`, verified live: 29 orphans found).

### RC-4: Stop-fill slippage
Stops are nominal −18% but fill at current tick: JEF −22.5%, PSA −22.2%, Tigger −26.7%, MIAOU −41.8% (avg −28.3%). Tick-gap slippage on dead pools (JEF exit quote liquidity: 17 lamports). ~0.004 SOL dead weight on 6 closes.

### RC-5: Entry quality — all floor coins
31/31 entries were mc_band `floor_lt_3k` (16) / `3k_5k` (14) / `5k_10k` (1), avg entry MC ≈ $3.5k, all at the curve floor. No runner distribution; most decayed −2% to −12% and sat (no max_hold_time, stagnation exit 0).

---

## 4. On-chain state of entries / exits / after-exit (user asked explicitly)

- **Entry:** every buy's token delta matches the journaled `qopen` exactly (on-chain `post_tokens` == journal quantity). Quote math: 0.01 + 0.001514 rent.
- **Exits:** PSA/Tigger/MIAOU drained to 0 tokens; JEF left 1e-6 dust; BENJO left 2e-6 dust; POORGOAT left **201,662 tokens (60% remainder, stranded)** — all verified via pre/post token balances.
- **After exit:** 29 nonempty accounts persist at freeze; value mostly dead-floor paper; rent locked.
- **Sell failures post-restart:** `Custom: 1` — pump.fun IDL codes start at 6000 so 1/2006 are not pump.fun's own; 2006 = Anchor `ConstraintSeeds` (stale curve PDA, documented in AGENTS.md); `Custom 1` consistent with insufficient funds / Anchor discriminant — flagged, not fully decoded, do not assume.

---

## 5. What was applied already

- `7875e48`: sweeper jsonParsed rewrite (SPL + Token-2022, retries), staged-partial `pnl_pct` percent fix, caps `max_concurrent_positions: 10` / `budget_sol: 0.15`, regression verifier 107/107.
- `f751dbf`: verifier import lint cleanup.
- Fleet stopped for evidence freeze; wallet at 0.007115 SOL — **new buys will fail until refilled**.

## 6. Outstanding / next actions

- Restart fleet for recovery sells + ATA close to unlock ~0.047 SOL rent (user decision pending).
- Consider `close_ata_on_sell: true` or a rent-reclaim sweep as a standing practice.
- RPC failover/provider strategy (Tatum credits exhausted; mainnet-beta 429 storms; keep 2rps/burst1).
- Realized-loss guard only bounds realized PnL — exposure caps are the real protection; document this in config comments.

---

## 7. Lessons learned

1. **Exposure caps are the risk guard; realized-loss limits are not.** A daily-loss cut on realized PnL cannot stop a wallet drain caused by unlimited concurrent deployment. Always run with `max_concurrent_positions` + `budget_sol` capped from the first live tick.
2. **Every buy costs quote + ~0.001514 ATA rent + fee — not quote.** 31 positions = 0.047 SOL silently locked. Account for rent in P&L math and reclaim it (close ATAs on sell).
3. **Free-tier RPC under load silently zeroes fee capture** (`paid_fee_sol: 0.0, fee_source: unknown`) and starves buy refreshes (stale-curve `ConstraintSeeds` reverts). When the journal says fees are 0, reconcile against on-chain tx metas before believing P&L.
4. **On-chain truth over journal truth.** Balance scans + `getTransaction` token pre/post deltas closed a "mystery gap" that was actually an accounting assumption. Always reconcile with the chain when real money is involved.
5. **The sweeper is a safety net; a crashing sweeper strands capital.** The `bytes.get` crash hid 366 failures behind "wallet scan failed" — grep tracebacks before blaming the endpoint (mirrors the earlier `grad_timing_assess` lesson: fail-closed bugs mask as RPC errors).
6. **Stop floors are nominal, fills are real.** Tick-gap slippage overshoots stops by 4–8+ points on dead pools; size stops assuming the worse fill.
7. **All-floor entries = no runners.** If every entry is `floor_lt_3k`/`3k_5k` at curve floor, the strategy is buying decay, not momentum. Validate entry bands before blaming exits.
8. **`pnl_pct` units matter — percent in journal, fraction in ledger.** A viewer multiplying by 100 again creates fake alarms. Document units at every capture site.
9. **Freeze evidence before investigating.** Stopping the fleet prevented further trades and preserved the exact wallet state — the investigation ran read-only against RPC.

---

## 8. Forensic method (reusable)

1. Freeze: stop service, confirm inactive, record wallet balance + token-account scan (raw JSON-RPC, both SPL + Token-2022, `jsonParsed`).
2. Fetch journal opens/closes; extract tx signatures.
3. `getSignaturesForAddress` (paginate) + `getTransaction` per sig — fee, pre/post SOL balances, pre/post token balances, errors. Multiple endpoints + backoff when storming; never trust a single failed fetch.
4. Join: journal qty vs on-chain token deltas; journal fee fields vs tx `fee`; expected free SOL vs measured.
5. Decode errors against AGENTS.md + IDL (pump.fun codes start 6000; 2006 = ConstraintSeeds).
6. Only then write conclusions — every line evidence-tagged.