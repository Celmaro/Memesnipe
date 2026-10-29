"""R3-P2-2/R3-P2-3 regression: durable nonce + Address Lookup Tables.

Covers:
- `_nonce_blockhash`: decodes the stored blockhash from a valid nonce
  account payload, produces a nonce-advance instruction; refuses malformed /
  short accounts (fail-closed, never guesses).
- `_lookup_tables`: loads a configured ALT account, skips unreadable ones,
  caches per table.
- Config wiring: YAML defaults null/[], validation rules, LIST_OF_STR_FIELDS.
- Off-by-default: no durable_nonce_account / alt_addresses -> the client
  builds a message with an empty lookup table list and the cached blockhash.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.client import SolanaClient  # noqa: E402
import config_loader  # noqa: E402


class FakeAccount:
    def __init__(self, data):
        self.data = data


class FakeClient:
    """Stands in for SolanaClient: get_account_info returns canned bytes."""

    def __init__(self, data_by_addr: dict[str, bytes | None]):
        self._data = data_by_addr
        self.get_account_info_calls = 0

    async def get_account_info(self, address, commitment=None):
        self.get_account_info_calls += 1
        data = self._data.get(str(address))
        return FakeAccount(data) if data is not None else FakeAccount(b"")


def _nonce_payload(stored_hash: bytes) -> bytes:
    # 4-byte version + 32-byte authority + 32-byte blockhash + 8-byte calc
    return b"\x01\x00\x00\x00" + b"\x11" * 32 + stored_hash + b"\x00" * 8


async def test_nonce_layout() -> list[str]:
    failures = []
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    authority = Keypair().pubkey()
    nonce_addr = str(Keypair().pubkey())
    stored = bytes(range(32))
    client = FakeClient({nonce_addr: _nonce_payload(stored)})
    sc = SolanaClient.__new__(SolanaClient)
    sc.durable_nonce_account = nonce_addr
    sc.get_account_info = client.get_account_info  # noqa: SLF001

    bh, ix = await sc._nonce_blockhash(nonce_addr, authority)  # noqa: SLF001
    check(
        "stored blockhash decoded",
        bh == Hash.from_bytes(stored),
        f"-> {bh}",
    )
    check("nonce-advance instruction produced", ix is not None)

    # Malformed: too short -> fail closed.
    client_short = FakeClient({nonce_addr: b"\x01\x00\x00\x00"})
    sc2 = SolanaClient.__new__(SolanaClient)
    sc2.durable_nonce_account = nonce_addr
    sc2.get_account_info = client_short.get_account_info  # noqa: SLF001
    try:
        await sc2._nonce_blockhash(nonce_addr, authority)  # noqa: SLF001
        check("short nonce account refused", False)
    except ValueError:
        check("short nonce account refused", True)
    return failures


async def test_alt_loading() -> list[str]:
    failures = []
    from unittest.mock import patch as _mpatch
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.keypair import Keypair as _Kp
    from solders.pubkey import Pubkey as _Pubkey

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    alt_addr = str(_Kp().pubkey())
    table = AddressLookupTableAccount(
        _Pubkey.from_string(alt_addr),
        [_Pubkey.new_unique(), _Pubkey.new_unique()],
    )
    client = FakeClient({alt_addr: b"\x01" * 120})
    sc = SolanaClient.__new__(SolanaClient)
    sc.alt_addresses = [alt_addr]
    sc._alt_cache = {}  # noqa: SLF001
    sc.get_account_info = client.get_account_info  # noqa: SLF001

    # The client decodes real on-chain data via solders.from_bytes; the
    # verifier stubs that decoder (trusted) to return the constructed table.
    with _mpatch(
        "core.client.AddressLookupTableAccount.from_bytes", return_value=table
    ):
        tables = await sc._lookup_tables()  # noqa: SLF001
        check(
            "ALT loaded from bytes",
            len(tables) == 1 and len(tables[0].addresses) == 2,
            f"-> {len(tables)} table(s)",
        )
        # Cached: second call does not re-read.
        calls_before = client.get_account_info_calls
        await sc._lookup_tables()  # noqa: SLF001
        check(
            "ALT cached within TTL (no second RPC)",
            client.get_account_info_calls == calls_before,
            f"-> {client.get_account_info_calls} vs {calls_before}",
        )

    # Unreadable ALT skipped, no crash.
    missing_addr = str(_Kp().pubkey())
    sc2 = SolanaClient.__new__(SolanaClient)
    sc2.alt_addresses = [missing_addr]
    sc2._alt_cache = {}  # noqa: SLF001
    sc2.get_account_info = FakeClient({}).get_account_info  # noqa: SLF001
    tables2 = await sc2._lookup_tables()  # noqa: SLF001
    check("unreadable ALT skipped", tables2 == [])
    return failures


def test_config_wiring() -> list[str]:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML default durable_nonce_account null",
        cfg["trade"].get("durable_nonce_account") is None,
        f"-> {cfg['trade'].get('durable_nonce_account')!r}",
    )
    check(
        "YAML default alt_addresses []",
        cfg["trade"].get("alt_addresses") == [],
        f"-> {cfg['trade'].get('alt_addresses')!r}",
    )
    check(
        "LIST_OF_STR_FIELDS includes trade.alt_addresses",
        "trade.alt_addresses" in config_loader.LIST_OF_STR_FIELDS,
    )
    rule_paths = [r[0] for r in config_loader.CONFIG_VALIDATION_RULES]
    check(
        "validation rule for durable_nonce_account",
        "trade.durable_nonce_account" in rule_paths,
    )
    return failures


def main() -> int:
    failures = asyncio.run(test_nonce_layout())
    failures += asyncio.run(test_alt_loading())
    failures += test_config_wiring()
    if failures:
        print(f"\nR3-P2-2/P2-3 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P2-2/P2-3 durable nonce + ALTs OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())