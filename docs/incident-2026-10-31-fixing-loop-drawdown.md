# Incident 2026-10-31 — Fixing-loop drawdown (portfolio 19→12 USD, ~40% SOL lost)

**Date:** 2026-10-30 22:14 → 2026-10-31 00:52 CST (fixing-loop live windows)
**Bot:** Memesnipe (`bots/memesnipe.yaml`), pump.fun live mode, `shadow_mode: false`
**Severity:** High (real funds; portfolio drew down while the $10 loss cut stayed silent)
**Commits in play:** `a930826` (P0 remediation + live config, R5-P0-1), `ef9474e` (R5-P0-2 deadlock/phantom-PnL/churn), `9784510` (R5-P0-2 quiet + dust parking), `b458d70` (R5-P0-3 unrecovered-spend loss cut)
**Incident #1 reference:** [docs/incident-2026-10-30-live-loss.md](incident-2026-10-30-live-loss.md)

---

## TL;DR

During the first live fixing-loop windows the wallet went from **≈0.195 SOL free (~$19)** down to **0.029758 SOL free (~$12 portfolio, user-observed)** — verified on-chain via two independent RPC providers. The killer was the **same blind spot as Incident #1, one level deeper**: the $10 loss cut gates on *realized* PnL, which stayed **+0.0038** while **0.11 SOL** went out to 11 buys whose tokens dumped and stayed **held** — unrecovered spend was never measured, so the guard never fired. Second-order defects amplified it: a **phantom +0.49 SOL "win"** fabricated by zero-entry-price math, an **open-position double-accumulation** that deadlocked buys (32 vs cap 3), **25 wallet positions vs the 3 the tracker reported**, and a dead RPC endpoint in `.env`.

---

## 1. Ledger reconciliation (verified, not estimated)

| Item | SOL | Evidence |
|---|---|---|
| Wallet free at fixing-loop start | ≈+0.195 | measured pre-window |
| 11 buys executed (0.01 quote each) | **−0.11** | `session_spend_sol` + "Buying" lines in log |
| 8 successful exits (2 SL fills) | small | `Final PnL -21.53%` and `-40.62%` rows |
| Realized PnL (tracker) | +0.0038 | risk ledger — **the misleading number** |
| Phantom +0.49 "win" (zero-entry bug) | +0.4914 | fabricated by `(sold − 0) × qty` on rehydrated orphans; reset to 0 |
| **Wallet free at freeze (on-chain)** | **0.029758** | verified via `api.mainnet-beta.solana.com` AND `solana-rpc.publicnode.com` |
| Nonempty token accounts at freeze | **25** | bot's own scanner; e.g. raw 346,682,685,388 units ≈ 346,682 tokens |

Key arithmetic: 0.195 − 0.11 (buys) − ~0.045 (unrecovered in held/dumped tokens) ≈ 0.03 free. The **≈0.11–0.15 SOL of damage is locked in 25 held positions** — most on closed/graduated curves (un-sellable via pump.fun), a few possibly still on-curve — plus SL-fill slippage and fees.

---

## 2. Where the money actually is (at freeze)

- **0.0298 SOL ($3–5)** — wallet free.
- **~0.08–0.11 SOL** — in **25 held token positions** (bought at 0.01 each, dumped while held; most un-sellable on closed curves).
- **+0.0038 SOL** — realized net (the only number the old loss cut could see).
- **−0.0023 to −0.0080 SOL** — observed SL fills (see RC-5, overshoot).
- **~0.005 SOL** — fees + failed-tx base fees across the windows.

---

## 3. Root causes (evidence-backed)

### RC-1: Loss cut blind to unrecovered spend — the real killer (fixed in `b458d70`)
`_block_reason()` checked `realized_pnl_sol <= -limit` only. While 0.11 SOL was committed to positions that dumped and **stayed held**, realized stayed **+0.0038** (small winners booked, losers not yet sold) → the cut never fired and the bot kept buying. This is Incident #1's "exposure is not realized loss" lesson at the *position* level: **money out vs money back must be measured, not just booked wins/losses.**
**Fix:** `gross_recovered_sol` + `record_recovery()` (gross proceeds on every close, WSOL only) and a new block: `session_spend_sol − gross_recovered_sol ≥ daily_loss_limit_sol` ⇒ block new buys. Wired into all four sell paths (confirmed fill, LATE_SUCCESS, staged partial, time-based).

### RC-2: Two opposing accounting bugs around open positions (fixed in `ef9474e`)
- **Double-accumulation deadlock:** persisted `open_positions` restored from disk *plus* rehydrate adding recovered inventory each boot → `open_positions=32` vs cap 3 → every buy blocked (the screenshot the user reported). Reverse of RC-1's problem: too *trigger-happy* guard.
- **Under-counted exposure:** rehydrated/swept liquidation-queue rows never took concurrency slots, so the wallet held **25 positions while the tracker said 3**. The "max 3 concurrent" mental model was false — portfolio exposure was far larger.
**Fix:** boot re-derives `open_positions` from actual held inventory (rehydrate/sweep), never restores it from disk; liquidation-queue closes bank PnL without releasing live slots (`record_liquidation_close`); live handoffs keep their slot.

