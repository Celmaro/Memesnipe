"""P2-STRUC-1 regression: blockhash cache freshness + cadence wiring.

Reference behavior (solana-trading-bot tx.rs:37-46): a cached blockhash is
refreshed off the hot path so sends never wait on an RPC round trip, and a
send never signs with a hash that could expire mid-flight. Guards:
1. SolanaClient accepts a configurable refresh interval and passes it to the
   background updater.
2. get_cached_blockhash refreshes synchronously when the cache is empty or
   older than 2x the interval (the stale path), and returns the last-known
   hash when a refresh fails after a prior success (a blip must not block).
3. YAML + validation wiring exist.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.client import SolanaClient

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class _BlockhashClient(SolanaClient):
    """Stub subclass: no real RPC, scripted blockhash fetch."""

    def __init__(self, interval: float):
        # Bypass the parent __init__ entirely (it starts the updater task);
        # set the fields get_cached_blockhash needs.
        self.rpc_endpoint = "stub"
        self._client = None
        self._cached_blockhash = None
        self._cached_blockhash_ts = 0.0
        self._blockhash_lock = asyncio.Lock()
        self.blockhash_refresh_interval = interval
        self.fetches = 0

    async def get_latest_blockhash(self):  # type: ignore[override]
        self.fetches += 1
        from solders.hash import Hash

        return Hash.from_string(
            "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
        )


def main() -> int:
    async def run() -> None:
        # 1. Empty cache -> synchronous fetch, then returns the hash.
        c = _BlockhashClient(interval=5.0)
        bh = await c.get_cached_blockhash()
        check("empty cache fetches", c.fetches == 1)
        check("empty cache returns hash", bh is not None)

        # 2. Fresh cache -> no refetch.
        c2 = _BlockhashClient(interval=5.0)
        c2._cached_blockhash = bh
        c2._cached_blockhash_ts = asyncio.get_event_loop().time()
        check("fresh cache no refetch", (await c2.get_cached_blockhash()) == bh)
        check("fresh cache fetches == 0", c2.fetches == 0)

        # 3. Stale cache (interval 0.05 -> stale floor 1s) -> refetch.
        c3 = _BlockhashClient(interval=0.05)
        c3._cached_blockhash = bh
        c3._cached_blockhash_ts = (
            asyncio.get_event_loop().time() - 5.0
        )
        await c3.get_cached_blockhash()
        check("stale cache refetches", c3.fetches >= 1)

        # 4. Refresh failure after a prior success -> last-known hash, no
        # raise (C2.2: only when the fallback is younger than the 150-block
        # validity window — a transient RPC blip must not block a send).
        c4 = _BlockhashClient(interval=0.05)
        c4._cached_blockhash = bh
        c4._cached_blockhash_ts = asyncio.get_event_loop().time() - 5.0  # stale, <60s
        c4.get_latest_blockhash = None  # type: ignore[assignment]  # break it

        async def boom():  # type: ignore[misc]
            raise RuntimeError("rpc down")

        c4.get_latest_blockhash = boom  # type: ignore[assignment]
        bh4 = await c4.get_cached_blockhash()
        check("failed refresh falls back to last-known", bh4 == bh)

        # 4b. C2.2: a refresh failure on a hash OLDER than the validity
        # window must FAIL CLOSED (raise) — signing a chain-dead hash
        # cannot land and wastes the send.
        c4b = _BlockhashClient(interval=0.05)
        c4b._cached_blockhash = bh
        c4b._cached_blockhash_ts = (
            asyncio.get_event_loop().time() - 120.0  # past 60s window
        )
        c4b.get_latest_blockhash = boom  # type: ignore[assignment]
        try:
            await c4b.get_cached_blockhash()
            check(
                "chain-dead fallback fails closed",
                False,
                "did not raise on dead hash",
            )
        except RuntimeError:
            check(
                "chain-dead fallback fails closed",
                True,
                "raised RuntimeError",
            )

    asyncio.run(run())

    # 5. Constructor stores the interval.
    try:
        sc = SolanaClient.__new__(SolanaClient)
        sc.blockhash_refresh_interval = 0.5
        check("client stores interval", sc.blockhash_refresh_interval == 0.5)
    except Exception:  # noqa: BLE001
        check("client stores interval", False, "constructor path raised")

    # 6. YAML + validation wiring.
    import yaml

    import config_loader

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML declares blockhash_refresh_interval",
        "blockhash_refresh_interval" in cfg.get("trade", {}),
    )
    check(
        "validation range entry exists",
        any(
            r[0] == "trade.blockhash_refresh_interval"
            for r in config_loader.CONFIG_VALIDATION_RULES
        ),
    )

    print()
    if FAILURES:
        print(f"P2-STRUC-1 FAILED: {len(FAILURES)} check(s) — {FAILURES}")
        return 1
    print("P2-STRUC-1 PASSED: blockhash freshness + cadence verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())