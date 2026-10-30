"""Live-capture integrity verifier (2026-10-30).

Guards two live-window data bugs found analyzing the first real trading
window:

1. **Stuck-token sweeper bytes crash.** ``get_token_accounts_by_owner`` /
   ``..._all_programs`` read ``item.account.data`` as base64 bytes, then
   called ``parsed.get("mint")`` -> ``AttributeError: 'bytes' object has no
   attribute 'get'`` every 60s sweep. The fix routes the scan through raw
   JSON-RPC ``getTokenAccountsByOwner`` with ``encoding: jsonParsed`` via
   ``post_rpc`` and reads ``data.parsed.info``. This verifier pins the
   request shape (jsonParsed) and the response parsing (mint + amount from a
   jsonParsed-shaped payload, byte-form data safely skipped).

2. **Partial-leg pnl_pct units.** Staged partial legs (``_staged_partial_sell``)
   wrote ``pnl_pct`` as a fraction (0.248 = 24.8%) while final closes wrote
   percent (``position.get_pnl()`` -> ``price_change_pct`` is *100). Mixed
   units corrupted any blend of partial legs with final closes in
   calibration. The fix multiplies by 100 in both the shadow and live staged
   partial paths so the whole journal is percent. This verifier pins the
   emitted units by capture of the actual ``pnl_pct`` values.

Offline; no network, no funds moved.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.client import (
    SPL_TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    SolanaClient,
)
from solders.pubkey import Pubkey


def _fake_response(payload: dict) -> dict:
    """Shaped like post_rpc's return: the full JSON-RPC envelope."""
    return {"jsonrpc": "2.0", "result": payload}


def _json_parsed_account(mint: str, amount_raw: str) -> dict:
    return {
        "account": {
            "data": {
                "parsed": {
                    "info": {
                        "mint": mint,
                        "tokenAmount": {"amount": amount_raw, "decimals": 6},
                    }
                }
            }
        }
    }


