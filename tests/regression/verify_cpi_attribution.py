"""R3-P2-11 regression: CPI outer-signer attribution.

Covers:
- parse_trade_event_from_logs with an outer_signer matching the decoded
  trader: unchanged (direct trade, no router).
- outer_signer differing from the decoded trader: trader corrected to the
  outer signer (aggregator/CPI router case) and attribution tagged.
- No outer_signer: unchanged prior behavior.
"""

import base64
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from platforms.pumpfun.event_parser import PumpFunEventParser  # noqa: E402

HUMAN = None  # set in main via real pubkeys
ROUTER = None  # set in main via real pubkeys


class _FakeIdl:
    def decode_event_data(self, decoded: bytes, name: str) -> dict | None:
        # Event layout we build: 8-byte discriminator + user + mint + is_buy
        # (booleans encoded as bytes 1/0). The real IDL parser yields base58
        # strings for pubkey fields; emulate that here.
        from solders.pubkey import Pubkey

        payload = decoded[8:]
        user = str(Pubkey.from_bytes(payload[:32]))
        mint = str(Pubkey.from_bytes(payload[32:64]))
        is_buy = payload[64] == 1
        return {
            "event_name": "TradeEvent",
            "fields": {
                "user": user,
                "mint": mint,
                "is_buy": is_buy,
                "token_amount": 1000,
                "sol_amount": 1000000,
                "quote_amount": 1000000,
                "fee_basis_points": 25,
                "fee": 0,
                "creator": str(Pubkey.from_bytes(bytes(32))),
                "creator_fee_basis_points": 0,
                "mayhem_mode": False,
                "quote_mint": str(Pubkey.from_bytes(bytes(32))),
                "timestamp": 0,
            },
        }


def _event_log(user_b58: str, is_buy: bool = True) -> str:
    from solders.pubkey import Pubkey

    disc = struct.pack("<Q", 1)
    body = (
        bytes(Pubkey.from_string(user_b58))
        + bytes(Pubkey.from_string("Mint111111111111111111111111111111111111111"))
        + bytes([1 if is_buy else 0])
    )
    return "Program data: " + base64.b64encode(disc + body).decode()


def _parser() -> PumpFunEventParser:
    p = PumpFunEventParser.__new__(PumpFunEventParser)
    p._trade_event_discriminator = 1  # matches our synthetic log
    p._idl_parser = _FakeIdl()
    # _emitted_by_program: emulate "from pump.fun" for every log line.
    p._emitted_by_program = lambda logs: [True] * len(logs)
    return p


def main() -> int:
    global HUMAN, ROUTER  # noqa: PLW0603
    from solders.keypair import Keypair

    HUMAN = str(Keypair().pubkey())
    ROUTER = str(Keypair().pubkey())

    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P2-11 CPI outer-signer attribution")
    p = _parser()

    # Direct trade: decoded trader == outer signer -> unchanged.
    r = p.parse_trade_event_from_logs([_event_log(HUMAN)], outer_signer=HUMAN)
    check(
        "direct trade keeps decoded trader",
        r is not None and r.get("trader") == HUMAN,
        f"-> {r and r.get('trader')}",
    )
    check(
        "direct trade attribution untagged",
        r is not None and "attribution" not in r,
        f"-> {r and r.get('attribution')}",
    )

    # Router trade: decoded trader is the router, outer signer is the human.
    r2 = p.parse_trade_event_from_logs([_event_log(ROUTER)], outer_signer=HUMAN)
    check(
        "router trade corrected to outer signer",
        r2 is not None and r2.get("trader") == HUMAN,
        f"-> {r2 and r2.get('trader')}",
    )
    check(
        "router trade attribution tagged",
        r2 is not None and r2.get("attribution") == "outer_signer",
        f"-> {r2 and r2.get('attribution')}",
    )
    check(
        "outer_signer carried on result",
        r2 is not None and r2.get("outer_signer") == HUMAN,
    )

    # No outer signer: prior behavior unchanged.
    r3 = p.parse_trade_event_from_logs([_event_log(ROUTER)])
    check(
        "no outer signer -> decoded trader kept",
        r3 is not None and r3.get("trader") == ROUTER,
        f"-> {r3 and r3.get('trader')}",
    )

    if failures:
        print(f"\nR3-P2-11 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P2-11 CPI outer-signer attribution OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())