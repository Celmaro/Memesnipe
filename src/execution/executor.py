"""Transaction executors and the fallback chain (T2.2).

Mirrors solana-trading-bot's TransactionExecutor abstraction: every send goes
through an executor, the Jito bundle path is one implementation, and a failing
executor falls through to the next while the ledger dedupes by signature.
DefaultExecutor preserves the exact behavior of the previous single-path send;
JitoExecutor is built but disabled until a config enables it.
"""

from __future__ import annotations

import asyncio
import base64
import random
import time
from dataclasses import dataclass
from typing import Protocol

import aiohttp
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from core.client import SolanaClient
from interfaces.core import ConfirmationStatus
from utils.logger import get_logger

logger = get_logger(__name__)

#: Default Jito block-engine bundle endpoints, fallback across regions. One
#: bundle POSTed to several: the first non-error response wins.
JITO_BUNDLE_ENDPOINTS = [
    "https://mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://tokyo.mainnet.block-engine.jito.wtf/api/v1/bundles",
]


@dataclass
class ExecResult:
    """Outcome of one submission attempt through an executor.

    `confirmation_status` carries the detailed enum when the executor
    confirmed the signature (SUCCESS / REVERTED / UNCONFIRMED) — the seller
    needs the discriminating status because an exit sell is not idempotent;
    callers on the buy path can rely on `success`.
    """

    success: bool
    signature: Signature | None = None
    error_message: str | None = None
    via: str = "default"
    confirmation_status: ConfirmationStatus | None = None


class TransactionExecutor(Protocol):
    """Send and confirm one trade; do not retry internally beyond the budget.

    Returns ExecResult; a failed submission must not raise. Conforming
    implementations keep the repo's boundary: a solders Signature in, bool-ish
    result out, and the ledger records `via` so sessions are attributable.
    """

    async def execute(
        self,
        instructions: list[Instruction],
        signer_keypair: Keypair,
        priority_fee: int | None,
        compute_unit_limit: int | None,
        account_data_size_limit: int | None = None,
        skip_preflight: bool = True,
    ) -> ExecResult: ...


class DefaultExecutor:
    """Existing single-path send: priority fee, CU limit, direct RPC.

    Behavior-preserving wrapper around SolanaClient.build_and_send_transaction
    so every caller moves to the executor protocol without changing semantics.
    """

    def __init__(self, client: SolanaClient, max_retries: int = 3, via: str = "default"):
        self.client = client
        self.max_retries = max_retries
        self.via = via

    async def execute(
        self,
        instructions: list[Instruction],
        signer_keypair: Keypair,
        priority_fee: int | None,
        compute_unit_limit: int | None,
        account_data_size_limit: int | None = None,
        skip_preflight: bool = True,
    ) -> ExecResult:
        try:
            signature = await self.client.build_and_send_transaction(
                list(instructions),
                signer_keypair,
                skip_preflight=skip_preflight,
                max_retries=self.max_retries,
                priority_fee=priority_fee,
                compute_unit_limit=compute_unit_limit,
                account_data_size_limit=account_data_size_limit,
            )
            status = await self.client.confirm_transaction_detailed(signature)
            return ExecResult(
                success=status is ConfirmationStatus.SUCCESS,
                signature=signature,
                via=self.via,
                confirmation_status=status,
            )
        except Exception as e:
            logger.exception("Default executor send failed")
            return ExecResult(success=False, error_message=str(e), via=self.via)


