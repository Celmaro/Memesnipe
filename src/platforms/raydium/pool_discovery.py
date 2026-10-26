"""Raydium AMM v4 pool discovery via byte-offset memcmp (P2-STRUC-3).

Port of the canonical zero-API Raydium pool lookup from the reference bot
(raydium.rs:148-208): a graduated pump.fun / Raydium LP coin's AMM pool is
found by listing the AMM v4 program's accounts where the AmmInfo layout's
coin-mint field (byte offset 400) and pc-mint field (byte offset 432) match
the pair you care about, narrowed by the fixed AmmInfo data size.

This is the blueprint for a GraduatedMarket/Raydium extension: the
discovery primitives here are offline-testable (filter construction) and the
RPC call is a pure list — no API key, no indexer dependency.

Not wired into any hot path; consumers (a future Raydium market) opt in.
Mint fields in AmmInfo are 32 raw bytes at the documented offsets; the
spl-token ATA of a Raydium pool derives from the pool's coin-lp-mint, which
this module does not need to touch for discovery.
"""

from __future__ import annotations

from solana.rpc.core import MemcmpOpts  # re-exported for callers
from solders.pubkey import Pubkey

from core.client import SolanaClient
from utils.logger import get_logger

logger = get_logger(__name__)

# The Raydium AMM v4 program (canonical mainnet address).
AMM_V4_PROGRAM: str = "675kPX9MHTjS2zt1qfr1NYAnze697dDmgDZJzP5sN"

# AmmInfo account layout: 752 bytes. The two 32-byte mint fields sit at
# byte offsets 400 (coin mint) and 432 (pc mint); a dataSize filter on 752
# plus the two memcmp fields uniquely identify the pool for a (coin, pc)
# pair without decoding the whole layout.
AMM_INFO_LEN = 752
COIN_MINT_OFFSET = 400
PC_MINT_OFFSET = 432


def build_pool_filters(
    coin_mint: Pubkey, pc_mint: Pubkey | None = None
) -> list[object]:
    """Memcmp/dataSize filters that select one Raydium AMM v4 pool.

    Args:
        coin_mint: The base token mint (e.g. a graduated pump.fun mint).
        pc_mint: The quote mint (defaults to WSOL when None). On Raydium,
            WSOL is the pc side for every SOL pair.

    Returns:
        solana-py filter list: [dataSize(752), memcmp(offset 400, coin_mint),
        memcmp(offset 432, pc_mint)].
    """
    coin_bytes = bytes(coin_mint)
    pc_bytes = bytes(pc_mint) if pc_mint is not None else bytes(WSOL_MINT())
    return [
        AMM_INFO_LEN,
        MemcmpOpts(offset=COIN_MINT_OFFSET, bytes=b58(coin_bytes)),
        MemcmpOpts(offset=PC_MINT_OFFSET, bytes=b58(pc_bytes)),
    ]


def pool_from_account(
    account_data: bytes,
    coin_mint: Pubkey | None = None,
    pc_mint: Pubkey | None = None,
) -> bool:
    """True when account_data is an AmmInfo for the given (coin, pc) pair.

    Pure byte-offset check, no RPC. Used to validate what a memcmp query
    returned (defense in depth: memcmp is server-side, this is client-side).
    """
    if len(account_data) < PC_MINT_OFFSET + 32:
        return False
    if coin_mint is not None:
        if account_data[COIN_MINT_OFFSET : COIN_MINT_OFFSET + 32] != bytes(
            coin_mint
        ):
            return False
    if pc_mint is not None:
        if account_data[PC_MINT_OFFSET : PC_MINT_OFFSET + 32] != bytes(pc_mint):
            return False
    return True


async def discover_pools(
    client: SolanaClient,
    coin_mint: Pubkey,
    pc_mint: Pubkey | None = None,
    program: str = AMM_V4_PROGRAM,
) -> list[Pubkey]:
    """Find Raydium AMM v4 pool accounts for a (coin, pc) pair.

    Args:
        client: RPC client (get_program_accounts wrapper; fail-open).
        coin_mint: Base mint to discover pools for.
        pc_mint: Quote mint; None = WSOL.
        program: Raydium AMM v4 program id (default canonical).

    Returns:
        Pool account pubkeys (empty on none found / RPC failure).
    """
    filters = build_pool_filters(coin_mint, pc_mint)
    try:
        accounts = await client.get_program_accounts(
            Pubkey.from_string(program), filters=filters
        )
    except Exception:
        logger.exception("Raydium pool discovery failed")
        return []
    if not accounts:
        return []
    # Defense in depth: re-verify each hit's raw bytes (the memcmp already
    # did this server-side; this catches a provider that ignores filters).
    return [
        pubkey
        for pubkey, account in accounts
        if pool_from_account(account.data, coin_mint, pc_mint)
    ]


def b58(data: bytes) -> str:
    """Base58-encode raw bytes (MemcmpOpts wants a base58 str, not bytes)."""
    return str(Pubkey.from_bytes(data))


def WSOL_MINT() -> Pubkey:  # noqa: N802 - imported lazily to avoid cycle
    """WSOL mint (the default pc side of a Raydium SOL pair)."""
    from solders.pubkey import Pubkey as _P

    return _P.from_string("So11111111111111111111111111111111111111112")