"""R4-P2-1/2/3 regression: copy-trader hardening.

- R4-P2-1: persistent-ring vetting — a leader whose buys co-fire with the
  same peer wallets across >= ring_cofire_threshold launches is not
  mirrored (2607.02795 union-find method on our own feed; numbers
  withdrawn, method sound).
- R4-P2-2: anti-front-run mirror jitter + split — random delay applied,
  mirrored size split into N quanta preserving total (2601.08641
  defensive mechanisms; not the LLM stack).
- R4-P2-3: leader quality gate — score_leader() ranks a good/bot/wash
  candidate correctly (Dune leaderboard v2 criteria; Baldwin bot filter;
  2601.08641 KOL low-frequency); min_leader_score gates the mirror.

All off by default (0/1).
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import yaml  # noqa: E402

import config_loader  # noqa: E402
from copy_trader.engine import CopyTraderEngine  # noqa: E402
from screening.copy_target_store import CopyTarget, CopyTargetStore  # noqa: E402
from screening.coordination import CoFireTracker  # noqa: E402
from screening.leader_score import LeaderStats, score_leader  # noqa: E402




class _AsyncMirror:
    """Async buy-mirror mock: records sizes, returns True."""

    def __init__(self, sink: list):
        self.sink = sink

    async def __call__(self, token_info, target, size):
        self.sink.append(size)
        return True


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R4-P2-1/2/3: copy-trader hardening (ring vetting / jitter+split / leader score)")

    # 1. Leader score: good KOL ranks above a bot-frequency wallet.
    good = score_leader(
        LeaderStats(
            realized_pnl_quote=5.0,
            tokens_30d=15,
            total_trades_30d=20,
            win_rate=0.6,
            pnl_concentration=0.3,
            realization_ratio=0.8,
        )
    )
    bot = score_leader(
        LeaderStats(
            realized_pnl_quote=50.0,
            tokens_30d=2000,  # 472/week = bot (Baldwin)
            total_trades_30d=3000,
            win_rate=0.5,
            pnl_concentration=0.5,
            realization_ratio=0.5,
        )
    )
    bad = score_leader(
        LeaderStats(
            realized_pnl_quote=-3.0,
            tokens_30d=10,
            total_trades_30d=10,
            win_rate=0.2,
            pnl_concentration=0.9,
            realization_ratio=0.2,
        )
    )
    check("good KOL scores highest", good > bot and good > bad, f"-> {good:.2f} vs {bot:.2f} vs {bad:.2f}")
    check(
        "bot-frequency never tops a selective KOL",
        bot < good,
        f"-> {bot:.2f} < {good:.2f}",
    )
    check(
        "negative PnL scores lowest",
        bad < 0.5,
        f"-> {bad:.2f}",
    )

    # 2. Ring vetting: engine refuses a leader with a persistent peer ring.
    with tempfile.TemporaryDirectory() as tmp:
        store = CopyTargetStore(Path(tmp) / "t.db")
        store.upsert(
            CopyTarget(
                wallet="ring_leader",  # type: ignore[arg-type]
                alias="ring",
                fixed_buy_sol=0.01,
                min_notional_sol=0.0,
            )
        )
        engine = CopyTraderEngine(
            client=None,  # type: ignore[arg-type]
            store=store,
            buy_mirror=lambda ti, t, size: _MirrorResult(size),
            min_buy_interval=0.0,
            ring_cofire_threshold=3,  # veto at >=3 shared launches
        )
        # Feed a genuine (non-pubkey) mint would fail Pubkey.from_string in
        # the engine, so use real base58 mints.
        M1 = "2cDQptKhHHPKhYu9uTvNMWNaGAT9uf4KdiypJbaiyNCm"
        M2 = "2kh3J4ZfU3mwkQVzXaD2R3reeRJSXuYytEV6TEA2kxzU"
        M3 = "cvTYj495HEH6qwJUPXHosWy5Sa8pkQFAJ9weS9CB6bL"
        # Build a ring: the leader + peer co-buy 3 launches.
        for i in range(3):
            engine._cofire.observe_buy(f"mint{i}", "ring_leader")  # noqa: SLF001
            engine._cofire.observe_buy(f"mint{i}", "peer")  # noqa: SLF001
        # Now the leader buys mintX; the engine should veto before mirroring.
        mirrored = []
        engine.buy_mirror = _AsyncMirror(mirrored)

        async def _run():
            await engine.on_trade_event(
                {
                    "is_buy": True,
                    "mint": M1,
                    "trader": "ring_leader",
                    "sol_amount": str(100 * 1e9),
                }
            )

        asyncio.run(_run())
        check(
            "ring leader not mirrored",
            len(mirrored) == 0,
            f"-> {len(mirrored)} mirrors",
        )

    # 3. Independent leader IS mirrored (no ring).
    with tempfile.TemporaryDirectory() as tmp:
        store = CopyTargetStore(Path(tmp) / "t.db")
        store.upsert(
            CopyTarget(
                wallet="solo",  # type: ignore[arg-type]
                alias="solo",
                fixed_buy_sol=0.01,
                min_notional_sol=0.0,
            )
        )
        mirrored2 = []
        engine2 = CopyTraderEngine(
            client=None,  # type: ignore[arg-type]
            store=store,
            buy_mirror=_AsyncMirror(mirrored2),
            min_buy_interval=0.0,
            ring_cofire_threshold=3,
        )

        async def _run2():
            await engine2.on_trade_event(
                {
                    "is_buy": True,
                    "mint": "2XvunU4BFPF5WZLNP1nLyy6BVfsRHoSMDBXY97yLhjx2",
                    "trader": "solo",
                    "sol_amount": str(50 * 1e9),
                }
            )

        asyncio.run(_run2())
        check(
            "independent leader mirrored",
            len(mirrored2) == 1,
            f"-> {len(mirrored2)} mirrors",
        )

    # 4. Split: 2 quanta preserve the total size.
    with tempfile.TemporaryDirectory() as tmp:
        store = CopyTargetStore(Path(tmp) / "t.db")
        store.upsert(
            CopyTarget(
                wallet="solo2",  # type: ignore[arg-type]
                alias="solo2",
                fixed_buy_sol=0.02,
                min_notional_sol=0.0,
            )
        )
        sizes = []
        engine3 = CopyTraderEngine(
            client=None,  # type: ignore[arg-type]
            store=store,
            buy_mirror=_AsyncMirror(sizes),
            min_buy_interval=0.0,
            mirror_split_quanta=2,
        )

        async def _run3():
            await engine3.on_trade_event(
                {
                    "is_buy": True,
                    "mint": "3HSJ7S2sKuaPgn9JCjKE61sx22Lh52sehAiTDnCADZgV",
                    "trader": "solo2",
                    "sol_amount": str(50 * 1e9),
                }
            )

        asyncio.run(_run3())
        check(
            "split into 2 quanta",
            len(sizes) == 2,
            f"-> {len(sizes)} fills",
        )
        check(
            "split preserves total size",
            abs(sum(sizes) - 0.02) < 1e-9,
            f"-> {sum(sizes):.6f}",
        )

    # 5. Config wiring.
    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    ct = cfg.get("copy_trader", {})
    check(
        "YAML ring_cofire_threshold 0",
        ct.get("ring_cofire_threshold") == 0,
        f"-> {ct.get('ring_cofire_threshold')!r}",
    )
    check(
        "YAML mirror_split_quanta 1",
        ct.get("mirror_split_quanta") == 1,
        f"-> {ct.get('mirror_split_quanta')!r}",
    )
    check(
        "YAML min_leader_score 0",
        ct.get("min_leader_score") == 0.0,
        f"-> {ct.get('min_leader_score')!r}",
    )
    for key in (
        "copy_trader.ring_cofire_threshold",
        "copy_trader.mirror_jitter_ms",
        "copy_trader.mirror_split_quanta",
        "copy_trader.min_leader_score",
    ):
        check(
            f"CONFIG_VALIDATION_RULES has {key}",
            any(r[0] == key for r in config_loader.CONFIG_VALIDATION_RULES),
        )
    runner = Path("src/bot_runner.py").read_text()
    engine_src = Path("src/copy_trader/engine.py").read_text()
    check(
        "bot_runner passes ring_cofire_threshold",
        "ring_cofire_threshold=cfg" in runner,
    )
    check(
        "engine has ring veto",
        "persistent ring co-fire" in engine_src,
    )
    check(
        "engine splits into quanta",
        "mirror_split_quanta" in engine_src,
    )

    if failures:
        print(f"\nR4-P2-1/2/3 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nCopy-trader hardening OK")
    return 0


class _MirrorResult:
    """Dummy mirror result (truthy)."""

    def __init__(self, size: float) -> None:
        self.size = size

    def __bool__(self) -> bool:
        return True


if __name__ == "__main__":
    sys.exit(main())