"""Wiring regression: metadata-socials gate is actually fed (R3-P2-8).

Audit fix proof: the gate previously constructed MetadataStore and read it in
``_skip_reason``, but NOTHING ever called ``put()`` — the refresher loop was
missing, so when enabled the gate silently fail-opened forever. This verifier
proves the refresher now:
- starts in both single-token and yolo mode when min_metadata_extensions > 0;
- reads the Metaplex metadata PDA + URI JSON for a mint and stores a snapshot
  (put reachable end-to-end);
- fails open (no snapshot) when the metadata account is missing.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.universal_trader import UniversalTrader  # noqa: E402
from screening.metadata_socials import MetadataStore  # noqa: E402
import config_loader  # noqa: E402


def _mk_trader(mint: str) -> UniversalTrader:
    t = object.__new__(UniversalTrader)
    t.stagnation_exit_min = 0  # R4-P1-9 mock default (off)
    t.stagnation_band_pct = 0.02  # R4-P1-9 mock default
    t.max_wash_share = 0.0  # R4-P1-6 mock default
    from screening.wash_lpi import WashLpiDetector as _WLD  # R4-P1-6 mock default
    t._wash_lpi = _WLD()  # R4-P1-6 mock default
    t.max_gini_coefficient = 0.0  # R4-P1-10 mock default
    t.rug_score_threshold = 0.0  # R4-P1-1 mock default
    t.factory_deploy_rate = 0.0  # R4-P1-2 mock default
    t.max_coordinated_supply_share = 0.0  # R4-P1-3 mock default
    t.exit_mode = "tp_sl"  # R4-P1-7 mock default
    t.strategies_ledger = None  # R4-P1-7 mock default
    t.max_tracked_tokens = 50
    t._metadata_store = MetadataStore()  # noqa: SLF001
    t.processed_tokens = {mint}
    t.traded_mints = set()
    t.token_queue = None
    t.min_metadata_extensions = 1
    t._metadata_refresh_task = None  # noqa: SLF001

    async def _get_account_info(*a, **k):
        return _FakeAccount()

    t.solana_client = SimpleNamespace(get_account_info=_get_account_info)
    return t


class _FakeAccount:
    """Real Token Metadata body: key(1) + authority(32) + mint(32) + three
    borsh strings name/symbol/uri. Build one with a real URI."""

    @property
    def data(self):
        name = b"TEST"
        symbol = b"TST"
        uri = b"https://example.com/meta.json"
        body = b"\x04" + b"\x00" * 64
        for s in (name, symbol, uri):
            body += len(s).to_bytes(4, "little") + s
        return body


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("Wiring: metadata-socials gate is fed (R3-P2-8)")
    from solders.keypair import Keypair

    mint_a = str(Keypair().pubkey())

    t = _mk_trader(mint_a)
    async def _fake_fetch(uri: str) -> str:
        return ('{"symbol":"TST","name":"TEST","extensions":{"twitter":"x.com/t","website":"w.io"}}')

    t._fetch_metadata_json = _fake_fetch  # noqa: SLF001
    asyncio.run(t._refresh_one_metadata_snapshot(mint_a))
    snap = t._metadata_store.get(mint_a)
    check(
        "refresher stores a snapshot (put reachable)",
        snap is not None,
        f"-> {snap}",
    )
    check(
        "snapshot carries non-empty extensions",
        snap is not None and snap.non_empty_extensions >= 2,
        f"-> {snap.non_empty_extensions if snap else 'n/a'}",
    )

    # URI extraction from a genuine metadata account body.
    t2 = _mk_trader(mint_a)
    uri = UniversalTrader._extract_metadata_uri(_FakeAccount().data)
    check(
        "URI extracted from metadata account body",
        uri == "https://example.com/meta.json",
        f"-> {uri!r}",
    )

    # Missing metadata account -> fail open (no snapshot, gate allows).
    t3 = _mk_trader(mint_a)

    async def _get_account_info_missing(*a, **k):
        raise ValueError("not found")

    t3.solana_client = SimpleNamespace(
        get_account_info=_get_account_info_missing
    )

    async def _fail_open():
        await t3._refresh_one_metadata_snapshot(mint_a)  # noqa: SLF001
        return t3._metadata_store.get(mint_a)  # noqa: SLF001

    snap3 = asyncio.run(_fail_open())
    check(
        "missing metadata account fails open",
        snap3 is None,
        f"-> {snap3}",
    )

    # Refresher task starts when the gate is on (source-level contract: both
    # mode branches start it, shutdown cancels it).
    src = Path("src/trading/universal_trader.py").read_text()
    starts = src.count("self._metadata_refresh_task = asyncio.create_task(")
    check(
        "refresher started in both modes",
        starts == 2,
        f"-> {starts} start sites",
    )
    check(
        "refresher cancelled on shutdown",
        "meta_task = getattr(self, \"_metadata_refresh_task\", None)"
        in src,
    )
    check(
        "refresher gated on min_metadata_extensions > 0",
        src.count("if self.min_metadata_extensions > 0:") >= 2,
    )

    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML default min_metadata_extensions 0",
        cfg["filters"].get("min_metadata_extensions") == 0,
        f"-> {cfg['filters'].get('min_metadata_extensions')!r}",
    )
    check(
        "CONFIG_VALIDATION_RULES has filters.min_metadata_extensions",
        any(
            r[0] == "filters.min_metadata_extensions"
            for r in config_loader.CONFIG_VALIDATION_RULES
        ),
    )

    if failures:
        print(f"\nMETADATA-FEED FAILED ({len(failures)}): {failures}")
        return 1
    print("\nMetadata-socials gate wiring OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())