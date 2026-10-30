"""Solana client abstraction for blockchain operations."""

import asyncio
import enum
import os
import random
import struct
from time import monotonic
from typing import Any

import aiohttp
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Processed
from solana.rpc.core import TxOptsModel
from solders.account import Account
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import AdvanceNonceAccountParams, advance_nonce_account
from solders.transaction import VersionedTransaction

from core.pubkeys import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, is_sol_paired
from core.rpc_rate_limiter import TokenBucketRateLimiter
from monitoring.metrics import metrics
from interfaces.core import ConfirmationStatus, Platform
from utils.idl_manager import get_idl_parser
from utils.logger import get_logger

# Token program ids for the token-accounts-by-owner scan (stuck sweeper).
# Reuse the canonical Pubkeys from core.pubkeys; do not re-derive strings.
SPL_TOKEN_PROGRAM_ID = TOKEN_PROGRAM
TOKEN_2022_PROGRAM_ID = TOKEN_2022_PROGRAM

logger = get_logger(__name__)

HTTP_TOO_MANY_REQUESTS = 429

# Length of the `[instruction_index, error_detail]` pair inside
# `{"InstructionError": [...]}` -- fixed by the RPC's `meta.err` shape.
INSTRUCTION_ERROR_PAIR_LEN = 2

# How long getTransaction keeps retrying a signature the node has not caught up
# to yet, and the pause between attempts. A null result means "I cannot see this
# transaction", which on a load-balanced endpoint is not the same as "it failed".
TX_RESULT_RETRY_BUDGET = 5.0
TX_RESULT_RETRY_DELAY = 0.4


class TxFailureKind(enum.Enum):
    """Why a transaction send failed, driving whether retry can help.

    Mapped from the RPC error text at the send call site. `UNKNOWN` is the
    default and the only safe catch-all: a generic failure string must not be
    treated as a retryable one.
    """

    BLOCKHASH_EXPIRED = "blockhash_expired"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    TXN_SIMULATION_FAILED = "txn_simulation_failed"
    SLIPPAGE_EXCEEDED = "slippage_exceeded"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


#: Substrings that classify a send exception. Order matters: check the most
#: specific first. These are the on-chain / preflight verdicts that come back
#: as exceptions.
_FAILURE_MARKERS: tuple[tuple[str, TxFailureKind], ...] = (
    ("BlockhashNotFound", TxFailureKind.BLOCKHASH_EXPIRED),
    ("blockhash not found", TxFailureKind.BLOCKHASH_EXPIRED),
    ("insufficient funds", TxFailureKind.INSUFFICIENT_FUNDS),
    ("InsufficientFundsForRent", TxFailureKind.INSUFFICIENT_FUNDS),
    ("Transaction simulation failed", TxFailureKind.TXN_SIMULATION_FAILED),
    ("simulation failed", TxFailureKind.TXN_SIMULATION_FAILED),
    ("Slippage", TxFailureKind.SLIPPAGE_EXCEEDED),
    ("slippage", TxFailureKind.SLIPPAGE_EXCEEDED),
)


def classify_tx_failure(exc: BaseException) -> TxFailureKind:
    """Classify a send exception into a retry policy category."""
    text = str(exc)
    for marker, kind in _FAILURE_MARKERS:
        if marker in text:
            return kind
    return TxFailureKind.UNKNOWN


class _Deadline:
    """An optional wall-clock budget shared by every attempt of one RPC call.

    Attempts and elapsed time are different bounds; this carries the second, so a
    caller can cap waiting without touching the retry counts.
    """

    def __init__(self, seconds: float | None) -> None:
        """Start the clock.

        Args:
            seconds: Budget in seconds, or None for no deadline at all
        """
        self.seconds = seconds
        self._expires_at = None if seconds is None else monotonic() + seconds

    def expired(self) -> bool:
        """Check whether the budget is already spent.

        Returns:
            True if there is a deadline and it has passed
        """
        return self._expires_at is not None and monotonic() >= self._expires_at

    def allows(self, wait: float) -> bool:
        """Check whether a backoff fits in what is left of the budget.

        Args:
            wait: How long the next backoff would sleep, in seconds

        Returns:
            True if the wait fits, or if there is no deadline
        """
        return self._expires_at is None or wait < self._expires_at - monotonic()

    def __str__(self) -> str:
        """Describe the deadline for a log line.

        Returns:
            Human-readable budget
        """
        return (
            "unbounded budget"
            if self.seconds is None
            else f"{self.seconds:.1f}s deadline"
        )


def _retry_after_seconds(header: str | None, attempt: int) -> float:
    """Work out how long to wait before retrying a rate-limited request.

    Args:
        header: Raw `Retry-After` header value, if the server sent one
        attempt: 1-based count of 429s seen so far on this call

    Returns:
        Seconds to wait, including jitter
    """
    try:
        wait_time = float(header) if header else None
    except (ValueError, TypeError):
        wait_time = None
    if wait_time is None:
        wait_time = min(2**attempt, 30)
    return wait_time + wait_time * random.uniform(0, 0.25)  # noqa: S311


def set_loaded_accounts_data_size_limit(bytes_limit: int) -> Instruction:
    """Create a SetLoadedAccountsDataSizeLimit instruction to reduce CU cost.

    A transaction may load up to 64MB of account data by default, costing 16k CU
    (8 CU per 32KB); a lower limit reduces that and improves priority. The saving
    is not visible in "consumed CU", which only counts execution CU.

    Args:
        bytes_limit: Max account data size in bytes

    Returns:
        Compute Budget instruction with discriminator 4

    Reference:
        https://www.anza.xyz/blog/cu-optimization-with-setloadedaccountsdatasizelimit
    """
    COMPUTE_BUDGET_PROGRAM = Pubkey.from_string(
        "ComputeBudget111111111111111111111111111111"
    )

    data = struct.pack("<BI", 4, bytes_limit)
    return Instruction(COMPUTE_BUDGET_PROGRAM, data, [])