### RC-3: Phantom +0.49 SOL "win" from zero entry price (fixed in `ef9474e`)
Rehydrated orphans carry `entry_price=0`; `(sold − 0) × qty` fabricated **+0.4914 SOL of realized "profit"**, giving the loss limit **+0.49 SOL of false headroom** — the opposite dangerous direction from RC-1. **Fix:** `_realized_pnl_for_close()` degrades zero-entry tails to 0.0 at all three close sites; banked-leg PnL is preserved.

### RC-4: Registry contamination (fixed in `ef9474e`/`9784510`)
22 unsold-registry rows, all `entry_price=0` (unknowable basis), **2 rows with `quantity=0.0`** (garbage — must never exist), all written in one sweeper batch at restart. This is what made the wallet *look* like 25 tracked positions. **Fix:** registry rows are truth-checked; parking + re-find loop ended via `_unsellable_parked`.

### RC-5: SL fills overshoot the floor (still open)
Observed fills: **−21.53%** (−0.003425, incl. −0.001713 banked legs) and **−40.62%** (−0.008024, incl. −0.004012 banked legs) vs a −20% stop floor. Same tick-gap slippage as Incident #1 RC-4 (fills at current tick on thin liquidity). **Not fixed yet.**

### RC-6: Entry quality — shotgun on a dying base rate (still open)
11 buys in ~2.5h at 0.01 SOL each, most dumped. Measured ledger profile: **10 real losses vs 4 real wins; avg loss ≈ −23% of position, avg real win ≈ +8–15%.** With winners cut early (staged 40% @ +20% then trail), the payout ratio is ≈1:1 against a ~30% win rate — structural negative expectancy. **Filters are blocking 274–3,309 events per window, but the surviving entries still carry the base rate.**

### RC-7: Dead RPC endpoint in `.env` (still open)
`SOLANA_NODE_RPC_ENDPOINT=solana-rpc.publicnode.com` returns **403 Forbidden** on `getTokenAccountsByOwner` (verified). The bot's YAML config uses `api.mainnet-beta.solana.com` (works), so trading was unaffected — but any tooling honoring `.env` (probes, and any future re-config) hits a dead endpoint.

---

## 4. What was applied already

- `ef9474e` (R5-P0-2): boot re-derivation of `open_positions`; liquidation-queue slot semantics; phantom-PnL degradation; `_unreadable_curves` negative cache (closed curves re-read ≤1/300s/mint, was every 15s tick).
- `9784510` (R5-P0-2): `curve_manager.get_pool_state` logs expected closed-curve failures as one WARNING line instead of full tracebacks (was ~90–120 lines/sweep); watcher TTL parks unsellable dust (`_unsellable_parked`, 1h lazy re-check).
- `b458d70` (R5-P0-3): net-unrecovered-spend loss cut + `record_recovery` on all sell paths; regression suite **113/113**.
- Persisted `risk_state.json` backfilled with `gross_recovered_sol: 0` → the guard now **blocks all new buys** (0.11 − 0 ≥ 0.098). Sells still run. **The bot cannot lose more of the remaining $3–5 by itself.**
- Fleet **stopped** for evidence freeze.

---

## 5. Outstanding / next actions

1. **Decide on the 25 held positions** — audit which are still on-curve (sellable) vs closed/graduated dust (only recoverable via manual/Jupiter path, not enabled). Reclaim where possible.
2. **Wallet refill** before the next live window — guard will block until recovered money crosses the cut or the session baseline is consciously reset.
3. **RC-6: entry-quality tuning** (user's stated goal: "fewer shots, ride runners") — raise `min_curve_progress_sol`/`consecutive_match_count`, add volume/holder minimums, loosen the early TP cut on the staged trail so winners can pay for several losers.
4. **RC-5: stop-fill slippage** — investigate fill-at-current-tick overshoot (confirmed twice this window).
5. **RC-7: `.env` RPC** — point `SOLANA_NODE_RPC_ENDPOINT` at a working endpoint or align it with the YAML config.
6. Confirm the guard's behavior on restart: `gross_recovered_sol` restores, `open_positions` re-derives to actual inventory, no double-count.

---

## 6. User-visible timeline (fixing loop era)

- 22:14 — fleet armed (post-`a930826`); live gates verified blocking (curve-progress, market-cap, Mayhem, quote-mint, risk cap).
- Live — "risk guard blocked" on **every** token: `open_positions=32` deadlock diagnosed → `ef9474e`.
- Restart — `open_positions=3`, buys resume; tracebacks ~100/sweep → `9784510`.
- 00:52+ — user wallet check: **>3 open positions, USD 19→12**. On-chain audit: **25 positions held, 0.0298 SOL free**. `b458d70` deployed; fleet stopped; guard now blocks buys.