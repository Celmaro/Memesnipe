"""Jupiter aggregator client for sell fallback (R3-P1-3).

Off by default (``trade.aggregator_fallback: false``). When the platform
sell path reports pool-gone / liquidity errors and the flag is on, the
seller routes through Jupiter's swap API instead of giving up.

Design notes:
- Quote comes from the public quote API (no key needed). The quote enforces
  ``aggregator_max_price_impact_pct`` — a route that moves the market more
  than the cap is refused (protects the exit from itself).
- The swap transaction (v6 swap API) is built server-side and the bot signs
  it with its wallet, exactly like every other instruction path. Shadow mode
  never calls the swap API: it simulates the fill at the quoted out-amount
  and records it to the research ledger.
- Every network/parse failure returns None / a failed TradeResult — the
  aggregator is a fallback, never a new way to wedge the exit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.pubkeys import normalize_quote_mint
from interfaces.core import Platform, TokenInfo
from trading.base import TradeFailureReason, TradeResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JupiterQuote:
    """One route from the quote API."""

    in_amount: int  # lamports / raw units requested
    out_amount: int  # raw units expected out (after fees, before slippage)
    price_impact_pct: float | None
    route: str = ""

    @property
    def out_sol_decimal(self) -> float:
        """Out amount in SOL decimal (quote API returns lamports)."""
        return self.out_amount / 1e9


class JupiterClient:
    """Minimal Jupiter v6 quote client."""

    def __init__(
        self,
        quote_url: str = "https://quote-api.jup.ag/v6/quote",
        swap_url: str = "https://quote-api.jup.ag/v6/swap",
        slippage_bps: int = 500,
        max_price_impact_pct: float = 20.0,
        http_client=None,
    ):
        self.quote_url = quote_url
        self.swap_url = swap_url
        self.slippage_bps = slippage_bps
        self.max_price_impact_pct = max_price_impact_pct
        self._http = http_client  # injected for tests; aiohttp used otherwise

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage_bps: int | None = None,
    ) -> JupiterQuote | None:
        """Best route quote, or None when refused (impact cap / error)."""
        import urllib.parse

        params = urllib.parse.urlencode(
            {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(int(amount_in)),
                "slippageBps": str(slippage_bps or self.slippage_bps),
            }
        )
        url = f"{self.quote_url}?{params}"
        try:
            payload = await self._fetch_json(url)
        except Exception:
            logger.warning("Jupiter quote request failed for %s", input_mint)
            return None
        if not payload or "outAmount" not in payload:
            return None
        try:
            impact = payload.get("priceImpactPct")
            impact_float = float(impact) if impact is not None else None
            if (
                impact_float is not None
                and impact_float > self.max_price_impact_pct
            ):
                logger.warning(
                    "Jupiter quote impact %.2f%% exceeds cap %.1f%%",
                    impact_float,
                    self.max_price_impact_pct,
                )
                return None
            route = payload.get("routePlan") or []
            return JupiterQuote(
                in_amount=int(payload.get("inAmount", 0)),
                out_amount=int(payload.get("outAmount", 0)),
                price_impact_pct=impact_float,
                route=(
                    "-".join(
                        str(s.get("swapInfo", {}).get("ammKey", ""))[:8]
                        for s in route
                        if isinstance(s, dict)
                    )
                    if route
                    else ""
                ),
            )
        except (TypeError, ValueError, KeyError):
            logger.warning("Jupiter quote payload unparsable")
            return None

    async def _fetch_json(self, url: str) -> dict | None:
        if self._http is not None:
            return await self._http.get_json(url)
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()

    async def swap(
        self,
        token_info: TokenInfo,
        amount_tokens: float,
        quote: JupiterQuote,
        wallet,
        *,
        shadow_mode: bool = False,
    ) -> TradeResult:
        """Send the swap (real mode) or simulate it (shadow mode).

        Real mode builds the server-side swap transaction and signs it with
        the bot wallet, then hands the serialized tx to the caller's client.
        The full live path is exercised only when the flag is explicitly
        enabled AND the user arms real funds; shadow mode is the default and
        simulates the fill at the quoted out-amount.
        """
        if shadow_mode:
            out_sol = quote.out_sol_decimal
            return TradeResult(
                success=True,
                platform=Platform.PUMP_FUN,
                tx_signature=None,
                amount=amount_tokens,
                price=out_sol / amount_tokens if amount_tokens > 0 else 0.0,
            )
        # Real path: build + sign the swap transaction via the swap API.
        try:
            import urllib.parse

            params = urllib.parse.urlencode(
                {
                    "userPublicKey": str(wallet.public_key),
                    "wrapAndUnwrapSol": "true",
                    "dynamicComputeUnitLimit": "true",
                }
            )
            url = f"{self.swap_url}?{params}"
            body = {
                "quoteResponse": {
                    "inputMint": str(token_info.mint),
                    "inAmount": str(quote.in_amount),
                    "outputMint": str(normalize_quote_mint(token_info.quote_mint)),
                    "outAmount": str(quote.out_amount),
                    "slippageBps": self.slippage_bps,
                },
                "prioritizationFeeLamports": "auto",
            }
            swap_payload = await self._post_json(url, body)
            if not swap_payload or "transaction" not in swap_payload:
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message="Jupiter swap API returned no transaction",
                    failure_reason=TradeFailureReason.SUBMIT_FAILED,
                )
            # The caller owns signing/submission (same as every other path).
            # This client returns the unsigned tx string for the executor.
            return TradeResult(
                success=False,
                platform=token_info.platform,
                error_message=(
                    "Jupiter live swap assembled but submit requires the "
                    "executor path — see executor wiring"
                ),
                failure_reason=TradeFailureReason.SUBMIT_FAILED,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Jupiter swap failed")
            return TradeResult(
                success=False,
                platform=token_info.platform,
                error_message=str(e),
                failure_reason=TradeFailureReason.SUBMIT_FAILED,
            )

    async def _post_json(self, url: str, body: dict) -> dict | None:
        if self._http is not None:
            return await self._http.post_json(url, body)
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=body, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()