def _describe_program_error(err: object) -> str | None:
    """Best-effort human name for an Anchor `Custom(N)` error in `meta.err`.

    Digs `{"InstructionError": [idx, {"Custom": n}]}` out of `meta.err` and looks
    the code up in pump.fun's IDL error table. `SolanaClient` is shared across
    platforms and carries no record of which program a transaction invoked, so
    only pump.fun's table (`idl/pump_fun_idl.json`) is ever checked. A revert on
    another program is described against the wrong table if its numeric code is
    also defined there, and left unnamed otherwise. An unrecognized shape returns
    None, never raises.

    Args:
        err: The raw `meta.err` value from a `getTransaction` response.

    Returns:
        A description like "pump.fun IDL: 6062 BuybackFeeRecipientMissing", or
        None. The "pump.fun IDL:" prefix must stay in the rendered string: a
        reader of a log line needs to know the name is pump.fun's interpretation,
        not a fact about whichever program actually reverted.
    """
    if not isinstance(err, dict):
        return None
    instruction_error = err.get("InstructionError")
    if (
        not isinstance(instruction_error, list)
        or len(instruction_error) != INSTRUCTION_ERROR_PAIR_LEN
    ):
        return None
    detail = instruction_error[1]
    if not isinstance(detail, dict):
        return None
    code = detail.get("Custom")
    if not isinstance(code, int):
        return None
    description = get_idl_parser(Platform.PUMP_FUN).describe_error_code(code)
    if description is None:
        return None
    return f"pump.fun IDL: {description}"


