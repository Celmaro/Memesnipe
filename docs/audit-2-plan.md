# Implementation plan — audit #2 (latency + architecture audits of 5b92d6f)

Two audit docs (ed1e81ba / 3fd26e30) describe commit `5b92d6f` (pre-R5 remote
main). Every claim was verified against the repo at HEAD (a6983cc). VERDICT:
the audits are structurally accurate about WHICH files exist but wrong about
the config schema, the test count, and several "optimizations" that are either
already implemented, already bypassed, or would silently override live gates.
This plan implements only the VERIFIED gaps; everything else is NOT-to-copy.

## Verified-against-code findings (this pass)

| Audit claim | Code reality | Verdict |
|---|---|---|
| "78 regression tests" | 97 verifiers at 5b92d6f, 101 at HEAD | **WRONG count** |
| 4 listeners exist (logs/blocks/geyser/shreds) + latency tools | `listener_factory.py` + `compare_deshred_latency.py`, `compare_listeners.py`, `live_listener_matrix.py` all present | TRUE |
| slot_buffer delays Block-0 CreateEvent | Docstring: "The snipe hot path stays unordered (it wants the raw event ASAP); only the exit-monitor / copy-trader consumers use this buffer" | **ALREADY BYPASSED — audit item #2 stale** |
| Install uvloop for 30–50% lower latency | Already installed AND wired in `bot_runner.py` (uvloop + winloop fallback) | **ALREADY DONE** |
| All named verifiers exist | All 37 audit-named tests exist under `tests/regression/` | TRUE (names real) |
| Proposed `screening:` / `risk_guards:` / `copy_trading:` / `trade.staged_exits:` / `trailing_stop:` / `hot_path_zero_rpc:` / `in_stream_exits:` / `depth_floor_sol:` / `creator_sell_exit:` / `stagnation_exit_seconds:` sections/keys | **NONE exist** in `config_loader.py` or `bots/memesnipe.yaml` (grep: zero hits). Real sections: `filters:`, `trade:` (`exit_mode`, `trail_*`), `copy_trader:`, `risk:` | **SCHEMA MISMATCH — would silently no-op or break** |
| `max_real_reserves_sol: 2.5` anti-bundler ceiling | Key does not exist. Real anti-bundler = `extreme_fast_token_amount` floor (min 2_620_000) + buy_slippage. `verify_real_reserves_cap.py` is the buy-OUTPUT cap, not an anti-bundler ceiling | **INVENTED KEY** |
| Blockhash refresh every 2.0s | Code default + yaml both `5.0` (`blockhash_refresh_interval: 5.0`) | Recommend-only; **WRONG as current-state claim** |
| Stores need WAL + synchronous=NORMAL | Only `rug_memory.py` + `strategies_report.py` set WAL. `creator_pnl_store.py`, `copy_target_store.py`, `strategies_ledger.py` do NOT | **REAL GAP (P1)** |
| Disabled bot-sniper-*.yaml are inert | All 5 are `enabled: false` BUT `bot_runner.run_all_bots()` globs `bots/*.yaml`, loads + VALIDATES every file — the 9 startup `Traceback: ValueError extreme_fast_token_amount` lines in EVERY fresh log come from these | **REAL NOISE (P1)** |
| Copy-engine enabled / qualification thresholds | `copy_trader.enabled: false` (R5-P0-4, deliberate) | NOT-to-copy until lifecycle verified |
| Exit calibration Stage1 +50%/50%, Stage2 +120%, trail +35%/18% | Contradicts our realistic-fill sweep: 40% @ +20% / trail 8% won; live staged_trail-tuned | **NOT-to-copy — our sweep beats it** |
| Risk guards: max_concurrent 3, DD 1 SOL, cooldown 300s | User-ordered OFF (0/null) for shadow sample throughput | NOT-to-copy in shadow |
| Zeabur ephemeral disk → /app/data | Deploy is a VM under systemd, persistent disk; state/ already on disk | N/A here (WAL still worth it) |
| Rate-limiter bypass for hot path | `client.py` awaits `TokenBucketRateLimiter.acquire()` (lines 384/410). Bypassing would AMPLIFY publicnode 429 storm | NOT-to-copy while on free RPC |

## Plan (verified gaps only)

