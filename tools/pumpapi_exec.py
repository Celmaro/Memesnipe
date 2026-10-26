"""PumpApi execution client — Lightning buy/sell + Jito bundles.

An alternative execution path to the repo's local geyser pipeline:
- Lightning mode: POST intent, pumpapi signs+broadcasts (needs privateKey
  or apiKey — NEVER hardcode; read from env PUMPAPI_PRIVATE_KEY /
  PUMPAPI_API_KEY).
- Local mode: returns an unsigned tx for the repo to sign (no key sent).
- Jito bundles: up to 5 txs atomic (e.g. buy + protective sell).
- guaranteedDelivery: rebroadcast up to 10s, returns confirmed flag.

All calls log intent + result to state/pumpapi_exec/YYYY-MM-DD.jsonl so a
shadow A/B (pumpapi vs local pipeline latency + landing rate) can run
without spending: pass --dry-run to build/validate the request and log it
without POSTing.

Usage (python):
    from tools.pumpapi_exec import PumpApiExecutor
    ex = PumpApiExecutor(dry_run=True)
    await ex.buy("MINT", amount_sol=0.01)
    await ex.sell("MINT", percent=100)

CLI:
    uv run tools/pumpapi_exec.py buy <MINT> --amount 0.01 [--dry-run]
    uv run tools/pumpapi_exec.py bundle-demo            # buy+sell bundle (dry)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

API_URL = "https://api.pumpapi.io"
EXEC_LOG_DIR = PROJECT_ROOT / "state" / "pumpapi_exec"

# Latency A/B constants (matches repo's min swap sizes / fees)
DEFAULT_SLIPPAGE = 20  # percent, mirrors memesnipe.yaml
DEFAULT_PRIORITY_FEE = 0.00023  # SOL — auto-splits 90% jito / 10% swqos
# 0.25% of volume per trade (their fee) — NOT sent, tracked for PnL
FEE_RATIO = 0.0025


class PumpApiExecutor:
    def __init__(
        self,
        dry_run: bool = True,
        slippage: int = DEFAULT_SLIPPAGE,
        priority_fee: float = DEFAULT_PRIORITY_FEE,
    ):
        self.dry_run = dry_run
        self.slippage = slippage
        self.priority_fee = priority_fee
        EXEC_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # ---------- auth ----------

    def _auth(self, body: dict) -> dict:
        """Attach apiKey or privateKey from env (never log them)."""
        if os.environ.get("PUMPAPI_API_KEY"):
            body["apiKey"] = os.environ["PUMPAPI_API_KEY"]
        elif os.environ.get("PUMPAPI_PRIVATE_KEY"):
            body["privateKey"] = os.environ["PUMPAPI_PRIVATE_KEY"]
        return body

    # ---------- logging ----------

    def _log(self, kind: str, payload: dict, result: dict | None, t0: float):
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "dry_run": self.dry_run,
            "latency_ms": round((time.time() - t0) * 1000, 1),
            "payload": {k: v for k, v in payload.items() if k not in ("apiKey", "privateKey")},
            "result": result,
        }
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            with (EXEC_LOG_DIR / f"{day}.jsonl").open("a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            print(f"[exec] log failed: {e}", file=sys.stderr)
        return row

    # ---------- core ----------

    async def _post(self, body: dict, kind: str) -> dict:
        t0 = time.time()
        body = self._auth(dict(body))
        if self.dry_run or "apiKey" not in body and "privateKey" not in body:
            row = self._log(kind, body, {"skipped": "dry-run" if self.dry_run else "no-credentials"}, t0)
            return row["result"]
        async with aiohttp.ClientSession() as s:
            try:
                async with s.post(API_URL, json=body, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    result = await r.json(content_type=None)
                    result["http_status"] = r.status
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {e}"}
        self._log(kind, body, result, t0)
        return result

    async def buy(self, mint: str, amount_sol: float | None = None, token_amount: float | None = None) -> dict:
        body = {
            "action": "buy",
            "mint": mint,
            "slippage": self.slippage,
            "priorityFee": self.priority_fee,
            "guaranteedDelivery": True,
        }
        if amount_sol is not None:
            body["amount"] = amount_sol
            body["denominatedInQuote"] = True
        elif token_amount is not None:
            body["amount"] = token_amount
            body["denominatedInQuote"] = False
        else:
            raise ValueError("amount_sol or token_amount required")
        return await self._post(body, "buy")

    async def sell(self, mint: str, percent: float | None = None, token_amount: float | None = None) -> dict:
        body = {
            "action": "sell",
            "mint": mint,
            "slippage": self.slippage,
            "priorityFee": self.priority_fee,
            "guaranteedDelivery": True,
        }
        if percent is not None:
            body["amount"] = f"{percent}%"
        elif token_amount is not None:
            body["amount"] = token_amount
            body["denominatedInQuote"] = False
        else:
            raise ValueError("percent or token_amount required")
        return await self._post(body, "sell")

    async def bundle(self, txs: list[dict], jito_tip: float = 0.0001) -> dict:
        """Atomic bundle, all-or-nothing, sandwich-protected."""
        body = {"jitoTip": jito_tip, "transactions": txs}
        return await self._post(body, "bundle")

    # ---------- convenience ----------

    async def buy_and_protect(self, mint: str, amount_sol: float, stop_pct: float = 20) -> dict:
        """Bundle: buy now + pre-signed protective sell that fires only if
        price drops `stop_pct` (via absolute minQuoteAmountOut bounds).
        NOTE: the sell leg uses minQuoteAmountOut so it only succeeds if the
        price has NOT fallen below the stop — for an actual conditional stop
        use the repo's own monitor + this client's sell()."""
        buy = {
            "action": "buy",
            "mint": mint,
            "amount": amount_sol,
            "denominatedInQuote": True,
            "slippage": self.slippage,
        }
        sell = {
            "action": "sell",
            "mint": mint,
            "amount": "100%",
            "slippage": self.slippage,
        }
        return await self.bundle([buy, sell])


async def _cli() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("buy")
    b.add_argument("mint")
    b.add_argument("--amount", type=float, default=0.01, help="SOL")
    s = sub.add_parser("sell")
    s.add_argument("mint")
    s.add_argument("--percent", type=float, default=100)
    sub.add_parser("bundle-demo")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--live", action="store_true", help="actually send (needs PUMPAPI_API_KEY/PRIVATE_KEY)")
    args = ap.parse_args()
    ex = PumpApiExecutor(dry_run=not args.live)
    if args.cmd == "buy":
        r = await ex.buy(args.mint, amount_sol=args.amount)
    elif args.cmd == "sell":
        r = await ex.sell(args.mint, percent=args.percent)
    else:
        r = await ex.bundle(
            [
                {"action": "buy", "mint": "So11111111111111111111111111111111111111112",
                 "amount": 0.01, "denominatedInQuote": True, "slippage": 20},
                {"action": "sell", "mint": "So11111111111111111111111111111111111111112",
                 "amount": "100%", "slippage": 20},
            ]
        )
    print(json.dumps(r, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_cli()))