class SolanaClient:
    """Abstraction for Solana RPC client operations."""

    def __init__(
        self,
        rpc_endpoint: str,
        max_rps: float = 25.0,
        burst_size: int | None = None,
        blockhash_refresh_interval: float = 5.0,
        durable_nonce_account: str | None = None,
        alt_addresses: list[str] | None = None,
    ):
        """Initialize Solana client with RPC endpoint.

        Args:
            rpc_endpoint: URL of the Solana RPC endpoint
            max_rps: Maximum RPC requests per second (rate limiter)
            blockhash_refresh_interval: Seconds between background cached-
                blockhash refreshes (P2-STRUC-1). A fresh cache means the
                send hot path never waits on a getLatestBlockhash round trip.
                5.0 is friendly to rate-limited public RPCs; a private RPC can
                go lower (validation floor 0.05).
            durable_nonce_account: Base58 nonce ACCOUNT address (R3-P2-2).
                When set, sends use the nonce account's stored blockhash with
                a nonce-advance instruction instead of the short-lived recent
                blockhash (replay-protected, valid far beyond 150 blocks).
                Off by default.
            alt_addresses: List of base58 Address Lookup Table addresses
                (R3-P2-3). When set, static keys are compressed through the
                tables (smaller tx, lower fee, faster validation). Off by
                default.
        """
        self.rpc_endpoint = rpc_endpoint
        self._client = None
        self._cached_blockhash: Hash | None = None
        self._cached_blockhash_ts: float = 0.0
        self._blockhash_lock = asyncio.Lock()
        self.blockhash_refresh_interval = blockhash_refresh_interval
        # C2.2: chain-validity tracking for the cached blockhash. The RPC
        # getLatestBlockhash response carries lastValidBlockHeight; we record
        # it (and the hash) so the cached-blockhash path can enforce a
        # fail-closed sign policy: a hash older than Solana's 150-block
        # validity window (~60s) has no chance of landing, so a refresh
        # failure must refuse the send instead of signing a dead hash.
        self._latest_blockhash: Hash | None = None
        self._latest_blockhash_height: int | None = None
        self._blockhash_updater_task = asyncio.create_task(
            self.start_blockhash_updater(self.blockhash_refresh_interval)
        )
        # R3-P2-2/R3-P2-3: durable nonce + ALTs (off by default).
        self.durable_nonce_account = durable_nonce_account
        self.alt_addresses = alt_addresses or []
        self._alt_cache: dict[str, tuple[float, AddressLookupTableAccount]] = {}
        self._latest_blockhash = None
        self._latest_blockhash_height = None
        # T2.2 confirm-path dedup: parsed getTransaction results keyed by
        # signature, reused by the token-balance / buy-details parsers so a
        # trade that confirms then parses does not fetch the same tx twice.
        self._tx_result_cache: dict[str, tuple[float, dict]] = {}
        self._tx_result_cache_ttl = 60.0
        self._rate_limiter = TokenBucketRateLimiter(
            max_rps=max_rps, burst_size=burst_size
        )
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        # Tatum RPC gateway auth: send x-api-key when the endpoint is a
        # tatum.io gateway and TATUM_API_KEY is present (fall back to the
        # geyser token slot, which the operator also sets to the Tatum key).
        self._rpc_headers: dict[str, str] = {}
        if "tatum.io" in (rpc_endpoint or ""):
            api_key = os.environ.get("TATUM_API_KEY") or os.environ.get(
                "GEYSER_API_TOKEN"
            )
            if api_key:
                self._rpc_headers = {"x-api-key": api_key}

    async def start_blockhash_updater(self, interval: float = 5.0):
        """Start background task to update recent blockhash.

        Runs from the constructor (P2-STRUC-1), so the cache is warm before
        the first send and the hot path never waits on a getLatestBlockhash
        round trip. The interval is configurable; 5.0s default is friendly
        to rate-limited public RPCs. C2.2: also proactively refreshes the
        chain-validity height so a send never signs with a hash whose
        lastValidBlockHeight has passed.
        """
        while True:
            try:
                blockhash = await self.get_latest_blockhash()
                async with self._blockhash_lock:
                    self._cached_blockhash = blockhash
                    self._cached_blockhash_ts = monotonic()
            except Exception as e:
                logger.warning(f"Blockhash fetch failed: {e!s}")
            finally:
                await asyncio.sleep(interval)

    async def get_cached_blockhash(self) -> Hash:
        """Return the most recently cached blockhash.

        Refreshes synchronously if the cache is stale (older than twice the
        configured refresh interval, floored at 1s) or empty — the send hot
        path must never sign with a blockhash that could expire mid-flight.
        C2.2: when the RPC told us the hash's lastValidBlockHeight and the
        current chain height has passed it, the hash is DEAD even if the
        local age is fresh — refresh, and if the refresh fails while the
        dead hash is the only fallback, raise (fail-closed) instead of
        signing a transaction that cannot land. Falls back to the last-known
        hash only when the hash is merely locally-stale (a transient RPC
        blip) and we do NOT know it is chain-dead.
        """
        #: C2.2: a cached blockhash older than this is chain-dead — Solana keeps a
        # blockhash valid for 150 blocks (~60s at 400ms slots). Signing with
        # a hash past that window cannot land; when the refresh ALSO fails,
        # the send must fail closed instead of signing a known-dead hash.
        # Young-ish (10-60s) fallbacks are still a shot (the local age check
        # at 2x interval treats them stale and refreshes first).
        max_fallback_age_s = 60.0
        async with self._blockhash_lock:
            stale_seconds = max(2 * self.blockhash_refresh_interval, 1.0)
            locally_stale = (
                self._cached_blockhash is None
                or monotonic() - self._cached_blockhash_ts > stale_seconds
            )

            if locally_stale:
                try:
                    blockhash = await self.get_latest_blockhash()
                    self._cached_blockhash = blockhash
                    self._cached_blockhash_ts = monotonic()
                except Exception:
                    if self._cached_blockhash is None:
                        raise RuntimeError(
                            "No cached blockhash available yet"
                        ) from None
                    # C2.2: a hash whose validity has lapsed has no chance of
                    # landing — refusing is correct (fail-closed). Only a
                    # fallback younger than the 150-block validity window may
                    # be signed, and only when the RPC is having a transient
                    # blip.
                    if (
                        monotonic() - self._cached_blockhash_ts
                        > max_fallback_age_s
                    ):
                        raise RuntimeError(
                            "Cached blockhash older than validity window "
                            "(lastValidBlockHeight passed) and refresh failed"
                        ) from None
                    logger.warning(
                        "Blockhash refresh failed; using last-known blockhash "
                        f"({monotonic() - self._cached_blockhash_ts:.0f}s old)"
                    )
            return self._cached_blockhash  # type: ignore[return-value]

    async def get_client(self) -> AsyncClient:
        """Get or create the AsyncClient instance."""
        if self._client is None:
            self._client = AsyncClient(
                self.rpc_endpoint,
                extra_headers=self._rpc_headers or None,
            )
        return self._client

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the shared aiohttp session.

        Returns:
            Shared aiohttp.ClientSession instance.
        """
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10),
                )
            return self._session

    async def close(self):
        """Close the client connection and stop the blockhash updater."""
        if self._blockhash_updater_task:
            self._blockhash_updater_task.cancel()
            try:
                await self._blockhash_updater_task
            except asyncio.CancelledError:
                pass

        if self._client:
            await self._client.close()
            self._client = None

        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _metered_sdk_rpc(self, method: str, request):
        """Await an SDK RPC, record it, and propagate provider throttling."""
        metrics.incr("rpc.requests")
        metrics.incr("rpc.sdk.requests")
        metrics.incr(f"rpc.method.{method}")
        started = monotonic()
        try:
            result = await request
            metrics.incr("rpc.success")
            return result
        except Exception as error:
            metrics.incr("rpc.errors")
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None) or getattr(
                error, "status_code", None
            )
            error_text = str(error).lower()
            if status == HTTP_TOO_MANY_REQUESTS or "429" in error_text or (
                "too many requests" in error_text
            ):
                metrics.incr("rpc.http_429")
                headers = getattr(response, "headers", {}) or {}
                retry_after = _retry_after_seconds(
                    headers.get("Retry-After"), 1
                )
                await self._rate_limiter.defer(max(1.0, retry_after))
            raise
        finally:
            metrics.record("rpc.latency", monotonic() - started)

    async def get_health(self) -> str | None:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getHealth",
        }
        result = await self.post_rpc(body)
        if result and "result" in result:
            return result["result"]
        return None

    async def get_account_info(
        self, pubkey: Pubkey, commitment: str | None = None
    ) -> Account:
        """Get account info from the blockchain.

        Args:
            pubkey: Public key of the account
            commitment: Optional commitment override (e.g., "processed" for
                fresh state right after a geyser event; default "confirmed")

        Returns:
            The solders `Account` -- callers read attributes like `.data` and
            `.owner`, never subscript it.

        Raises:
            ValueError: If account doesn't exist
        """
        await self._rate_limiter.acquire()
        client = await self.get_client()
        kwargs: dict[str, Any] = {"encoding": "base64"}
        if commitment is not None:
            kwargs["commitment"] = commitment
        response = await self._metered_sdk_rpc(
            "getAccountInfo", client.get_account_info(pubkey, **kwargs)
        )
        if not response.value:
            raise ValueError(f"Account {pubkey} not found")
        return response.value

    async def get_multiple_accounts(
        self, pubkeys: list[Pubkey], commitment: str | None = None
    ) -> list[Account | None]:
        """Get several accounts in one slot-consistent RPC round trip.

        A single getMultipleAccounts response is served by one node at one slot,
        unlike back-to-back get_account_info calls, which a load-balanced
        endpoint may serve from nodes seconds apart.

        Args:
            pubkeys: Public keys of the accounts
            commitment: Optional commitment override (default "confirmed")

        Returns:
            One entry per pubkey, in order -- a solders `Account` or None.
        """
        await self._rate_limiter.acquire()
        client = await self.get_client()
        kwargs: dict[str, Any] = {"encoding": "base64"}
        if commitment is not None:
            kwargs["commitment"] = commitment
        response = await self._metered_sdk_rpc(
            "getMultipleAccounts", client.get_multiple_accounts(pubkeys, **kwargs)
        )
        return list(response.value)

    async def get_token_largest_accounts(
        self, mint: Pubkey, commitment: str = "confirmed"
    ):
        """Fetch the largest token accounts through the shared RPC budget."""
        await self._rate_limiter.acquire()
        client = await self.get_client()
        return await self._metered_sdk_rpc(
            "getTokenLargestAccounts",
            client.get_token_largest_accounts(mint, commitment=commitment),
        )

    async def get_token_supply(
        self, mint: Pubkey, commitment: str | None = None
    ) -> int | None:
        """Get the total supply of a mint (T2.5 honeypot burn-ratio gate).

        Args:
            mint: Mint address
            commitment: Optional commitment override (default "confirmed")

        Returns:
            Raw supply (no decimals applied), or None if the RPC fails.
            The caller applies the mint's decimals.
        """
        await self._rate_limiter.acquire()
        client = await self.get_client()
        kwargs: dict[str, Any] = {}
        if commitment is not None:
            kwargs["commitment"] = commitment
        try:
            response = await self._metered_sdk_rpc(
                "getTokenSupply", client.get_token_supply(mint, **kwargs)
            )
            if not response.value:
                return None
            return response.value.amount
        except Exception:
            logger.exception(f"get_token_supply failed for {mint}")
            return None

    async def get_slot(self, commitment: str = "processed") -> int | None:
        """Get the current slot (P2-EXIT-4 slot-delta rug checks).

        Args:
            commitment: Commitment level for the read. Defaults to
                "processed" so the slot advances with the frontier the
                listeners see, not the finalized chain.

        Returns:
            Current slot, or None if the RPC failed (fails open — a missing
            slot means the rug heuristic cannot judge, not that it fires).
        """
        try:
            await self._rate_limiter.acquire()
            client = await self.get_client()
            response = await self._metered_sdk_rpc(
                "getSlot", client.get_slot(commitment=commitment)
            )
            return int(response.value)
        except Exception:
            logger.exception("get_slot failed")
            return None

    async def get_program_accounts(
        self,
        program_id: Pubkey,
        filters: list[object] | None = None,
        commitment: str = "processed",
    ) -> list[tuple[Pubkey, Account]]:
        """List accounts owned by a program, with optional memcmp filters.

        Args:
            program_id: Program owning the accounts to list.
            filters: solana-py filters (``int`` dataSize entries and
                ``MemcmpOpts`` instances), e.g. the Raydium AmmInfo memcmp
                pair (P2-STRUC-3).
            commitment: Commitment level for the read.

        Returns:
            (pubkey, account) pairs, empty on error (fail open — callers
            that list pools want a clean "no pools" answer on RPC trouble,
            not an exception).
        """
        try:
            await self._rate_limiter.acquire()
            client = await self.get_client()
            response = await self._metered_sdk_rpc(
                "getProgramAccounts",
                client.get_program_accounts(
                    program_id, commitment=commitment, filters=filters
                ),
            )
            return [(acc.pubkey, acc.account) for acc in response.value]
        except Exception:
            logger.exception("get_program_accounts failed")
            return []

    async def get_token_account_balance(
        self, token_account: Pubkey, commitment: str = "confirmed"
    ) -> int:
        """Get token balance for an account.

        Defaults to "confirmed" rather than solana-py's "finalized", which lags
        it: the finalized balance right after a sell is the pre-sell amount, and
        cleanup then builds a burn for tokens the account no longer holds.

        Args:
            commitment: Commitment level for the balance read

        Returns:
            Token balance as integer
        """
        await self._rate_limiter.acquire()
        client = await self.get_client()
        response = await self._metered_sdk_rpc(
            "getTokenAccountBalance",
            client.get_token_account_balance(
                token_account, commitment=commitment
            ),
        )
        if response.value:
            return int(response.value.amount)
        return 0

    async def get_token_accounts_by_owner(
        self, owner: Pubkey, commitment: str = "confirmed"
    ) -> list[dict]:
        """List the owner's token accounts with mint and balance.

        Used by the stuck-token sweeper to find orphaned holdings. Returns a
        list of plain dicts ({mint, token_amount: {amount}}) so callers never
        touch solana-py's nested result objects directly.

        Args:
            owner: Wallet pubkey to scan
            commitment: Commitment level for the read

        Returns:
            List of account summaries; empty on failure or no accounts
        """
        return (
            await self._token_accounts_raw(
                owner, SPL_TOKEN_PROGRAM_ID, commitment=commitment
            )
            or []
        )

    async def get_token_accounts_by_owner_all_programs(
        self, owner: Pubkey, commitment: str = "confirmed", retries: int = 2
    ) -> list[dict]:
        """List token accounts across SPL and Token-2022 programs.

        The stuck sweeper needs Token-2022 coverage too: reward coins (and
        older dust like the DICE position) live under TokenzQdBNbLqP...

        Keep the retry budget deliberately small: a second full wallet scan
        after a rate-limited request multiplies provider load. A failed program
        scan is deferred to the next scheduled sweep rather than recursively
        amplified here.
        """
        import asyncio as _asyncio

        combined: list[dict] = []
        for prog in (SPL_TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
            last_error: Exception | None = None
            for attempt in range(retries):
                try:
                    accounts = await self._token_accounts_raw(
                        owner,
                        prog,
                        commitment=commitment,
                        deadline_seconds=8.0,
                    )
                except Exception as error:  # noqa: BLE001
                    last_error = error
                    if attempt < retries - 1:
                        await _asyncio.sleep(3 * (attempt + 1))
                    continue
                if accounts is None:
                    last_error = RuntimeError("token-account RPC returned no response")
                    # post_rpc has already exhausted its bounded retry/deadline
                    # policy and armed the shared provider cooldown on 429.
                    # Do not immediately issue another full scan.
                    break
                combined.extend(accounts)
                break
            else:
                logger.error(
                    f"get_token_accounts_by_owner failed for program {prog} "
                    f"after {retries} attempts: {last_error}"
                )
        return combined

    async def _token_accounts_raw(
        self,
        owner: Pubkey,
        program_id: Pubkey,
        commitment: str = "confirmed",
        *,
        deadline_seconds: float = 8.0,
    ) -> list[dict] | None:
        """Raw jsonParsed token-account scan via post_rpc.

        solana-py's TokenAccountOpts defaults to base64, so
        ``item.account.data`` arrives as raw bytes and ``parsed.get`` crashes
        the stuck-token sweeper (observed live: ``AttributeError: 'bytes'
        object has no attribute 'get'``). ``getTokenAccountsByOwner`` with
        ``encoding: jsonParsed`` returns ``data.parsed.info`` as a plain dict;
        route that through the repo's own post_rpc wrapper (rate limit, 429
        and error retries) so the recovery path cannot be killed by a single
        transient rate limit.

        Returns None only when post_rpc exhausted its retries; an empty wallet
        scan is ``[]`` (distinct from failure so callers can stop retrying).
        """
        response = await self.post_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    str(owner),
                    {"programId": str(program_id)},
                    {"encoding": "jsonParsed", "commitment": commitment},
                ],
            },
            max_retries=2,
            max_429_retries=2,
            deadline_seconds=deadline_seconds,
        )
        if response is None:
            return None
        out: list[dict] = []
        for item in response.get("result", {}).get("value", []):
            data = item.get("account", {}).get("data", {})
            if not isinstance(data, dict):
                continue
            info = data.get("parsed", {}).get("info", {})
            mint = info.get("mint")
            if not mint:
                continue
            amount = info.get("tokenAmount", {}).get("amount", "0")
            out.append({"mint": mint, "token_amount": {"amount": amount}})
        return out

    async def get_latest_blockhash(self) -> Hash:
        """Get the latest blockhash."""
        await self._rate_limiter.acquire()
        client = await self.get_client()
        response = await self._metered_sdk_rpc(
            "getLatestBlockhash",
            client.get_latest_blockhash(commitment="processed"),
        )
        val = response.value
        # C2.2: record lastValidBlockHeight next to the hash — the RPC
        # reports it and the cached-blockhash path uses it (see
        # MAX_BLOCKHASH_FALLBACK_AGE_S) to refuse signing a hash that can
        # no longer land.
        if val is not None:
            self._latest_blockhash = val.blockhash
            self._latest_blockhash_height = getattr(
                val, "last_valid_block_height", None
            )
        return val.blockhash

    async def build_and_send_transaction(
        self,
        instructions: list[Instruction],
        signer_keypair: Keypair,
        skip_preflight: bool = True,
        max_retries: int = 3,
        priority_fee: int | None = None,
        compute_unit_limit: int | None = None,
        account_data_size_limit: int | None = None,
    ) -> Signature:
        """Send a transaction with optional priority fee and compute unit limit.

        Args:
            instructions: Instructions to include in the transaction.
            signer_keypair: Keypair to sign the transaction.
            skip_preflight: Whether to skip preflight checks.
            max_retries: Maximum number of retry attempts.
            priority_fee: Priority fee in microlamports.
            compute_unit_limit: Compute unit limit. Defaults to 85,000.
            account_data_size_limit: Account data size limit in bytes. Reduces CU
                cost from 16k to ~128 CU; must be the first instruction.

        Returns:
            Transaction signature.
        """
        client = await self.get_client()

        logger.info(
            f"Priority fee in microlamports: {priority_fee if priority_fee else 0}"
        )

        # Add compute budget instructions if applicable
        if (
            priority_fee is not None
            or compute_unit_limit is not None
            or account_data_size_limit is not None
        ):
            fee_instructions = []

            if account_data_size_limit is not None:
                fee_instructions.append(
                    set_loaded_accounts_data_size_limit(account_data_size_limit)
                )
                logger.info(f"Account data size limit: {account_data_size_limit} bytes")

            # Set compute unit limit (use provided value or default to 85,000)
            cu_limit = compute_unit_limit if compute_unit_limit is not None else 85_000
            fee_instructions.append(set_compute_unit_limit(cu_limit))

            # Set priority fee if provided
            if priority_fee is not None:
                fee_instructions.append(set_compute_unit_price(priority_fee))

            instructions = fee_instructions + instructions

        if self.durable_nonce_account:
            # R3-P2-2: durable nonce. Use the nonce account's stored
            # blockhash (valid far beyond 150 blocks) and prepend a nonce
            # advance instruction. The nonce authority is the signer wallet.
            recent_blockhash, nonce_ix = await self._nonce_blockhash(
                self.durable_nonce_account, signer_keypair.pubkey()
            )
            instructions = [nonce_ix] + instructions
        else:
            recent_blockhash = await self.get_cached_blockhash()

        # R3-P2-3: Address Lookup Tables (off by default). Compress static
        # keys through the configured tables so the message is smaller and
        # cheaper to validate; identical instruction data.
        lookup_tables = await self._lookup_tables() if self.alt_addresses else []
        message = MessageV0.try_compile(
            signer_keypair.pubkey(), instructions, lookup_tables, recent_blockhash
        )
        transaction = VersionedTransaction(message, [signer_keypair])

        for attempt in range(max_retries):
            try:
                await self._rate_limiter.acquire()
                tx_opts = TxOptsModel(
                    skip_preflight=skip_preflight, preflight_commitment=Processed
                )
                response = await self._metered_sdk_rpc(
                    "sendTransaction", client.send_transaction(transaction, tx_opts)
                )
                return response.value

            except Exception as e:
                kind = classify_tx_failure(e)
                terminal = kind in {
                    TxFailureKind.INSUFFICIENT_FUNDS,
                    TxFailureKind.BLOCKHASH_EXPIRED,
                }
                if attempt == max_retries - 1 or terminal:
                    if terminal:
                        logger.error(
                            f"Transaction send failed ({kind.value}), not retrying: {e!s}"
                        )
                    else:
                        logger.exception(
                            f"Failed to send transaction after {max_retries} attempts"
                        )
                    raise

                wait_time = 2**attempt
                logger.warning(
                    f"Transaction attempt {attempt + 1} failed ({kind.value}): "
                    f"{e!s}, retrying in {wait_time}s"
                )
                await asyncio.sleep(wait_time)

    async def _nonce_blockhash(
        self, nonce_account_b58: str, authority: Pubkey
    ) -> tuple[Hash, Instruction]:
        """R3-P2-2: read the nonce account's stored blockhash + advance ix.

        The nonce account layout (32-byte authority + 32-byte blockhash at
        offset 4 after the 4-byte state discriminator) is stable; a readable
        account with the expected length is required, else the send fails
        closed (never guess a blockhash).

        Returns:
            (blockhash, nonce_advance_instruction) to prepend to the message.
        """
        nonce_pk = Pubkey.from_string(nonce_account_b58)
        account_info = await self.get_account_info(nonce_pk)
        data = account_info.data if account_info else None
        # NonceAccount: 4-byte version + 32-byte authority + 32-byte blockhash
        # + 8-byte fee calculator.
        if not data or len(data) < 72:
            raise ValueError(
                f"durable nonce account {nonce_account_b58} unreadable or "
                f"malformed (len={len(data) if data else 0}); refusing to guess"
            )
        stored_hash = data[36:68]
        return (
            Hash.from_bytes(stored_hash),
            advance_nonce_account(
                AdvanceNonceAccountParams(nonce_pubkey=nonce_pk, authorized_pubkey=authority)
            ),
        )

    async def _lookup_tables(self) -> list[AddressLookupTableAccount]:
        """R3-P2-3: load configured Address Lookup Tables for the message.

        Best-effort: an unreadable / malformed table is skipped (the message
        still compiles with the remaining tables). The cache TTL avoids
        re-reading the tables on every send.
        """
        loaded: list[AddressLookupTableAccount] = []
        now = monotonic()
        for alt_b58 in self.alt_addresses:
            # 60s cache per table: a hit returns the parsed table without a
            # re-read (the message needs the bytes, not just a freshness
            # stamp, so the cache holds the table itself).
            cached = self._alt_cache.get(alt_b58)
            if cached is not None and now - cached[0] < 60.0:
                loaded.append(cached[1])
                continue
            try:
                pk = Pubkey.from_string(alt_b58)
                info = await self.get_account_info(pk)
                if not info or not info.data:
                    continue
                table = AddressLookupTableAccount.from_bytes(info.data)
                self._alt_cache[alt_b58] = (now, table)
                loaded.append(table)
            except Exception:  # noqa: BLE001
                logger.warning("ALT %s could not be loaded; skipping", alt_b58)
                continue
        return loaded

    async def confirm_transaction(
        self, signature: str | Signature, commitment: str = "confirmed"
    ) -> bool:
        """Wait for transaction confirmation and verify execution success.

        Confirms the transaction landed on-chain, then checks meta.err: a
        transaction can be included in a block and still fail execution.

        Deliberately a bool. Returning :class:`ConfirmationStatus` would be a
        silent trap — every member is truthy, so every existing
        `if await client.confirm_transaction(sig):` would start passing
        unconditionally. Callers that need to tell a revert from an unknown call
        :meth:`confirm_transaction_detailed`.

        Args:
            signature: Transaction signature, base58 string or Signature
            commitment: Confirmation commitment level

        Returns:
            Whether transaction was confirmed AND executed successfully
        """
        status = await self.confirm_transaction_detailed(signature, commitment)
        return status is ConfirmationStatus.SUCCESS

    async def confirm_transaction_detailed(
        self, signature: str | Signature, commitment: str = "confirmed"
    ) -> ConfirmationStatus:
        """Confirm a transaction, distinguishing a revert from an unknown.

        Same work as :meth:`confirm_transaction`, but it reports *why* a
        transaction did not succeed. Resubmitting a non-idempotent transaction
        after a confirmed revert is correct; resubmitting after a lookup that
        never answered can close an already-closed position twice.

        Args:
            signature: Transaction signature, base58 string or Signature
            commitment: Confirmation commitment level

        Returns:
            SUCCESS, REVERTED, or UNCONFIRMED
        """
        # The RPC client rejects a base58 string, and the resulting TypeError
        # would be swallowed by the handler below — reporting "not confirmed"
        # for a transaction that was never actually looked up.
        if isinstance(signature, str):
            try:
                signature = Signature.from_string(signature)
            except ValueError:
                logger.exception(f"Malformed transaction signature: {signature}")
                return ConfirmationStatus.UNCONFIRMED

        # solana-py's confirm_transaction performs its own repeated
        # getSignatureStatuses (and sometimes getBlockHeight) calls without
        # going through this application's token bucket. Poll through post_rpc
        # instead so every confirmation read is metered and 429-cooldown aware.
        commitment_rank = {
            "processed": 0,
            "confirmed": 1,
            "finalized": 2,
        }.get(commitment, 1)
        deadline = monotonic() + 20.0
        poll_interval = 1.0
        while monotonic() < deadline:
            response = await self.post_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignatureStatuses",
                    "params": [[str(signature)], {"searchTransactionHistory": True}],
                },
                max_retries=2,
                max_429_retries=3,
                deadline_seconds=max(0.0, deadline - monotonic()),
            )
            if response and isinstance(response.get("result"), dict):
                values = response["result"].get("value") or []
                status = values[0] if values else None
                if status is not None:
                    confirmation = status.get("confirmationStatus")
                    rank = {
                        "processed": 0,
                        "confirmed": 1,
                        "finalized": 2,
                    }.get(confirmation, -1)
                    if rank >= commitment_rank:
                        if status.get("err") is not None:
                            return ConfirmationStatus.REVERTED
                        return await self.verify_transaction_status(signature)
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))

        logger.warning(
            "Timed out waiting for transaction %s to reach %s; outcome remains unknown",
            str(signature)[:16],
            commitment,
        )
        return ConfirmationStatus.UNCONFIRMED

    async def verify_transaction_succeeded(self, signature: str | Signature) -> bool:
        """Check whether a landed transaction actually executed successfully.

        Bool wrapper around :meth:`verify_transaction_status`. See
        :meth:`confirm_transaction` for why this does not return the enum.

        Args:
            signature: Transaction signature, base58 string or Signature

        Returns:
            Whether the transaction executed without a program error
        """
        status = await self.verify_transaction_status(signature)
        return status is ConfirmationStatus.SUCCESS

    async def verify_transaction_status(
        self, signature: str | Signature
    ) -> ConfirmationStatus:
        """Read what actually happened to a landed transaction.

        Landing in a block and succeeding are different things: a revert shows up
        in `meta.err`. Split out from :meth:`confirm_transaction` so it can run
        against a transaction that landed some time ago — signature statuses fall
        out of the RPC's recent history, `getTransaction` does not. Use this to
        re-check a transaction whose first confirmation came back UNCONFIRMED.

        Args:
            signature: Transaction signature, base58 string or Signature

        Returns:
            SUCCESS, REVERTED, or UNCONFIRMED
        """
        signature = str(signature)
        result = await self._get_transaction_result(signature)
        if not result:
            logger.warning(
                f"Could not fetch transaction {signature[:16]}... "
                f"to verify execution — treating as unconfirmed"
            )
            return ConfirmationStatus.UNCONFIRMED

        tx_err = result.get("meta", {}).get("err")
        if tx_err:
            detail = _describe_program_error(tx_err)
            logger.error(
                f"Transaction {signature[:16]}... confirmed but failed: {tx_err}"
                + (f" ({detail})" if detail else "")
            )
            return ConfirmationStatus.REVERTED

        return ConfirmationStatus.SUCCESS

    async def _get_transaction_result_cached(
        self,
        signature: str | Signature,
        budget_seconds: float = TX_RESULT_RETRY_BUDGET,
    ) -> dict | None:
        """Fetch a transaction result, reusing a recent one when available.

        The confirm path already fetched it (see `_get_transaction_result`):
        parsers like :meth:`get_buy_transaction_details` and
        :meth:`get_transaction_token_balance` call this instead so a trade
        that confirms then parses does not pay two getTransaction RPCs.
        """
        signature = str(signature)
        cache = getattr(self, "_tx_result_cache", None)
        if cache is not None:
            hit = cache.get(signature)
            if hit is not None and monotonic() - hit[0] < getattr(
                self, "_tx_result_cache_ttl", 60.0
            ):
                return hit[1]
        return await self._get_transaction_result(signature, budget_seconds)

    async def get_transaction_token_balance(
        self, signature: str | Signature, user_pubkey: Pubkey, mint: Pubkey
    ) -> int | None:
        """Get the user's token balance after a transaction from postTokenBalances.

        Args:
            signature: Transaction signature, base58 string or Signature
            user_pubkey: User's wallet public key
            mint: Token mint address

        Returns:
            Token balance (raw amount) after transaction, or None if not found
        """
        result = await self._get_transaction_result_cached(signature)
        if not result:
            return None

        meta = result.get("meta", {})
        post_token_balances = meta.get("postTokenBalances", [])

        user_str = str(user_pubkey)
        mint_str = str(mint)

        for balance in post_token_balances:
            if balance.get("owner") == user_str and balance.get("mint") == mint_str:
                ui_amount = balance.get("uiTokenAmount", {})
                amount_str = ui_amount.get("amount")
                if amount_str:
                    return int(amount_str)

        return None

    async def get_buy_transaction_details(
        self,
        signature: str | Signature,
        mint: Pubkey,
        sol_destination: Pubkey,
        quote_mint: Pubkey | None = None,
    ) -> tuple[int | None, int | None]:
        """Get actual tokens received and quote spent from a buy transaction.

        Reads SOL transferred to the pool/curve from preBalances/postBalances and
        tokens received from the token balance diff. A non-SOL quote asset does
        not show up in lamport balances, so its spend comes from the quote mint's
        token balance deltas instead.

        Args:
            signature: Transaction signature, base58 string or Signature
            mint: Token mint address
            sol_destination: Address where SOL is sent (bonding curve for pump.fun,
                           quote_vault for letsbonk)
            quote_mint: Quote mint of the coin. Pass None or wrapped SOL for
                       SOL-paired coins.

        Returns:
            Tuple of (tokens_received_raw, quote_spent_raw), or (None, None)
        """
        # Normalized up front: the log lines below slice it, which a Signature
        # does not support.
        signature = str(signature)
        result = await self._get_transaction_result_cached(signature)
        if not result:
            return None, None

        meta = result.get("meta", {})

        # Check for transaction execution errors (e.g., MaxLoadedAccountsDataSizeExceeded)
        tx_err = meta.get("err")
        if tx_err:
            detail = _describe_program_error(tx_err)
            logger.error(
                f"Transaction {signature[:16]}... failed with error: {tx_err}"
                + (f" ({detail})" if detail else "")
            )
            return None, None

        # Get tokens received from pre/post token balance diff
        # This works for Token2022 where owner might be different
        tokens_received = self._extract_positive_token_diff(meta, str(mint))
        if tokens_received is not None:
            logger.info(f"Tokens received from tx: {tokens_received}")

        # Non-SOL quote assets move as SPL token transfers, so the lamport
        # deltas below would report only rent/fees. Read the quote spend from
        # the quote mint's token balance deltas: the positive diff is the
        # curve's quote vault receiving what the buyer paid.
        if quote_mint is not None and not is_sol_paired(quote_mint):
            quote_spent = self._extract_positive_token_diff(meta, str(quote_mint))
            if quote_spent is None:
                logger.warning(
                    f"No positive {quote_mint} balance diff found in tx "
                    f"{signature[:16]}...; cannot determine quote spent"
                )
            else:
                logger.info(f"Quote spent from tx: {quote_spent} (mint {quote_mint})")
            return tokens_received, quote_spent

        # Get SOL spent from preBalances/postBalances at sol_destination
        sol_destination_str = str(sol_destination)
        sol_spent = None
        pre_balances = meta.get("preBalances", [])
        post_balances = meta.get("postBalances", [])
        account_keys = (
            result.get("transaction", {}).get("message", {}).get("accountKeys", [])
        )

        for i, key in enumerate(account_keys):
            key_str = key if isinstance(key, str) else key.get("pubkey", "")
            if key_str == sol_destination_str:
                if i < len(pre_balances) and i < len(post_balances):
                    sol_spent = post_balances[i] - pre_balances[i]
                    if sol_spent > 0:
                        logger.info(f"SOL to pool/curve: {sol_spent} lamports")
                    else:
                        logger.warning(
                            f"SOL destination balance change not positive: {sol_spent}"
                        )
                        sol_spent = None
                break

        return tokens_received, sol_spent

    @staticmethod
    def _extract_positive_token_diff(meta: dict, mint_str: str) -> int | None:
        """Find the largest positive token balance change for a mint in a tx.

        Args:
            meta: Transaction meta containing pre/postTokenBalances
            mint_str: Mint address to look for

        Returns:
            Raw positive balance delta, or None if no account gained this mint
        """
        pre_by_idx = {
            b.get("accountIndex"): b for b in meta.get("preTokenBalances", [])
        }
        post_by_idx = {
            b.get("accountIndex"): b for b in meta.get("postTokenBalances", [])
        }

        best: int | None = None
        for idx in set(pre_by_idx) | set(post_by_idx):
            pre = pre_by_idx.get(idx)
            post = post_by_idx.get(idx)

            if (post or pre).get("mint", "") != mint_str:
                continue

            pre_amount = (
                int(pre.get("uiTokenAmount", {}).get("amount", 0)) if pre else 0
            )
            post_amount = (
                int(post.get("uiTokenAmount", {}).get("amount", 0)) if post else 0
            )
            diff = post_amount - pre_amount

            if diff > 0 and (best is None or diff > best):
                best = diff

        return best

    async def _get_transaction_result(
        self,
        signature: str | Signature,
        budget_seconds: float = TX_RESULT_RETRY_BUDGET,
    ) -> dict | None:
        """Fetch transaction result from RPC, retrying while it is not visible.

        A null result is ambiguous: the transaction may not exist, or the node
        answering may be behind the one that confirmed the signature. Retrying
        within a budget separates the two.

        Args:
            signature: Transaction signature, base58 string or Signature
            budget_seconds: Whole wall-clock budget, not just the gap between
                attempts — each lookup is given the time left on it, so a stalled
                endpoint cannot stretch the call past it.

        Returns:
            Transaction result dict or None
        """
        # A Signature is not JSON serializable, so it has to be stringified here
        # rather than relying on every caller to remember.
        signature = str(signature)
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    # Without this the RPC rejects every versioned transaction
                    # with -32015, so meta.err cannot be read and a good trade
                    # reads back as unconfirmed. Must cover v1, not just v0: this
                    # also reads back signatures the bot did not send.
                    "maxSupportedTransactionVersion": 1,
                },
            ],
        }

        deadline = monotonic() + budget_seconds
        attempts = 0
        while True:
            attempts += 1
            # Hand the lookup what is left of the budget: the deadline check
            # below only runs once post_rpc returns, so otherwise the worst case
            # is budget_seconds plus one full post_rpc backoff.
            response = await self.post_rpc(
                body, deadline_seconds=max(0.0, deadline - monotonic())
            )
            result = response.get("result") if response else None
            if result and "meta" in result:
                if attempts > 1:
                    logger.info(
                        f"Transaction {signature[:16]}... became visible on "
                        f"attempt {attempts}"
                    )
                # T2.2: share the fetched result with the parsers so a
                # confirm-then-parse flow does not re-fetch the same tx.
                cache = getattr(self, "_tx_result_cache", None)
                if cache is not None and len(cache) < 64:
                    cache[signature] = (monotonic(), result)
                return result

            if monotonic() + TX_RESULT_RETRY_DELAY > deadline:
                logger.warning(
                    f"Failed to get transaction {signature[:16]}... after "
                    f"{attempts} attempt(s) within {budget_seconds:.1f}s"
                )
                return None

            await asyncio.sleep(TX_RESULT_RETRY_DELAY)

    async def post_rpc(
        self,
        body: dict[str, Any],
        max_retries: int = 3,
        max_429_retries: int = 10,
        deadline_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        """Send a raw RPC request with rate limiting, retry, and 429 handling.

        Attempts and wall time are bounded separately. Without a deadline the
        retry schedule alone can keep one call going for minutes — three error
        retries backing off 1, 2, 4 ... 16s, or ten 429 retries waiting up to 30s
        each and honouring a `Retry-After` header of any size.

        Args:
            body: JSON-RPC request body.
            max_retries: Maximum retry attempts for errors.
            max_429_retries: Maximum retry attempts for 429 rate limits.
            deadline_seconds: Wall-clock budget covering every attempt and every
                backoff between them. `None` bounds attempts only. Leave it unset
                when a slow truthful answer beats a punctual wrong one.

        Returns:
            Parsed JSON response, or None if all attempts fail.
        """
        method = body.get("method", "unknown")
        error_attempts = 0
        rate_limit_attempts = 0
        deadline = _Deadline(deadline_seconds)

        while error_attempts < max_retries:
            if deadline.expired():
                logger.warning(f"RPC request {method} exceeded its {deadline}")
                break

            request_started: float | None = None
            try:
                await self._rate_limiter.acquire()
                session = await self._get_session()
                metrics.incr("rpc.requests")
                metrics.incr(f"rpc.method.{method}")
                request_started = monotonic()

                async with session.post(
                    self.rpc_endpoint,
                    json=body,
                    headers=self._rpc_headers or None,
                ) as response:
                    if response.status == HTTP_TOO_MANY_REQUESTS:
                        metrics.incr("rpc.http_429")
                        if request_started is not None:
                            metrics.record("rpc.latency", monotonic() - request_started)
                            request_started = None
                        rate_limit_attempts += 1
                        total_wait = _retry_after_seconds(
                            response.headers.get("Retry-After"), rate_limit_attempts
                        )
                        # Arm the provider-wide cooldown even when this
                        # particular request has exhausted its retry budget.
                        await self._rate_limiter.defer(total_wait)
                        if rate_limit_attempts >= max_429_retries:
                            logger.error(
                                f"RPC rate limited (429) on {method}, "
                                f"exhausted {max_429_retries} rate-limit retries"
                            )
                            break
                        logger.warning(
                            f"RPC rate limited (429) on {method}, "
                            f"429 retry {rate_limit_attempts}/{max_429_retries}, "
                            f"shared cooldown {total_wait:.1f}s"
                        )
                        if not deadline.allows(total_wait):
                            logger.warning(
                                f"RPC request {method} gave up on its {deadline} "
                                f"(next retry would wait {total_wait:.1f}s)"
                            )
                            break
                        # Re-enter through acquire(); the shared cooldown is
                        # enforced centrally and no concurrent caller can keep
                        # hitting the same throttled provider.
                        continue

                    response.raise_for_status()
                    result = await response.json()
                    metrics.incr("rpc.success")
                    if request_started is not None:
                        metrics.record("rpc.latency", monotonic() - request_started)
                    return result

            except aiohttp.ContentTypeError:
                metrics.incr("rpc.errors")
                if request_started is not None:
                    metrics.record("rpc.latency", monotonic() - request_started)
                logger.exception(f"Failed to decode RPC response for {method}")
                break

            # asyncio.TimeoutError is what aiohttp raises when the request
            # timeout fires, and it is not an aiohttp.ClientError — without it
            # here every RPC timeout propagated out of post_rpc unretried and
            # crashed the caller with an exception whose str() is empty.
            except (TimeoutError, aiohttp.ClientError):
                metrics.incr("rpc.errors")
                if request_started is not None:
                    metrics.record("rpc.latency", monotonic() - request_started)
                error_attempts += 1
                if error_attempts >= max_retries:
                    logger.exception(
                        f"RPC request {method} failed after {max_retries} attempts"
                    )
                    break

                wait_time = min(2 ** (error_attempts - 1), 16)
                jitter = wait_time * random.uniform(0, 0.25)  # noqa: S311
                logger.warning(
                    f"RPC request {method} failed "
                    f"(attempt {error_attempts}/{max_retries}), "
                    f"retrying in {wait_time + jitter:.1f}s"
                )
                if not deadline.allows(wait_time + jitter):
                    logger.warning(
                        f"RPC request {method} gave up on its {deadline} "
                        f"(next retry would wait {wait_time + jitter:.1f}s)"
                    )
                    break
                await asyncio.sleep(wait_time + jitter)

        return None
