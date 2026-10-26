# Bots

Each YAML is a standalone bot configuration for the Memesnipe sniper.

- `memesnipe.yaml` — primary pump.fun config (shadow mode ON; see below).
- `bot-sniper-{1,2,3,5}.yaml` — pump.fun listener variants (geyser / logs / blocks / shreds).
- `bot-sniper-6-stonkfun.yaml` — StonkFun platform config.

## Shadow mode (default in memesnipe.yaml)

`trade.shadow_mode: true` = log-only paper trading: the bot simulates fills at
the live curve price, records to `shadow_data/YYYY-MM-DD.jsonl`, and sends
**zero** transactions. Keep `trade.shadow_mode: true` and `trade.marry_mode:
false` until the wallet is funded and you explicitly decide to go live.

## Anti-bundler extreme-fast calibration (R3-P1-6)

`extreme_fast_mode` buys a FIXED token count (`extreme_fast_token_amount`)
instead of sizing by SOL. This mode trades away the curve-quality gates and
has a real on-chain failure mode: too small a count = dust position whose
sell reverts with pump.fun error `6024 Overflow` (live-verified on DICE).
**It is off by default and must stay off** unless you accept that trade-off.

If it is ever enabled, pair the amount with slippage so a >2.5 SOL dev
bundle reverts on-chain (audit #2 §2.1: at block-0, 0.1 SOL buys 3.56M
tokens clean vs 2.62M under a 5 SOL dev bundle). Calibrated pairs
(amount → tokens at 0.1 SOL buy, block-0, 6-decimal curve):

| extreme_fast_token_amount | expected buy @ block-0 | >2.5 SOL bundle reverts? |
|---------------------------|------------------------|--------------------------|
| 3_560_000                 | ≈ 0.1 SOL clean        | yes (2.62M needed)      |
| 2_620_000                 | ≈ 0.08 SOL             | marginal (bundle edge)  |
| < 2_620_000               | dust risk              | 6024 Overflow on sell   |

The config validator enforces internal consistency: when
`extreme_fast_mode: true`, `extreme_fast_token_amount` must be ≥
`2_620_000` (below the dev-bundle revert threshold the dust/6024 window
reopens) and the implied notional (amount × block-0 price) must not exceed
`trade.max_market_cap_sol`-style sanity bounds. Keeping the mode off makes
the validator a no-op.