### P0 — safety: config must not silently ignore or re-route
| ID | Item | Action | Verifier |
|---|---|---|---|
| A2-P0-1 | Unknown-key config lint | `config_loader` warns on unrecognized keys in known sections (filters/trade/research/copy_trader/priority_fees/risk) so a bad paste (like the audit YAML) surfaces instead of silently no-opping; ALSO warn when `trade.exit_mode` is absent while `trade.exit_strategy` is set (the audit yaml would silently flip live exits back to tp_sl) | `verify_config_lint.py` — feed audit-style yaml, assert warnings fire + no silent override |
| A2-P0-2 | Document NOT-to-apply values | `docs/audit-2-not-to-copy.md`: forbid pasting audit §10 yaml (buy_amount 0.05, exit_strategy tp_sl, max_hold_time 40, max_concurrent 3, max_token_age 1.5, copy_trading enabled) — every one overrides a live shadow decision | doc + plan |

### P1 — persistence + ops hygiene (verified real)
| ID | Item | Action | Verifier |
|---|---|---|---|
| A2-P1-1 | WAL on the 3 stores | Add `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;` to connection init in `creator_pnl_store.py`, `copy_target_store.py`, `strategies_ledger.py` (match rug_memory pattern) | `verify_store_wal.py` — open each store, assert `journal_mode=wal` + `synchronous=1` |
| A2-P1-2 | Kill startup Traceback noise | Move `bots/bot-sniper-1/2/3/5/6-yaml` → `bots/disabled/` (bot_runner globs only `bots/*.yaml`); git mv keeps history | restart bot, assert fresh log has **0** `Traceback` lines (vs 9 today) |

### P2 — runtime (two-liners, low risk)
| ID | Item | Action | Verifier |
|---|---|---|---|
| A2-P2-1 | GC threshold | `gc.set_threshold(50000, 50, 50)` in `bot_runner.py` (uvloop already done — audit claim already satisfied, no action) | runs; suite green |
| A2-P2-2 | slot_buffer / blockhash | **No change** — hot path already bypasses buffer (docstring); keep blockhash 5.0s (2.0s on free publicnode = more 429s). Document as verified-already | test suite |

### P3 — methodology (no behavior change)
| ID | Item | Action | Verifier |
|---|---|---|---|
| A2-P3-1 | Correct audit fact base | Commit a `docs/audit-2-verification.md` table (this page) so future sessions don't re-trust: 101 verifiers, schema key map, which "optimizations" were already done | doc committed |
| A2-P3-2 | Copy-engine thresholds reference | When R5-P0-4 lifecycle verification completes, use audit §6 numbers ONLY as calibration references on the paper ledger, never as live gates | later |

## NOT-to-copy (explicit)
1. **Audit §10 YAML wholesale** — schema mismatch + silently overrides live gates (worst offender: `exit_strategy: "tp_sl"` without `exit_mode`, would re-arm the era that lost −375%).
2. **Exit calibration (§8.1)** — our realistic-fill sweep + live staged_trail-tuned (+355%→ corrected +28% open-time, grid +489% first-fire) outperform; audit numbers are from a 9-SOL operator.
3. **Risk guards (§8.4)** — user set them off for shadow sample throughput.
4. **Rate-limiter bypass (§1.5)** — would worsen the publicnode 429 storm.
5. **pumpapi sidecar removal (§2.4)** — pumpapi_sidecar.py is our DATA infra (replay/grads); removing breaks the pipeline. (pumpapi_exec.py is the only unused fallback.)
6. **cookbook mass deletion (§2.1)** — 52 files are test-coupled (`verify_cookbook_arguments.py`); big-bang delete is high-risk, revisit after R5 closes.
7. **max_real_reserves_sol / anti-bundler math (§5A)** — key invented; shelf math until the real reserves-cap path exists.

## Verification protocol
1. `uv run python tests/regression/run_all.py` before/after each change (100/100 baseline; moves no funds).
2. Each item ships its verifier.
3. Shadow mode stays ON; nothing real-mode enabled.
4. One commit per item group (e.g. `feat(audit2): ...`), plan committed first.

## Suggested order
A2-P0-1 (config lint) → A2-P1-1 (WAL) → A2-P1-2 (bots noise) → A2-P2-1 (GC) →
A2-P3-1 (fact base doc); P0/P1 are the only behavior-adjacent items.