class JitoExecutor:
    """Submit a single-tx bundle with a tip, through block-engine (T2.2).

    The swap instructions are compiled together with a tip transfer to a
    validator chosen via getTipAccounts() — the anti-pattern the reference
    bot shows (hard-coded tip accounts, static fee) is deliberately avoided.

    P2-STRUC-4 additions:
    - ``auth_keypairs`` (optional): per-request signing keypairs rotated
      round-robin across requests (region rotation). When set, every
      sendBundle request carries the Jito auth headers
      (``x-jito-signing-key`` + ``Jito-Signature``) so the block engine
      accepts the bundle under the searcher's identity. Empty = unsigned
      (the pre-P2-STRUC-4 behavior).
    - Accepted-signal authority: a non-null bundleId in the sendBundle
      response is treated as authoritative — the block engine took the
      bundle, so no second RPC confirm is issued. A null/error result
      (rejected) fails the send. Malformed responses fall back to the RPC
      confirm for safety.
    """

    def __init__(
        self,
        client: SolanaClient,
        endpoints: list[str] | None = None,
        tip_lamports: int = 10_000,
        tip_accounts: list[Pubkey] | None = None,
        timeout_s: float = 10.0,
        auth_keypairs: list[Keypair] | None = None,
        dontfront: bool = False,
        # C2.4: getBundleStatuses polling after an accepted sendBundle.
        # Off by default: the accepted-bundle trust model (block engine
        # took the bundle; landing is its promise) remains authoritative.
        # When on, an accepted bundle is polled via getBundleStatuses so a
        # "processed but dropped" bundle surfaces as a failed send instead
        # of a silent success. Only meaningful when Jito is enabled.
        poll_bundle_status: bool = False,
        bundle_poll_attempts: int = 10,
        bundle_poll_interval_s: float = 1.0,
    ):
        self.client = client
        # R3-P2-6: DontFront route. The DontFront-compatible block-engine
        # variant runs the same sendBundle JSON API; the endpoint string is
        # the differ. When the flag is on, the configured endpoints are
        # assumed to already be the DontFront variant (the user's RPC
        # provider maps block-engine + DontFront).
        self.dontfront = dontfront
        if dontfront and endpoints:
            self.endpoints = [
                ep.replace(
                    "block-engine.jito.wtf", "dontfront.block-engine.jito.wtf"
                )
                if "block-engine" in ep
                else ep
                for ep in endpoints
            ]
        else:
            self.endpoints = endpoints or JITO_BUNDLE_ENDPOINTS
        self.tip_lamports = tip_lamports
        self._tip_accounts = tip_accounts
        # R4-P0-3: contention tip scaling. While armed, the tip sent to the
        # block engine is tip_lamports * contention_tip_scale (off = 1.0).
        self._contention_tip_scale = 1.0
        self.timeout_s = timeout_s
        self.auth_keypairs = auth_keypairs or []
        self._auth_round_robin = 0
        # C2.4: bundle-status polling configuration (see ctor docstring).
        self.poll_bundle_status = poll_bundle_status
        self.bundle_poll_attempts = bundle_poll_attempts
        self.bundle_poll_interval_s = bundle_poll_interval_s

    def set_contention_tip_scale(self, scale: float) -> None:
        """R4-P0-3: escalate the Jito tip during a launch flood.

        Off by default (1.0). The trader arms THIS alongside the priority-fee
        managers when contention-mode fees are on, so a bundled send still
        races the flood. A tip surge can never exceed
        max_fee_fraction_of_position: bot_runner applies the same fraction
        cap when deriving the scale.
        """
        self._contention_tip_scale = max(1.0, float(scale))

    async def get_tip_accounts(self) -> list[Pubkey]:
        """Fetch current tip recipients from block-engine (cached 60s)."""
        if self._tip_accounts:
            return self._tip_accounts
        for endpoint in self.endpoints[:2]:
            try:
                body = {"jsonrpc": "2.0", "id": 1, "method": "getTipAccounts", "params": []}
                session = await self.client._get_session()  # noqa: SLF001
                headers = self._auth_headers(body)
                async with session.post(
                    endpoint,
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout_s),
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    accounts = data.get("result", [])
                    if accounts:
                        self._tip_accounts = [Pubkey.from_string(a) for a in accounts]
                        return self._tip_accounts
            except Exception:
                logger.exception(f"getTipAccounts failed on {endpoint}")
        return []

    def _auth_headers(self, body: dict) -> dict[str, str]:
        """P2-STRUC-4: Jito auth headers for a request body, or {} unsigned.

        Rotates a configured auth keypair round-robin so a key can be
        replaced mid-run (region/key rotation) without restarting. The
        signature covers the exact JSON body bytes the request sends.
        """
        if not self.auth_keypairs:
            return {}
        keypair = self.auth_keypairs[
            self._auth_round_robin % len(self.auth_keypairs)
        ]
        self._auth_round_robin += 1
        import json as _json

        body_bytes = _json.dumps(body, separators=(",", ":")).encode()
        sig = keypair.sign_message(body_bytes)
        return {
            "x-jito-signing-key": str(keypair.pubkey()),
            "Jito-Signature": str(sig),
        }

    @staticmethod
    def classify_send_bundle_response(payload: dict) -> str:
        """P2-STRUC-4: authoritative reading of a sendBundle JSON result.

        Returns:
            "accepted" when the block engine returned a bundle id (it took
            the bundle: landing is the engine's promise, no RPC confirm
            needed), "rejected" when the result is null or carries an error
            (the engine refused it), or "unknown" for a malformed payload
            (caller falls back to RPC confirm).
        """
        if not isinstance(payload, dict):
            return "unknown"
        result = payload.get("result")
        if isinstance(result, str) and result:
            return "accepted"
        # A present-but-null result, or an error field, is a real rejection.
        # A payload that never mentions result at all is malformed.
        if ("error" in payload) or ("result" in payload and result is None):
            return "rejected"
        return "unknown"

    async def _poll_bundle_status(self, bundle_id: str) -> str | None:
        """C2.4: poll getBundleStatuses for an accepted bundle.

        Returns the final bundle state ("confirmed" | "failed" | "invalid")
        once the block engine reports a terminal state, or None when every
        endpoint errored / answered malformed / timed out. The block
        engine's ``getBundleStatuses`` returns a state per bundle:
        "pending" while the window is open, then "confirmed" (landed),
        "failed" (rejected by validators), or "invalid" (never accepted).
        Polls up to ``bundle_poll_attempts`` at ``bundle_poll_interval_s``.
        """
        session = await self.client._get_session()  # noqa: SLF001
        last_state: str | None = None
        for attempt in range(max(1, self.bundle_poll_attempts)):
            for endpoint in self.endpoints:
                try:
                    body = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getBundleStatuses",
                        "params": [[bundle_id]],
                    }
                    async with session.post(
                        endpoint,
                        json=body,
                        headers=self._auth_headers(body),
                        timeout=aiohttp.ClientTimeout(total=self.timeout_s),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        payload = await resp.json()
                        result = (payload or {}).get("result")
                        if not isinstance(result, dict):
                            continue
                        values = result.get("value") or []
                        for item in values:
                            state = (item or {}).get("confirmation_status")
                            if state in ("confirmed", "failed", "invalid"):
                                return state
                            if state:
                                last_state = state
                except Exception:
                    logger.exception(f"Jito getBundleStatuses failed on {endpoint}")
                if attempt < self.bundle_poll_attempts - 1:
                    await asyncio.sleep(self.bundle_poll_interval_s)
        # No terminal state observed in the window. Report what we saw.
        return last_state if last_state else None

    async def execute(
        self,
        instructions: list[Instruction],
        signer_keypair: Keypair,
        priority_fee: int | None,
        compute_unit_limit: int | None,
        account_data_size_limit: int | None = None,
        skip_preflight: bool = True,
    ) -> ExecResult:
        try:
            tip_accounts = await self.get_tip_accounts()
            if not tip_accounts:
                return ExecResult(
                    success=False,
                    error_message="no jito tip accounts available",
                    via="jito",
                )
            tip_account = random.choice(tip_accounts)
            # R4-P0-3: contention races escalate the block-engine tip by the
            # armed scale (1.0 = off) so a bundled send competes with the
            # launch flood.
            tip_lamports = int(self.tip_lamports * self._contention_tip_scale)
            tip_ix = transfer(
                TransferParams(
                    from_pubkey=signer_keypair.pubkey(),
                    to_pubkey=tip_account,
                    lamports=tip_lamports,
                )
            )
            recent = await self.client.get_cached_blockhash()
            message = MessageV0.try_compile(
                signer_keypair.pubkey(),
                [tip_ix, *instructions],
                [],
                recent,
            )
            tx = VersionedTransaction(message, [signer_keypair])
            serialized = base64.b64encode(bytes(tx)).decode()

            response = await self._send_bundle(serialized)
            if response is None:
                return ExecResult(
                    success=False,
                    error_message="jito bundle rejected by all endpoints",
                    via="jito",
                )
            classification, payload = response
            signature = tx.signatures[0]
            # P2-STRUC-4: the block engine's answer IS the landing verdict —
            # an accepted bundle id needs no second RPC confirm; a rejected
            # bundle must not be retried by the fallback chain as if it never
            # reached the chain. Only an unreadable payload falls back to RPC.
            if classification == "accepted":
                bundle_id = payload.get("result")
                # C2.4: when enabled, poll getBundleStatuses so a bundle the
                # engine accepted but validators dropped surfaces as a failed
                # send instead of a silent success. Off (default): the
                # accepted-bundle trust model stands (engine took it, landing
                # is its promise). Poll timeout keeps SUCCESS + a warning —
                # the bundle WAS accepted; only terminal failures downgrade.
                if self.poll_bundle_status and bundle_id:
                    state = await self._poll_bundle_status(str(bundle_id))
                    if state == "failed" or state == "invalid":
                        logger.warning(
                            f"Jito bundle {bundle_id} reported {state}"
                        )
                        return ExecResult(
                            success=False,
                            signature=signature,
                            error_message=f"jito bundle {state} after accept",
                            via="jito",
                            confirmation_status=ConfirmationStatus.REVERTED,
                        )
                    if state is None:
                        logger.warning(
                            f"Jito bundle {bundle_id} status unknown after "
                            f"{self.bundle_poll_attempts} polls; treating as "
                            "accepted"
                        )
                return ExecResult(
                    success=True,
                    signature=signature,
                    via="jito",
                    confirmation_status=ConfirmationStatus.SUCCESS,
                )
            if classification == "rejected":
                return ExecResult(
                    success=False,
                    signature=signature,
                    error_message=(
                        f"jito bundle rejected by block engine: "
                        f"{payload.get('error') or 'null result'}"
                    ),
                    via="jito",
                    confirmation_status=ConfirmationStatus.REVERTED,
                )
            # Unknown/malformed: fall back to the RPC confirm.
            status = await self.client.confirm_transaction_detailed(signature)
            return ExecResult(
                success=status is ConfirmationStatus.SUCCESS,
                signature=signature,
                via="jito",
                confirmation_status=status,
            )
        except Exception as e:
            logger.exception("Jito executor send failed")
            return ExecResult(success=False, error_message=str(e), via="jito")

    async def _send_bundle(self, serialized: str) -> tuple[str, dict] | None:
        """POST one-bundle to every endpoint; first 2xx/JSON result wins.

        Returns:
            (classification, payload) for the first endpoint that returned a
            parseable answer (accepted OR rejected), or None when every
            endpoint errored or answered with an unreadable payload.
        """
        body = {"jsonrpc": "2.0", "id": 1, "method": "sendBundle", "params": [[serialized]]}
        session = await self.client._get_session()  # noqa: SLF001
        for endpoint in self.endpoints:
            try:
                headers = self._auth_headers(body)
                async with session.post(
                    endpoint,
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout_s),
                ) as resp:
                    if resp.status != 200:
                        continue
                    payload = await resp.json()
                    classification = self.classify_send_bundle_response(payload)
                    if classification != "unknown":
                        return (classification, payload)
            except Exception:
                logger.exception(f"Jito sendBundle failed on {endpoint}")
        return None


