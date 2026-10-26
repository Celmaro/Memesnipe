"""RPC catch-up backfill after a geyser gap (T2.6).

Ports carbon's RpcTransactionCrawler cursor algorithm: paginate
getSignaturesForAddress with `before`/`until` signature cursors, fetch
transactions with bounded concurrency and backoff, feed each replayed
transaction through the same platform parsers the live stream uses. Triggered
on startup (catch-up since the last checkpoint) and on reconnect gaps so
missed launches and missed exit events are recovered rather than lost.

Replay is buy-eligible only when the freshness gate would have accepted it;
a gap older than that still recovers EXIT events for positions we hold.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from solders.pubkey import Pubkey
from solders.signature import Signature

from core.client import SolanaClient
from utils.logger import get_logger

logger = get_logger(__name__)


class TransactionCrawler:
    """Replay a program's transactions from a signature cursor.

    Args:
        client: Solana RPC client (rate-limited)
        target: Address whose history to crawl (program id or mint)
        fetch_concurrency: Max concurrent getTransaction calls
        page_size: getSignaturesForAddress limit per page
        max_retries: Retries per fetch with exponential backoff
        checkpoint_path: Optional JSON file to persist the last signature
    """

    def __init__(
        self,
        client: SolanaClient,
        target: Pubkey,
        fetch_concurrency: int = 5,
        page_size: int = 100,
        max_retries: int = 3,
        checkpoint_path: Path | None = None,
    ):
        self.client = client
        self.target = target
        self.fetch_concurrency = fetch_concurrency
        self.page_size = page_size
        self.max_retries = max_retries
        self.checkpoint_path = checkpoint_path
        self._most_recent_signature: str | None = None

    async def crawl(
        self,
        parser: Any,
        handle_transaction: Any,
        before: Signature | None = None,
        until: Signature | None = None,
        max_pages: int | None = None,
    ) -> int:
        """Replay transactions through the parser until the cursor runs out.

        Args:
            parser: Platform parser with parse_token_creation_from_logs
            handle_transaction: Callable(update_like) that receives each
                replayed transaction; the caller decides buy/exit handling
            before: Start cursor (oldest-first pagination anchor)
            until: Stop cursor (newest bound, raised after the first pass)
            max_pages: Safety cap on pages crawled

        Returns:
            Number of transactions replayed
        """
        cursor = before
        pages = 0
        replayed = 0
        raise_until = until  # carbon: until-bound raised after first pass

        while max_pages is None or pages < max_pages:
            sigs = await self._fetch_signatures(cursor, raise_until)
            if not sigs:
                break
            pages += 1
            logger.info(f"Crawler: fetched {len(sigs)} signatures (page {pages})")

            sem = asyncio.Semaphore(self.fetch_concurrency)

            async def fetch_one(sig: Signature) -> None:
                nonlocal replayed
                async with sem:
                    tx = await self._fetch_transaction(sig)
                    if tx is None:
                        return
                    await handle_transaction(tx, sig)
                    replayed += 1

            await asyncio.gather(*(fetch_one(s) for s in sigs))

            # Oldest-first: the last signature of this page is the next cursor.
            cursor = sigs[-1]
            if self._most_recent_signature is None:
                self._most_recent_signature = str(sigs[0])
                raise_until = Signature.from_string(self._most_recent_signature)

            if self.checkpoint_path is not None:
                self._save_checkpoint(str(cursor))

        logger.info(f"Crawler: replayed {replayed} transactions total")
        return replayed

    async def _fetch_signatures(
        self, before: Signature | None, until: Signature | None
    ) -> list[Signature]:
        for attempt in range(self.max_retries):
            try:
                await self.client._rate_limiter.acquire()  # noqa: SLF001
                resp = await (await self.client.get_client()).get_signatures_for_address(
                    self.target,
                    before=before,
                    until=until,
                    limit=self.page_size,
                )
                return [s.signature for s in (resp.value or [])]
            except Exception:
                logger.exception(
                    f"getSignaturesForAddress failed (attempt {attempt + 1})"
                )
                await asyncio.sleep(2**attempt)
        return []

    async def _fetch_transaction(self, signature: Signature) -> dict | None:
        """Fetch one transaction as a dict, or None on persistent failure."""
        for attempt in range(self.max_retries):
            try:
                await self.client._rate_limiter.acquire()  # noqa: SLF001
                resp = await (await self.client.get_client()).get_transaction(
                    signature, encoding="json"
                )
                if resp.value is None:
                    return None
                raw = resp.value
                if hasattr(raw, "to_json"):
                    return json.loads(raw.to_json())
                if isinstance(raw, dict):
                    return raw
                return None
            except Exception:
                logger.exception(
                    f"getTransaction failed for {signature} (attempt {attempt + 1})"
                )
                await asyncio.sleep(2**attempt)
        return None

    def _save_checkpoint(self, cursor: str) -> None:
        if self.checkpoint_path is None:
            return
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path.write_text(cursor, encoding="utf-8")


def load_checkpoint(path: Path | None) -> str | None:
    """Read the persisted cursor, or None when absent/empty."""
    if path is None or not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None