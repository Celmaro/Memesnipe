# Memesnipe RPC / Geyser resource runbook

## Why the previous live profile saturated

The 2026-10-30 incident report reconciled the approximately $10 drawdown to uncapped position exposure, not RPC fees: 31 buys deployed almost the entire wallet, while the realized-loss circuit breaker observed only about 0.80 USD in realized losses. It also recorded stale-curve buy failures, fee capture marked unknown under 429s, and hundreds of wallet-sweeper failures.

The pre-remediation runtime had several independent sources of request pressure:

- Each open position ran its own price loop. With `price_check_interval: 1` and 10 concurrent positions, the price monitors alone requested roughly 10 curve reads per second.
- `solana-py` confirmation polling was only charged one token before its internal repeated status/block-height reads.
- HTTP 429 handling slept only the request that received the response; other tasks continued sending.
- Holder refresh used raw SDK calls rather than the common client wrapper.
- The wallet sweeper could retry the whole two-program wallet scan three times, while each raw request could itself perform eight 429 retries.
- A shared provider can be hit by several bot processes, but the token bucket exists only inside one `SolanaClient` instance. A per-process 2-RPS setting is not a machine-wide or account-wide quota.

## Changes in this branch

1. The token bucket now has a provider-wide cooldown. Every request sharing the client waits through a 429 cooldown, and the bucket is emptied so the cooldown does not end with a full burst.
2. Raw JSON-RPC and SDK calls record per-method counts, latency and errors. 429s from HTTP and SDK exceptions arm the same cooldown.
3. Confirmation now polls `getSignatureStatuses` through the metered JSON-RPC path. Polls respect the requested commitment and the caller deadline; transaction-history search is avoided for freshly submitted signatures.
4. RPC deadlines now bound token-bucket waiting and the HTTP/JSON response, not merely the sleeps between retries.
5. Holder-snapshot reads use the wrapped client.
6. The sweeper has a small retry/deadline budget. A rate-limited full-wallet scan is not immediately repeated.
7. Geyser reconnect backoff is no longer reset immediately after every successful TCP/gRPC connection. `RESOURCE_EXHAUSTED` is logged separately and gets the maximum reconnect delay. Stream message sizes are capped for this transaction/slot subscription.
8. The Pump.fun curve-state cache is now a real TTL + LRU cache with a hard entry limit.
9. Buyer/advisory maps are retained only for current candidates or owned positions and are cleared after a position closes or a candidate is evicted.
10. The branch config is a conservative read-only profile: 2-RPS / burst-1, two positions, three-second price polling, 15-second blockhash refresh, fixed rather than dynamically sampled priority fees, and shadow mode enabled.

## What to watch during shadow validation

The periodic metrics line now includes:

- `rpc_requests`: metered raw + SDK RPC requests in this process.
- `rpc_429`: HTTP/SDK rate-limit responses.
- `rpc_p95`: recent RPC latency histogram p95.
- `geyser_reconnects`: stream reconnect count.
- `grpc_exhausted`: gRPC `RESOURCE_EXHAUSTED` count.

The counters are process-local and cumulative; they are not provider billing metrics. The latency histogram is a rolling sample, not a full historical trace. If multiple processes share a key/IP, aggregate all their logs and provider dashboards.

For the Geyser stream, preserve the actual gRPC status code and details. `RESOURCE_EXHAUSTED` is not automatically synonymous with an HTTP 429: it can represent provider quota, resource exhaustion or flow-control pressure. A quiet stream, `UNAVAILABLE`, `DEADLINE_EXCEEDED` and `RESOURCE_EXHAUSTED` should be compared separately.

## Provider layout

Do not use Solana's shared public mainnet endpoint as the production transaction path. Use a dedicated HTTP RPC plan for the bot's reads, sends and confirmations, plus a separate Yellowstone/Geyser subscription plan sized for transaction-update volume. Keep the trading read/build/submit/confirm path consistent on one primary HTTP provider; move noncritical analytics to a second read endpoint only after explicitly designing for slot/fork consistency.

The current limiter is process-local. If more than one process uses the same provider account or source IP, either:

- allocate a provider quota to each process with a safe aggregate ceiling; or
- move rate limiting to a shared local service (Redis/sidecar) keyed by provider credential/IP.

A local `max_rps` value cannot enforce a shared quota across independent processes.

## Remaining work before live re-arm

- The session risk counters are still in memory. A restart resets the position/spend counters; the existing unsold registry only rehydrates positions already handed to its watcher. Perform wallet inventory reconciliation and seed/lock risk state before allowing new buys after restart.
- The 60-second wallet sweeper is a safety net, not startup reconciliation. Do not rely on it to prevent new buys immediately after restart.
- Current strategy uses one RPC read per position per monitor tick. A shared price-monitor service that batches open curve accounts with `getMultipleAccounts`, or consumes Geyser account updates for active curves, is the next structural load reduction. It needs parity tests for graduated PumpSwap positions and stale/missing accounts before replacing the current loop.
- Persistent de-duplication for processed mints and explicit lifecycle management for the remaining long-lived sets/maps still need a separate design. Do not simply evict active mints from duplicate protection.
- The branch has not been run against a live RPC provider and no transaction has been sent. Run the offline regression scripts and a shadow observation window before any live canary.

## Regression checks to run

```bash
uv run tests/regression/verify_rpc_deadline.py
uv run tests/regression/verify_rpc_shared_cooldown.py
uv run tests/regression/verify_metered_confirmation.py
uv run tests/regression/verify_curve_state_cache_bounds.py
uv run tests/regression/verify_mint_tracking_cleanup.py
uv run tests/regression/verify_tx_status_checks.py
uv run tests/regression/verify_t1_hardening.py
uv run tests/regression/verify_curve_refresh_buy_path.py
uv run tests/regression/verify_cleanup_harvests_withheld_fees.py
uv run tests/regression/verify_cleanup_survives_shutdown.py
```

After offline checks pass, monitor the shadow profile. A live canary should be a separate operator-approved step with a dedicated provider, a reconciled wallet inventory, persisted position/budget state, a one-position limit and a hard session budget that includes ATA rent and transaction fees.