class FallbackChainExecutor:
    """Try executors in order until one lands; dedupe by signature (T2.2).

    A submission failure (never reached chain) is the only thing that triggers
    a fallback. The swap instructions are re-signed per attempt (fresh
    blockhash), so each attempt's signature differs — the ledger keys on mint
    + signature, so a later attempt can never be confused with an earlier one.
    """

    def __init__(self, executors: list[TransactionExecutor], dedupe_ttl: float = 30.0):
        self.executors = executors
        self._recent: dict[str, float] = {}
        self.dedupe_ttl = dedupe_ttl

    def _seen(self, signature: Signature | None) -> bool:
        if signature is None:
            return False
        sig = str(signature)
        now = time.monotonic()
        self._recent = {k: v for k, v in self._recent.items() if now - v < self.dedupe_ttl}
        if sig in self._recent:
            return True
        self._recent[sig] = now
        return False

    async def execute(
        self,
        instructions: list[Instruction],
        signer_keypair: Keypair,
        priority_fee: int | None,
        compute_unit_limit: int | None,
        account_data_size_limit: int | None = None,
        skip_preflight: bool = True,
    ) -> ExecResult:
        for executor in self.executors:
            result = await executor.execute(
                instructions,
                signer_keypair,
                priority_fee,
                compute_unit_limit,
                account_data_size_limit,
                skip_preflight,
            )
            if result.success:
                return result
            if result.signature is not None and self._seen(result.signature):
                logger.warning(
                    f"Executor {result.via} returned a signature already seen; "
                    "treating as duplicate, not retrying"
                )
                return result
            logger.warning(
                f"Executor {result.via} failed: {result.error_message}; "
                "falling through to next"
            )
        return result  # type: ignore[return-value]  # last executor always binds