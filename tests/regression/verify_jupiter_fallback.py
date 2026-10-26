"""R3-P1-3 regression: Jupiter aggregator sell fallback.

Covers:
- JupiterClient.get_quote: parses a quote payload, refuses impact above cap.
- Seller.execute: with aggregator_fallback off, a platform SUBMIT_FAILED is
  returned unchanged (no Jupiter call). With it on + a jupiter_client, a
  SUBMIT_FAILED platform sell routes through Jupiter; a REVERTED platform
  failure does NOT (double-sell protection).
- Config wiring: YAML default off, BOOL_FIELDS, validation rules.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from execution.jupiter_client import JupiterClient, JupiterQuote  # noqa: E402
from interfaces.core import Platform, TradeFailureReason  # noqa: E402
from trading.base import TradeResult  # noqa: E402
import config_loader  # noqa: E402


class FakeHttp:
    def __init__(self, payload: dict | None):
        self._payload = payload
        self.post_calls = []

    async def get_json(self, url: str) -> dict | None:
        return self._payload

    async def post_json(self, url: str, body: dict) -> dict | None:
        self.post_calls.append((url, body))
        return {"transaction": "base64-tx"}


async def test_quote_parsing() -> list[str]:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P1-3 Jupiter aggregator fallback")

    http = FakeHttp(
        {"inAmount": "1000000", "outAmount": "1500000", "priceImpactPct": 3.2}
    )
    client = JupiterClient(http_client=http, max_price_impact_pct=20.0)
    q = await client.get_quote("mintA", "mintB", 1_000_000)
    check("quote parsed", q is not None and q.out_amount == 1_500_000, f"-> {q}")
    check(
        "impact under cap kept",
        q is not None and q.price_impact_pct == 3.2,
        f"-> {q.price_impact_pct if q else None}",
    )

    http_bad = FakeHttp({"inAmount": "1000000", "outAmount": "1500000", "priceImpactPct": 55.0})
    client_bad = JupiterClient(http_client=http_bad, max_price_impact_pct=20.0)
    q_bad = await client_bad.get_quote("mintA", "mintB", 1_000_000)
    check(
        "impact above cap refused (None)",
        q_bad is None,
        f"-> {q_bad}",
    )

    http_none = FakeHttp(None)
    client_none = JupiterClient(http_client=http_none)
    q_none = await client_none.get_quote("mintA", "mintB", 1_000_000)
    check("missing payload -> None", q_none is None, f"-> {q_none}")

    return failures


async def test_seller_fallback_selection() -> list[str]:
    failures = []
    from trading.platform_aware import PlatformAwareSeller

    class FakeJupiter:
        def __init__(self):
            self.swap_calls = 0
            self.last_quote = None

        async def get_quote(self, **kwargs):
            self.last_quote = kwargs
            return JupiterQuote(in_amount=1_000_000, out_amount=900_000, price_impact_pct=5.0)

        async def swap(self, token_info, amount_tokens, quote, wallet, shadow_mode=False):
            self.swap_calls += 1
            return TradeResult(success=True, platform=Platform.PUMP_FUN, amount=amount_tokens, price=0.9)

    token = SimpleNamespace(
        mint="Mint111111111111111111111111111111111111111",
        platform=Platform.PUMP_FUN,
        quote_mint="So11111111111111111111111111111111111111112",
    )

    def make_seller(flag: bool, jup=None) -> PlatformAwareSeller:
        s = object.__new__(PlatformAwareSeller)
        s.aggregator_fallback = flag
        s.jupiter_client = jup
        s.aggregator_slippage_bps = 500
        s.aggregator_max_price_impact_pct = 20.0
        s.wallet = None
        return s

    # Off: platform failure returned unchanged, no Jupiter call.
    jup = FakeJupiter()
    s_off = make_seller(False, jup)
    s_off._execute_platform_sell = _coro_result(
        TradeResult(
            success=False,
            platform=Platform.PUMP_FUN,
            error_message="pool gone",
            failure_reason=TradeFailureReason.SUBMIT_FAILED,
        )
    )
    r = await s_off.execute(token, 1.0, 1.0)
    check = _checker(failures)
    check("fallback off: platform failure returned", not r.success and jup.swap_calls == 0)

    # On + SUBMIT_FAILED: routes to Jupiter.
    jup2 = FakeJupiter()
    s_on = make_seller(True, jup2)
    s_on._execute_platform_sell = _coro_result(
        TradeResult(
            success=False,
            platform=Platform.PUMP_FUN,
            error_message="pool gone",
            failure_reason=TradeFailureReason.SUBMIT_FAILED,
        )
    )
    r2 = await s_on.execute(token, 1.0, 1.0)
    check("fallback on: SUBMIT_FAILED routed", r2.success and jup2.swap_calls == 1, f"-> {jup2.swap_calls}")

    # On + REVERTED: do NOT route (double-sell protection).
    jup3 = FakeJupiter()
    s_on3 = make_seller(True, jup3)
    s_on3._execute_platform_sell = _coro_result(
        TradeResult(
            success=False,
            platform=Platform.PUMP_FUN,
            error_message="reverted",
            failure_reason=TradeFailureReason.REVERTED,
        )
    )
    r3 = await s_on3.execute(token, 1.0, 1.0)
    check("fallback on: REVERTED not routed", not r3.success and jup3.swap_calls == 0)

    # On + no jupiter client: platform failure returned.
    jup4 = FakeJupiter()
    s_on4 = make_seller(True, None)
    s_on4._execute_platform_sell = _coro_result(
        TradeResult(
            success=False,
            platform=Platform.PUMP_FUN,
            error_message="pool gone",
            failure_reason=TradeFailureReason.SUBMIT_FAILED,
        )
    )
    r4 = await s_on4.execute(token, 1.0, 1.0)
    check("fallback on but no client: not routed", not r4.success)

    return failures


def _coro_result(result: TradeResult):
    """Return an async stub that yields the given TradeResult."""

    async def _inner(*_a, **_k):
        return result

    return _inner


def _checker(failures: list):
    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)
    return check


def test_config_wiring(failures: list) -> None:
    check = _checker(failures)
    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML default aggregator_fallback false",
        cfg["trade"].get("aggregator_fallback") is False,
        f"-> {cfg['trade'].get('aggregator_fallback')!r}",
    )
    check(
        "BOOL_FIELDS includes trade.aggregator_fallback",
        "trade.aggregator_fallback" in config_loader.BOOL_FIELDS,
    )
    rule_paths = [r[0] for r in config_loader.CONFIG_VALIDATION_RULES]
    check(
        "validation rule for aggregator_slippage_bps",
        "trade.aggregator_slippage_bps" in rule_paths,
    )
    check(
        "validation rule for aggregator_max_price_impact_pct",
        "trade.aggregator_max_price_impact_pct" in rule_paths,
    )


def main() -> int:
    failures = asyncio.run(test_quote_parsing())
    failures += asyncio.run(test_seller_fallback_selection())
    test_config_wiring(failures)
    if failures:
        print(f"\nR3-P1-3 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P1-3 Jupiter aggregator fallback OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
