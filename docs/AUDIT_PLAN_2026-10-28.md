# Audit Verification & Implementation Plan — 2026-10-28

Audit target: external 437-line audit of Memesnipe (pinned at `73c3740`).
Verified against: **HEAD `3851377`** (working tree clean) — grid prune `0f5df0d` and
RPC plumbing `dcbe96e` intervene, so several audit claims were re-judged against the
current tree. Every claim below was checked with file:line evidence (read-only),
not from the audit's own text.

## 1. Audit claim scoreboard (39 claims)

| Verdict         | Count | Claims |
|-----------------|-------|--------|
| confirmed       | 27    | C2.1 C2.2 C2.3 C3.2 C3.3 C4.2 C4.4 C4.5 C4.6 C4.7 C4.8 C5.1 C5.2 C5.5 C6.1 C7.2 C7.3 C8.1 C8.2 C8.3 C8.5 C9.1 C10.2 C11.1 C12.1 C12.2 C12.3 |
| partially-confirmed | 7 | C2.5 C4.1 C4.3 C5.3 C5.4 C7.1 C10.1 |
| latent (real but currently neutralized) | 3 | C2.4 (jito off) C3.1 (copy off) C3.4 (shadow mode) |
| by-design         | 1    | C3.5 (shreds listener docs accurate; geyser active) |
| false / stale at HEAD | 1 | C6.2 (identifier change — none exists; live is mint-keyed throughout) |

Net: the audit's findings largely **hold**. Exact stale/incorrect sub-claims to ignore:
- C5.3 "sells whole wallet": false at HEAD — `PlatformAwareSeller.execute` now
  *requires* `token_amount`/`token_price`, so the mirror-sell call raises a TypeError
  and leaves the position open (still a bug — silent no-op — but not a whole-wallet sale).
- C5.4 "three sequential GMGN fetches": false at HEAD — `asyncio.gather` + cached
  multiwindow profile.
- C4.1 blanket "flag not used": partial — the curve gate *does* fail closed on its
  RPC-error branch (universal_trader.py:2390-2392); all other missing-data gates don't.
- C2.5 "bid_multiplier applies to fixed fallback": false — it only applies inside the
  dynamic path (priority_fee/manager.py:98-117).
- C7.1 "trail10 label configured with 8% trail": resolved by the 4-cell prune
  (`st_trail10_sl25` is 10% and matches `trail10_trail_pct`).

## 2. Severity framing

The single most important insight from verification: **the replay tool that produced the
loop-5 evidence base has three entry-semantics defects (C8.1) and the research metrics
count every staged partial leg as a full close (C7.3/C8.3/C4.8)**. Every tuning result
presented so far — including loop-5 — is contaminated by partial-leg inflation and by a
60-second token-age gate (not the intended 1s) plus MC-as-curve-progress. Fix accounting
and replay first; only then re-run sweeps.

## 3. Implementation plan (priority-ordered)

### P0 — evidence integrity & active-path safety (do before any further tuning)
1. **C8.1 Fix replay backtester entry semantics** (`tools/replay_backtester.py`)
   - `:164` `age <= TOKEN_MAX_AGE_S * 60` → use `TOKEN_MAX_AGE_S` directly (intended 1s gate).
   - `:181` `curve_progress = tok["mc_sol"]` → derive from real curve/reserve fields.
   - `:455` thread `args.max_open` into the backtester instead of global `MAX_OPEN_POSITIONS`.
   - Re-run the four-cell sweep after the fix; treat *all prior* headline numbers as invalid for selection.
2. **C7.3/C8.3 Partial-leg accounting** — tag partial `sim_close` rows (`is_partial: true`, `leg_index`, `position_open_id`); make `net_metrics.compute_metrics` aggregate legs into one
   position (closed only on final leg), fix `sweep_zombies` net-count, and fix
   `tools/rug_memory.py` ingest to record one creator trade per position.
3. **C3.3 Bounded token queue** — replace unbounded `asyncio.Queue`; evict/cancel enqueued
   items, dedupe at *enqueue* time, and decide the `max_token_age: 0` contract (recommend: 0 = enforce safety default, not disable).
4. **C3.4 Duplicate-buy race** — reserve mint in `_active_exit_mints` (or a lock set)
   *before* buyer execution; release on failure/position setup.
5. **C7.2 Durable unsold-position registry** — persist `_unsold_positions` to
   `state/` JSON, reclaim on boot; keep the 15s/600s watcher but don't lose state on restart.

