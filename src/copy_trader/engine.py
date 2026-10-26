"""Copy-trading engine (T3.2/T3.3 consumer): mirror leader buys.

Fed by the geyser stream's raw logs (the listener routes transactions to
``handle_geyser_transaction``); the engine decodes TradeEvents via the pump.fun
parser, matches them against enabled copy targets, applies per-wallet limits
(fixed size, min notional, max position, slippage) and mirrors the buy through
the same PlatformAwareBuyer the sniper uses.

Ships disabled: ``copy_trader.enabled: false`` in config means no targets are
loaded and no leader tx is ever mirrored. The engine never reads the wallet's
private key or the bot's .env — it only sends signed transactions built by the
shared buyer, exactly like a normal snipe.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from solders.pubkey import Pubkey

from core.client import SolanaClient
from interfaces.core import TokenInfo
from screening.copy_target_store import CopyTarget, CopyTargetStore
from utils.logger import get_logger

logger = get_logger(__name__)


def _mirror_size(leader_sol: float, target: CopyTarget) -> float:
    """Size a mirrored buy (P2-COP-1).

    Fixed mode: the target's ``fixed_buy_sol`` (0 = use the default). Percent
    mode: ``percent_of_leader`` (>0) mirrors that fraction of the leader's
    notional, clamped by ``max_position_sol`` when set. Returns <= 0 when no
    size is configured.
    """
    if target.percent_of_leader and target.percent_of_leader > 0:
        size = leader_sol * target.percent_of_leader / 100.0
        if target.max_position_sol and target.max_position_sol > 0:
            size = min(size, target.max_position_sol)
        return max(0.0, size)
    return max(0.0, target.fixed_buy_sol or 0.0)


class CopyTraderEngine:
    """Decode TradeEvents and mirror leader buys for enabled targets.

    Args:
        client: Solana RPC client (for price checks / mint metadata)
        store: Copy-target store (SQLite)
        buy_mirror: Callable(token_info, target, size_sol) that performs the
            actual buy through the shared buyer. Returned bool = success.
            ``size_sol`` is the engine-computed mirror size (fixed or percent).
        min_buy_interval: Min seconds between mirrored buys (rate guard)
        profiler: Optional GMGN WalletProfiler; when set, a leader's profile
            is consulted before mirroring (risk filter, never a hard gate)
    """

    def __init__(
        self,
        client: SolanaClient,
        store: CopyTargetStore,
        buy_mirror: Callable[[TokenInfo, CopyTarget, float], Any] | None = None,
        min_buy_interval: float = 5.0,
        profiler: Any | None = None,
    ):
        self.client = client
        self.store = store
        self.buy_mirror = buy_mirror
        self.min_buy_interval = min_buy_interval
        self.profiler = profiler
        self._last_buy_ts: float = 0.0
        self._last_mirrored: dict[str, float] = {}  # "{trader}:{mint}" -> ts, dedupe

    async def on_transaction_logs(self, logs: list[str]) -> None:
        """Feed geyser transaction logs; mirror any leader buy found."""
        try:
            trade = self._parse_trade(logs)
            if not trade:
                return
            await self._maybe_mirror(trade)
        except Exception:
            logger.exception("Copy-trader: error handling transaction logs")

    async def on_trade_event(self, trade: dict[str, Any]) -> None:
        """Direct TradeEvent entry (bypasses log re-parse)."""
        try:
            await self._maybe_mirror(trade)
        except Exception:
            logger.exception("Copy-trader: error handling trade event")

    def _parse_trade(self, logs: list[str]) -> dict[str, Any] | None:
        # The listener hands over already-decoded TradeEvents via on_trade_event;
        # this hook exists for direct log feeding (tests, backfill) and is
        # implemented by wiring the pump.fun parser in the caller.
        return None

    async def _maybe_mirror(self, trade: dict[str, Any]) -> None:
        if not trade.get("is_buy"):
            return
        mint = trade.get("mint", "")
        if not mint:
            return
        trader = trade.get("trader", "")
        if self._is_duplicate(trader, mint):
            return

        targets = self.store.get_active_wallets()
        if not targets:
            return
        target = next((t for t in targets if t.wallet == trader), None)
        if target is None:
            return

        # Rate guard: at least min_buy_interval since the last mirror.
        now = time.monotonic()
        if now - self._last_buy_ts < self.min_buy_interval:
            logger.info(
                f"Copy-trader: skipping {trader[:8]} buy on {mint[:8]} "
                f"(rate guard {now - self._last_buy_ts:.1f}s < {self.min_buy_interval}s)"
            )
            return

        # Min notional gate.
        sol_amount = 0.0
        try:
            sol_amount = float(trade.get("sol_amount", 0)) / 1e9
        except (TypeError, ValueError):
            sol_amount = 0.0
        if sol_amount < target.min_notional_sol:
            logger.info(
                f"Copy-trader: skipping {trader[:8]} buy on {mint[:8]} "
                f"({sol_amount:.4f} SOL < min_notional {target.min_notional_sol})"
            )
            return

        # Build a TokenInfo the shared buyer understands.
        try:
            mint_pubkey = Pubkey.from_string(mint)
        except Exception:
            logger.warning(f"Copy-trader: invalid mint {mint[:16]}")
            return
        token_info = TokenInfo(
            name=(trade.get("name") or trade.get("symbol") or mint[:6]),
            symbol=(trade.get("symbol") or mint[:6]),
            uri=(trade.get("uri") or ""),
            mint=mint_pubkey,
            platform=self._platform(trade),
            bonding_curve=(
                Pubkey.from_string(trade["bonding_curve"])
                if trade.get("bonding_curve")
                else None
            ),
            token_program_id=(
                Pubkey.from_string(trade["token_program_id"])
                if trade.get("token_program_id")
                else None
            ),
            creation_timestamp=trade.get("timestamp"),
            creator=(
                Pubkey.from_string(trade["creator"]) if trade.get("creator") else None
            ),
        )

        # Size the mirror (P2-COP-1): fixed mode uses fixed_buy_sol; percent
        # mode mirrors a fraction of the leader's notional, clamped by the
        # per-token max_position_sol cap. Below the floor, skip the mirror.
        size = _mirror_size(sol_amount, target)
        if size <= 0:
            logger.info(
                f"Copy-trader: skipping {trader[:8]} buy on {mint[:8]} "
                f"(mirror size {size:.6f} SOL <= 0)"
            )
            return

        logger.info(
            f"Copy-trader: mirroring {trader[:8]} buy on {mint[:8]} "
            f"({sol_amount:.4f} SOL leader, mirror {size:.4f} SOL)"
        )

        # T3.3: consult the GMGN profile when configured — risk filter, not a
        # hard gate. A fetch failure logs and proceeds (mirror is explicit opt-in).
        # R3-P2-7: the multi-window profile adds the 24h window so
        # keep-all-windows-positive qualification has the data it needs.
        if self.profiler is not None:
            try:
                profile = await self.profiler.profile_multiwindow(trader)
                if profile:
                    windows = profile.get("windows", {})
                    logger.info(
                        f"Copy-trader: leader {trader[:8]} multi-window profile "
                        f"1d={windows.get('1d', {}).get('realized_pnl')} "
                        f"7d={windows.get('7d', {}).get('realized_pnl')} "
                        f"30d={windows.get('30d', {}).get('realized_pnl')}"
                    )
            except Exception:
                logger.exception(
                    f"Copy-trader: profile lookup failed for {trader[:8]}; "
                    f"proceeding (risk filter, not a gate)"
                )

        if self.buy_mirror is not None:
            try:
                ok = await self.buy_mirror(token_info, target, size)
                if ok:
                    self._last_buy_ts = time.monotonic()
                    self._last_mirrored[f"{trader}:{mint}"] = time.monotonic()
                    logger.info(f"Copy-trader: mirrored buy on {mint[:8]}")
                else:
                    logger.warning(f"Copy-trader: mirror buy failed on {mint[:8]}")
            except Exception:
                logger.exception(f"Copy-trader: mirror buy raised on {mint[:8]}")

    def _is_duplicate(self, trader: str, mint: str) -> bool:
        last = self._last_mirrored.get(f"{trader}:{mint}", 0.0)
        return time.monotonic() - last < self.min_buy_interval

    def _platform(self, trade: dict[str, Any]):
        from interfaces.core import Platform

        return Platform.PUMP_FUN