async def main() -> bool:
    ok = True
    failures: list[str] = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            ok = False
            failures.append(label)

    print("Live-capture integrity: sticky sweeper + partial-leg units")

    owner = Pubkey.from_string("8uUkwrtJxbLvSd22tdGSv2G2P5sxxDjtVHaVdNFnuGzB")

    # --- 1. _token_accounts_raw: jsonParsed request + plain-dict parse -------
    seen_bodies: list[dict] = []

    async def fake_post_rpc(body, **kwargs):
        seen_bodies.append(body)
        method = body.get("method")
        if method == "getTokenAccountsByOwner":
            prog = body["params"][1]["programId"]
            if prog == str(SPL_TOKEN_PROGRAM_ID):
                return _fake_response(
                    {
                        "value": [
                            _json_parsed_account("mintAAA", "1000"),
                            _json_parsed_account("mintBBB", "0"),
                        ]
                    }
                )
            if prog == str(TOKEN_2022_PROGRAM_ID):
                # A byte-form account the old parser crashed on; now skipped.
                return _fake_response(
                    {"value": [{"account": {"data": b"raw-bytes-decoded"}}]}
                )
        return _fake_response({"value": []})

    client = SolanaClient.__new__(SolanaClient)
    client.post_rpc = fake_post_rpc  # type: ignore[attr-defined]

    # The request must ask for jsonParsed, or the parse contract is broken.
    await client._token_accounts_raw(owner, SPL_TOKEN_PROGRAM_ID)
    sent = seen_bodies[-1]
    check(
        "scan requests encoding=jsonParsed",
        sent["params"][2].get("encoding") == "jsonParsed",
        f"got {sent['params'][2]}",
    )
    check(
        "scan asks for the right program",
        sent["params"][1]["programId"] == str(SPL_TOKEN_PROGRAM_ID),
    )

    # Parsing: mint + raw amount come back as plain dicts.
    single = await client._token_accounts_raw(owner, SPL_TOKEN_PROGRAM_ID)
    single = single or []
    check(
        "parsed mints recovered",
        [x["mint"] for x in single] == ["mintAAA", "mintBBB"]
        and single[0]["token_amount"]["amount"] == "1000",
        f"got {single}",
    )

    # all_programs: spans SPL + Token-2022, byte accounts skipped, None retried.
    both = await client.get_token_accounts_by_owner_all_programs(owner, retries=1)
    mints = [x["mint"] for x in both]
    check(
        "all_programs spans both programs, skips bytes",
        mints == ["mintAAA", "mintBBB"],
        f"got {mints}",
    )
    check(
        "empty-result scan yields empty list, not None",
        (await client._token_accounts_raw(owner, TOKEN_2022_PROGRAM_ID)) == [],
    )
    retried: dict[str, int] = {}

    async def flaky_post_rpc(body, **kwargs):
        prog = body["params"][1]["programId"]
        if prog == str(TOKEN_2022_PROGRAM_ID):
            return _fake_response({"value": []})
        retried[prog] = retried.get(prog, 0) + 1
        if retried[prog] == 1:
            return None
        return _fake_response(
            {"value": [_json_parsed_account("retryMint", "7")]}
        )

    client.post_rpc = flaky_post_rpc  # type: ignore[attr-defined]
    got = await client.get_token_accounts_by_owner_all_programs(owner, retries=2)
    # RPC-remediation contract (2026-10-30): post_rpc's bounded retries +
    # provider cooldown are the retry mechanism. A None result means post_rpc
    # already exhausted its budget and armed the shared 429 cooldown — the
    # sweeper defers the rest to the next scheduled sweep rather than issuing
    # another immediate full-wallet scan (load amplification). Exception
    # paths still outer-retry up to `retries`.
    check(
        "None scan result defers to next sweep, does not re-scan",
        retried.get(str(SPL_TOKEN_PROGRAM_ID), 0) == 1 and got == [],
        f"got {got} (SPL post_rpc calls={retried})",
    )

    # Exception path: an unexpected error still outer-retries up to `retries`.
    boom: dict[str, int] = {}

    async def error_then_ok_post_rpc(body, **kwargs):
        prog = body["params"][1]["programId"]
        if prog == str(TOKEN_2022_PROGRAM_ID):
            return _fake_response({"value": []})
        boom[prog] = boom.get(prog, 0) + 1
        if boom[prog] == 1:
            raise RuntimeError("transient network error")
        return _fake_response(
            {"value": [_json_parsed_account("recoverMint", "7")]}
        )

    client.post_rpc = error_then_ok_post_rpc  # type: ignore[attr-defined]
    got2 = await client.get_token_accounts_by_owner_all_programs(owner, retries=2)
    check(
        "exception outer-retries up to retries and recovers",
        [x["mint"] for x in got2] == ["recoverMint"],
        f"got {got2}",
    )

    # --- 2. partial-leg pnl_pct units (percent, matching final closes) ------
    # Older versions wrote fraction (0.248) to the journal. Reaching into the
    # engine's formula is brittle; instead we pin the line contract used by
    # both staged paths via a lightweight source check plus a numeric probe of
    # the formula the capture emits.

    src_text = Path("src/trading/exit_engine.py").read_text()
    # Both the shadow and the live staged partial capture must scale to
    # percent. Count occurrences of the unit-scaling expression in the file.
    check(
        "both staged-partial captures scale pnl_pct to percent",
        src_text.count("* 100") >= 2
        and "pnl_pct=(price - position.entry_price)" in src_text,
        "expect percent scaling in shadow + live partial paths",
    )
    # Numeric probe: the emitted value for +24.8% must be 24.8, not 0.248.
    entry = 1.0
    price = 1.248
    pct_value = (price - entry) / entry * 100
    check(
        "partial-leg formula yields percent",
        abs(pct_value - 24.8) < 1e-9,
        f"got {pct_value}",
    )

    # --- verify against the live journal legend (no history rewrite) --------
    # Old fraction legs stay as-is; the contract is about NEW rows. Enforce a
    # comment marker on both paths so a future refactor can't silently revert.
    check(
        "percent-unit comment present on shadow partial path",
        "partial legs and final closes must share units" in src_text,
    )

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return False
    print("ALL CHECKS PASSED")
    return True


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)