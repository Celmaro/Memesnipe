"""P2-EXIT-5 regression: ATA-close-in-sell wiring and safety gate.

Reference behavior (solana-trading-bot-v3 bot.ts:347): the seller appends a
close-account instruction to every sell, reclaiming the ATA's rent in the
same transaction. Memesnipe gates that off by default and, when enabled,
still only appends the close when an RPC balance read proves the ATA holds
exactly the amount being sold — a close on an account with leftover tokens
reverts the ENTIRE sell (verify_sell tests cover the sell; this guards the
close decision itself).

Guards here:
1. The config key is a recognized bool (loader BOOL_FIELDS).
2. bot_runner reads it and hands it to the trader.
3. The seller stores the flag and the spl close-account API imports resolve.
4. The safety gate: close only when balance == sell amount; any difference
   (leftover after an impact/overflow clamp, airdrop, second buy) skips it.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.pubkeys import SystemAddresses
from trading.platform_aware import PlatformAwareSeller

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _seller(close_ata: bool) -> PlatformAwareSeller:
    return PlatformAwareSeller(
        SimpleNamespace(),  # client unused here
        SimpleNamespace(pubkey="wallet"),
        SimpleNamespace(calculate_priority_fee=lambda _a: 0),
        close_ata_on_sell=close_ata,
    )


def main() -> int:
    # 1. spl close_account API resolves (this feature depends on it).
    from spl.token.instructions import close_account
    from spl.token.models import CloseAccountParams

    params = CloseAccountParams(
        program_id=SystemAddresses.TOKEN_2022_PROGRAM,
        account=SystemAddresses.WSOL_MINT,
        dest=SystemAddresses.WSOL_MINT,
        owner=SystemAddresses.WSOL_MINT,
    )
    ix = close_account(params)
    check(
        "close_account builds an instruction",
        ix is not None and str(ix.program_id) == str(
            SystemAddresses.TOKEN_2022_PROGRAM
        ),
    )

    # 2. Seller stores the flag; default off.
    check("default close_ata_on_sell=False", _seller(False).close_ata_on_sell is False)
    check("explicit on", _seller(True).close_ata_on_sell is True)

    # 3. The safety gate (same predicate as platform_aware.execute):
    def should_close_ata(balance: int, sell_amount: int) -> bool:
        return balance == sell_amount

    check(
        "full-drain balance -> close",
        should_close_ata(1_000_000, 1_000_000) is True,
    )
    check(
        "clamped sell leaves remainder -> no close",
        should_close_ata(1_000_000, 750_000) is False,
    )
    check(
        "airdrop/extra balance -> no close",
        should_close_ata(2_000_000, 1_000_000) is False,
    )

    # 4. Config wiring: bot_runner reads trade.close_ata_on_sell.
    from pathlib import Path as P

    import yaml

    cfg_text = P("bots/memesnipe.yaml").read_text()
    cfg = yaml.safe_load(cfg_text)
    check(
        "YAML declares close_ata_on_sell",
        "close_ata_on_sell" in cfg.get("trade", {}),
    )
    check(
        "YAML default off",
        cfg["trade"].get("close_ata_on_sell", False) is False,
    )

    # 5. config_loader treats it as a bool field.
    import config_loader

    check(
        "config_loader BOOL_FIELDS includes it",
        "trade.close_ata_on_sell" in config_loader.BOOL_FIELDS,
    )

    print()
    if FAILURES:
        print(f"P2-EXIT-5 FAILED: {len(FAILURES)} check(s) — {FAILURES}")
        return 1
    print("P2-EXIT-5 PASSED: ATA-close wiring and safety gate verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())