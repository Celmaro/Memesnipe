# Audit-2 NOT-to-copy — values that must never be pasted into live config

Source: the two latency/architecture audits of `5b92d6f` (ed1e81ba, 3fd26e30).
Verified against code at HEAD in `docs/audit-2-plan.md`. This file is the
standing prohibition list. If a session ever applies ANY of these, it has
violated a verified safety decision.

## 1. The audit §10 YAML wholesale — FORBIDDEN
Every top-level section it proposes (`screening:`, `risk_guards:`,
`copy_trading:`, `trade.staged_exits:`, `trade.trailing_stop:`,
`hot_path_zero_rpc:`, `in_stream_exits:`, `depth_floor_sol:`,
`creator_sell_exit:`) does NOT exist in the real schema
(`filters:`, `trade:`, `copy_trader:`, `risk:`). Unknown keys are now
flagged by `config_loader.warn_unknown_config_keys()` (A2-P0-1) — the
warning means "this paste did nothing", not "applied".

Worst single value: `exit_strategy: "tp_sl"` WITHOUT `trade.exit_mode`.
That silently re-arms plain TP/SL — the exit mode whose live era lost
-375% (trail10 alternative was worse; staged_trail-tuned is the tested
winner). `config_loader` warns on this exact shape now.

## 2. Sized/armed values from the audit — FORBIDDEN without re-verification
| Audit value | Why not | Our tested value |
|---|---|---|
| `buy_amount: 0.05` | Audit assumes funded production; live is 0.01 shadow sim | `0.01` |
| `max_hold_time: 40` | User explicitly set `null` (TP/SL govern; remove time gate) | `null` |
| `max_concurrent_positions: 3` | User set `0` (off) for A/B sample throughput | `0` |
| `max_token_age: 1.5` | User set `0` (off); tighter than our ~1.2s/token processing | `0` |
| `risk_guards.max_daily_drawdown_sol: 1.0` etc. | Risk guards off by user order in shadow | `budget_sol/daily_loss_limit_sol` off |
| `copy_trading.enabled: true` | R5-P0-4 lifecycle landed but unverified; stays `copy_trader.enabled: false` | `false` |
| `marry_mode: false / yolo_mode: true` combo | harmless individually but pasted with wrong schema; marry_mode stays false | as-is |
| `node.max_rps: 25` | fine value, but pasting the audit block brings the forbidden keys | `25` |

## 3. Calibration numbers — FORBIDDEN as-is (our data contradicts)
- Exit stages `+50%/50%`, `+120%/50%`, trail `+35%/18%`: our realistic-fill
  sweep found 40%-at-+20% + 8% trail best; base_tp20_sl20 +11.7% beats all
  trails in realistic fills. The audit's numbers come from a 9-SOL operator.
- Gini 0.82 / top5 32% / LPI 0.65 / coordination ≥4 / fragility 0.40-0.65:
  directional only; our gates are off by design until the ledger confirms.
- Copy-leader WR 58–84%, hold ≥25s, PF ≥1.8: references for R5-P0-4
  calibration on the paper ledger, never live gates yet.
- `max_real_reserves_sol: 2.5` anti-bundler: the key does not exist; our
  anti-bundler is `extreme_fast_token_amount` floor (min 2_620_000).

## 4. Structural removals — FORBIDDEN
- `tools/pumpapi_sidecar.py` removal: it's the DATA sidecar (replay/grads/
  paper-copy feed). Removing breaks research. (Only `pumpapi_exec.py` is an
  unused fallback.)
- `cookbook/` mass deletion: 52 files, some test-coupled
  (`verify_cookbook_arguments.py`); big-bang deletion is high risk.
- Rate-limiter bypass for hot path: would amplify the publicnode 429 storm.
- Switching `blockhash_refresh_interval: 5.0 -> 2.0` while on free publicnode:
  more RPC pressure, same throttle. Revisit only on a paid RPC.

## Enforcement
- `config_loader.warn_unknown_config_keys()` runs on every load (A2-P0-1).
- `tests/regression/verify_config_lint.py` pins the audit-paste shape as
  flagged and the shipped config as clean.
- Any session told to "apply the audit YAML" must tell the user it CANNOT
  take effect (unknown keys) instead of editing the yaml to "match".