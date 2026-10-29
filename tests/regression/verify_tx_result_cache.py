"""T2.2: confirm-then-parse must not re-fetch the same transaction.

The buy path confirms a signature (which fetches the getTransaction result
to read meta.err), then parses amounts out of the same transaction. Before
the dedup, that second step issued a second getTransaction RPC — a wasted
round trip on the hot buy path, bounded only by luck. The cache in
`SolanaClient` shares the first fetch with the parsers so a trade that
confirms then parses pays exactly one getTransaction.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from core.client import SolanaClient  # noqa: E402

SIG = str(Signature.from_bytes(bytes(range(64))))


class _FakeClient(SolanaClient):
    """Real class, stubbed transport: counts getTransaction RPCs issued."""

    def __init__(self) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
        super().__init__("http://127.0.0.1:1")
        self.fetches: list[str] = []
        # The confirm status-poll goes through the wrapped solana client;
        # stub it to a no-op so only the getTransaction path is exercised.
        self.get_client = self._fake_get_client  # type: ignore[method-assign]

    async def _fake_get_client(self):  # noqa: ANN201
        return SimpleNamespace(confirm_transaction=self._noop_confirm)

    async def _noop_confirm(
        self, _signature, commitment: str = "confirmed", sleep_seconds: int = 1
    ) -> None:
        return None

    async def post_rpc(self, body, **_kwargs):  # noqa: ANN001
        method = body.get("method")
        if method == "getTransaction":
            self.fetches.append(body["params"][0])
            return {"result": {"meta": {"err": None, "postTokenBalances": []}}}
        if method == "getSignatureStatuses":
            return {"result": {"value": [{"confirmationStatus": "confirmed"}]}}
        return None


def main() -> None:
    """Build the fake client inside a running loop (the ctor spawns a task)."""
    import asyncio

    return asyncio.run(_run())


async def _run() -> int:
    fails: list[str] = []
    c = _FakeClient()

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            fails.append(label)

    # The confirm path fetches the result once.
    status = await c.confirm_transaction_detailed(SIG)
    check("confirm path resolves status", status is not None, f"-> {status}")
    n_after_confirm = len(c.fetches)
    check("confirm path fetched the tx once", n_after_confirm == 1, f"n={n_after_confirm}")

    # Parsing the same signature reuses the cached result: no second fetch.
    bal = await c.get_transaction_token_balance(
        SIG, Pubkey.new_unique(), Pubkey.new_unique()
    )
    check("balance parser runs", bal is None, f"-> {bal}")
    check(
        "balance parser reused the cached fetch",
        len(c.fetches) == n_after_confirm,
        f"fetches {len(c.fetches)} vs {n_after_confirm}",
    )

    # A different signature still fetches fresh.
    await c.get_transaction_token_balance(
        SIG + "1", Pubkey.new_unique(), Pubkey.new_unique()
    )
    check(
        "different signature fetches fresh",
        len(c.fetches) == n_after_confirm + 1,
        f"fetches {len(c.fetches)} vs {n_after_confirm + 1}",
    )

    await c.close()
    print("\n" + ("T2.2 confirm-path dedup verifier: PASS" if not fails else f"FAIL: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())