### P1 — correctness & config plumbing
6. **C5.1** Normalize profiler period keys (`1d/7d/30d`) to int days before `TradeSummary`;
   fail closed on qualification errors instead of swallowing.
7. **C5.2** Honor `CopyTarget.mode` (buy_only blocks sells), `require_confirmation`,
   `priority_fee` in the mirror path; apply `max_position_sol` cap to fixed sizing.
8. **C5.5 + C5.3** Explicit copy position IDs; mirror sim_close with real
   `amount_tokens`/`pnl_quote`/`hold_seconds`; persist the mirror ledger; pass validated
   `token_amount`/price to the seller (fix the current TypeError no-op).
9. **C8.2** Extend `StrategiesLedger.close_position`: blend each strategy's own
   first-exit + staged-leg path into per-strategy net PnL instead of the single live-close
   repricing; keep live close as the would-flag reference only.
10. **C8.5** Move shadow/ledger JSONL appends off the event loop (background writer queue /
    buffered handle) — shadow runs are the live path.
11. **C2.2** Enforce `lastValidBlockHeight` (or fail closed) before signing with a
    refreshed fallback hash; never sign with an un-refreshable stale hash when a budget exists.
12. **C3.2** Geyser reconnect: resume from last *completed* slot or add overlap+dedupe.
13. **C12.2** Fix `backfill` location mismatch (move to top level or read `trade.backfill`);
    add config_loader schema check. **C12.1** delete dead `filters.risk` block.

### P2 — gate & screening hardening (latent today, required before real-mode)
14. **C4.1** Extend `fail_closed_gates` to all data-missing branches (curve pool/price None,
    holder no-snapshot, wash/cofire no-observations, metadata) or document each as by-design.
15. **C4.2** Add wash/cofire/rug-score thresholds to the trade-feed wiring condition in `start()`.
16. **C4.3** Move creator-cadence recording out of `_skip_reason` into one per-mint hook;
    extend the pre-buy recheck to rug-score/creator-PnL/factory gates.
17. **C4.4** Enforce max snapshot age at holder-gate read (module docstring already promises it).
18. **C4.5** Rename `max_coordinated_supply_share` → wallet-share semantics (or compute true
    supply share); fix the yaml comment units.
19. **C4.6** Wash detection: identify round-tripping wallets first, then sum quote across all
    their events (paper's strong test).
20. **C4.7** Rug score: require minimum feature coverage before scoring (refuse on too many
    unknowns) rather than unknown = benign 0.
21. **C4.8** Creator-PnL ledger: per-position aggregation (overlaps P0-2), parameterize cost
    model constants.
22. **C10.2** SSRF: allowlist metadata URI schemes/hosts (ipfs/arweave/https; block
    loopback/link-local/cloud-metadata), stream body with explicit cap.
23. **C2.4** If Jito is ever enabled: add `getBundleStatuses` polling (or document the
    accepted-bundle trust model). **C2.5** apply `bid_multiplier` on the fixed-fallback path
    or document the asymmetry.

### P3 — portability, monitoring, structure
24. **C10.1** Make state paths repo-relative/env-relative (2 of 3 are absolute
    `/home/agentuser/...`); optionally add per-position creator-PnL events for auditability.
25. **C11.1** Enable `startup_latency_test` at boot; optional `/metrics` HTTP exporter.
26. **C12.3** Incremental decomposition of `UniversalTrader` (4484 lines) into injected
    collaborators: exit engine, gate stack, shadow writer, copy engine.
27. **C6.1/C6.2** Decide migration continuity: teach replay/copy to route post-graduation
    through the PumpSwap/graduated adapter instead of force-closing at `migrate`.

### P4 — verification protocol for each change
29. Run full regression suite (currently 102 verifiers) after every P0/P1 landing.
30. Re-run the four-cell sweep only **after** P0-1/P0-2 land; keep hour-14 held-out split
    and ±2000% outlier band; require ≥100 closes/strategy before ranking.
31. Keep `shadow_mode: true` as the resting state; real-mode windows only on explicit user
    authorization, always returning to shadow.

## 4. Accepted by design (no action)
- **C3.5** Shreds listener is documented pre-execution; geyser is the active listener.
- **C7.1** `max_hold_time: null` + `stagnation_exit_min: 0` is a deliberate TP/SL-only
  policy; revisit only if dead-zone holds become a measured problem (track via sweet_zombies).