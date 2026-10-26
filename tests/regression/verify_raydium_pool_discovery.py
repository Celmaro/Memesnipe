"""P2-STRUC-3 regression: Raydium byte-offset memcmp pool discovery.

Reference behavior (raydium.rs:148-208): zero-API Raydium pool discovery via
getProgramAccounts with a dataSize filter (AmmInfo = 752 bytes) plus memcmp
on the coin mint at byte offset 400 and the pc mint at offset 432. Guards:
1. Filter construction: exact offsets, exact dataSize, base58-encoded memcmp
   bytes (the format solana-py's MemcmpOpts wants).
2. The client-side AmmInfo verifier matches by raw byte offsets.
3. Off-by-default / no hot-path wiring: nothing else imports this module.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from platforms.raydium.pool_discovery import (
    AMM_INFO_LEN,
    COIN_MINT_OFFSET,
    PC_MINT_OFFSET,
    build_pool_filters,
    pool_from_account,
)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def make_pool_bytes(coin: bytes, pc: bytes) -> bytes:
    """Aminfo-shaped blob: 752 bytes with coin/pc at the canonical offsets."""
    data = bytearray(752)
    data[COIN_MINT_OFFSET : COIN_MINT_OFFSET + 32] = coin
    data[PC_MINT_OFFSET : PC_MINT_OFFSET + 32] = pc
    return bytes(data)


def main() -> int:
    from solana.rpc.core import MemcmpOpts
    from solders.pubkey import Pubkey

    coin = Pubkey.from_string(
        "2ueF9m1w9b7UqWQ5fUoWcKQ7jTxZeEKi24cYd4u9pump"
    )
    pc = Pubkey.from_string("So11111111111111111111111111111111111111112")

    # 1. Filter list: [dataSize(752), memcmp(400), memcmp(432)].
    filters = build_pool_filters(coin, pc)
    check("three filters", len(filters) == 3, f"got {len(filters)}")
    check("dataSize filter first", filters[0] == AMM_INFO_LEN, f"got {filters[0]}")
    m1, m2 = filters[1], filters[2]
    check(
        "coin memcmp offset 400",
        isinstance(m1, MemcmpOpts) and m1.offset == 400,
    )
    check(
        "pc memcmp offset 432",
        isinstance(m2, MemcmpOpts) and m2.offset == 432,
    )

    # 2. memcmp bytes are the base58 of the raw mint bytes (what MemcmpOpts
    #    wants — the str round-trips through Pubkey).
    if isinstance(m1, MemcmpOpts):
        check(
            "coin memcmp bytes = base58(mint bytes)",
            "".join(m1.bytes.split()) == str(coin),
            f"got {m1.bytes!r} vs {str(coin)!r}",
        )

    # 3. pool_from_account match + non-match at raw offsets.
    pool = make_pool_bytes(bytes(coin), bytes(pc))
    check("AmmInfo verifier matches pair", pool_from_account(pool, coin, pc))
    check(
        "AmmInfo verifier rejects wrong coin",
        not pool_from_account(pool, pc, coin),
    )
    check(
        "AmmInfo verifier rejects short blob",
        not pool_from_account(b"short", coin, pc),
    )

    # 4. Default pc = WSOL when None.
    filters_default = build_pool_filters(coin)
    if isinstance(filters_default[2], MemcmpOpts):
        check(
            "default pc = WSOL",
            "".join(filters_default[2].bytes.split()) == str(pc),
        )

    # 5. No hot-path wiring: the imported module must not be referenced by
    #    the trader or bot_runner (off by default / blueprint only).
    import subprocess

    grep = subprocess.run(
        ["grep", "-rn", "pool_discovery", "src/trading", "src/bot_runner.py"],
        capture_output=True,
        text=True,
    )
    check(
        "no hot-path import (blueprint only)",
        grep.returncode == 1,
        grep.stdout.strip()[:200],
    )

    print()
    if FAILURES:
        print(f"P2-STRUC-3 FAILED: {len(FAILURES)} check(s) — {FAILURES}")
        return 1
    print("P2-STRUC-3 PASSED: Raydium memcmp pool discovery verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())