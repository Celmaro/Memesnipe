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

import asyncio
import json
import time
from pathlib import Path
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
    size is configured. C5.2: the per-token ``max_position_sol`` cap now
    applies to FIXED mode too (previously only percentage mode honored it),
    so a wallet row cannot push a position past its stated risk bound.
    """
    if target.percent_of_leader and target.percent_of_leader > 0:
        size = leader_sol * target.percent_of_leader / 100.0
        if target.max_position_sol and target.max_position_sol > 0:
            size = min(size, target.max_position_sol)
        return max(0.0, size)
    size = target.fixed_buy_sol or 0.0
    if target.max_position_sol and target.max_position_sol > 0:
        size = min(size, target.max_position_sol)
    return max(0.0, size)


def _profile_float(v: Any) -> float | None:
    """Coerce a profile value to float; None when absent/unparseable."""
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _profile_int(v: Any) -> int | None:
    """Coerce a profile value to int; None when absent/unparseable."""
    try:
        if v is None:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


class CopyTraderEngine:
    """Decode TradeEvents and mirror leader buys for enabled targets.

    Args:
        client: Solana RPC client (for price checks / mint metadata)
        store: Copy-target store (SQLite)
        buy_mirror: Callable(token_info, target, size_sol) that performs the
            actual buy through the shared buyer. Returned bool = success.
            ``size_sol`` is the engine-computed mirror size (fixed or percent).
        sell_mirror: Callable(token_info, target, size_sol, reason) that
            closes a mirrored position (real sell in live mode, shadow close
            in shadow mode). Returned bool = success.
        min_buy_interval: Min seconds between mirrored buys (rate guard)
        profiler: Optional GMGN WalletProfiler; when set, a leader's profile
            is consulted before mirroring (risk filter, never a hard gate)
    """

    def __init__(
        self,
        client: SolanaClient,
        store: CopyTargetStore,
        buy_mirror: Callable[[TokenInfo, CopyTarget, float], Any] | None = None,
        sell_mirror: Callable[[TokenInfo, CopyTarget, float, str], Any] | None = None,
        min_buy_interval: float = 5.0,
        profiler: Any | None = None,
        # R4-P2-1: persistent-ring vetting (0 = off). When a target's buy
        # co-fires with the same peer wallets across >= ring_cofire_threshold
        # launches, the leader looks like part of a coordinated ring
        # (2607.02795 union-find method, on our own feed) -> stop mirroring.
        ring_cofire_threshold: int = 0,
        # R4-P2-2: anti-front-run mirror jitter (0 = off) + split (1 = one
        # fill). Jitter randomizes the mirror delay so followers can't front-
        # run the full amount; split divides the mirrored size into quanta
        # fills sent back-to-back (2601.08641 defensive mechanisms).
        mirror_jitter_ms: float = 0.0,
        mirror_split_quanta: int = 1,
        # R4-P2-3: leader quality gate (0 = off). When the profiler returns a
        # profile, score the leader (leader_score module); below this score
        # the mirror is skipped. 0 keeps today's behavior (profile consulted,
        # never a gate).
        min_leader_score: float = 0.0,
        # C5.3: persist the mirrored-position ledger to this JSON path so a
        # restart does not strand open copies (None = legacy in-memory only).
        persist_path: str | None = None,
    ):
        self.client = client
        self.store = store
        self.buy_mirror = buy_mirror
        self.sell_mirror = sell_mirror
        self.min_buy_interval = min_buy_interval
        self.profiler = profiler
        self.ring_cofire_threshold = ring_cofire_threshold
        self.mirror_jitter_ms = mirror_jitter_ms
        self.mirror_split_quanta = max(1, int(mirror_split_quanta))
        self.min_leader_score = min_leader_score
        self._last_buy_ts: float = 0.0
        self._last_mirrored: dict[str, float] = {}  # "{trader}:{mint}" -> ts, dedupe
        # R5-P0-4: open mirrored positions keyed "{trader}:{mint}" -> dict
        # with size (SOL), entry_price, entry_time, so a leader sell event
        # reconciles and exits the copy AND the close can report real PnL.
        # C5.3: persisted to disk (JSON) so a restart does not lose the
        # ledger — an in-memory-only registry stranded positions forever.
        self._mirror_positions: dict[str, dict] = {}
        self._persist_path = Path(persist_path) if persist_path else None
        if self._persist_path is not None:
            self._load_positions()
        # R4-P2-1: co-fire tracker over the engine's own buy observations.
        from screening.coordination import CoFireTracker

        self._cofire = CoFireTracker()

    # ---- C5.3: durable mirror-position ledger ----

    def _load_positions(self) -> None:
        """Restore the mirror ledger from disk (best-effort)."""
        if not self._persist_path:
            return
        try:
            if not self._persist_path.exists():
                return
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            cleaned: dict[str, dict] = {}
            for k, v in data.items():
                if isinstance(v, dict) and "size" in v:
                    cleaned[str(k)] = {
                        "size": float(v.get("size", 0.0) or 0.0),
                        "entry_price": float(v.get("entry_price", 0.0) or 0.0),
                        "entry_time": float(v.get("entry_time", 0.0) or 0.0),
                    }
            self._mirror_positions = cleaned
            if cleaned:
                logger.info(
                    f"Copy-trader: restored {len(cleaned)} open mirror "
                    f"position(s) from {self._persist_path}"
                )
        except Exception:
            logger.exception("Copy-trader: failed to load mirror ledger")

    def _save_positions(self) -> None:
        """Persist the mirror ledger (best-effort, never raises)."""
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._mirror_positions, indent=2), encoding="utf-8"
            )
            tmp.replace(self._persist_path)
        except Exception:
            logger.exception("Copy-trader: failed to save mirror ledger")

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
        mint = trade.get("mint", "")
        if not mint:
            return
        trader = trade.get("trader", "")
        key = f"{trader}:{mint}"
        # R5-P0-4: leader SELL event — reconcile the mirrored position and
        # exit the copy (was previously ignored, stranding positions).
        if not trade.get("is_buy"):
            if key in self._mirror_positions and self.sell_mirror is not None:
                pos = self._mirror_positions.pop(key)
                size = float(pos.get("size", 0.0) or 0.0)
                target = self._target_for(trader)
                # C5.2: honor the target's mode — buy_only targets accumulate
                # the leader's picks and never inherit their exits, so a
                # leader sell must NOT close the mirrored position.
                if target is not None and target.mode == "buy_only":
                    logger.info(
                        f"Copy-trader: {trader[:8]} sell on {mint[:8]} ignored "
                        f"(target mode buy_only; position stays)"
                    )
                    self._mirror_positions[key] = pos
                    self._save_positions()
                    return
                try:
                    ok = await self.sell_mirror(
                        self._token_info_from_trade(trade),
                        target,
                        size,
                        "leader_sell",
                        entry=pos,
                    )
                except Exception:
                    logger.exception(
                        f"Copy-trader: sell mirror raised on {mint[:8]}"
                    )
                    self._mirror_positions[key] = pos  # retry next event
                    self._save_positions()
                    return
                if ok:
                    logger.info(
                        f"Copy-trader: mirrored sell on {mint[:8]} "
                        f"({size:.6f} SOL, leader_sell)"
                    )
                else:
                    logger.warning(
                        f"Copy-trader: mirror sell failed on {mint[:8]}; "
                        f"position stays open"
                    )
                    self._mirror_positions[key] = pos
                    self._save_positions()
            return

        if self._is_duplicate(trader, mint):
            return

        targets = self.store.get_active_wallets()
        if not targets:
            return
        target = next((t for t in targets if t.wallet == trader), None)
        if target is None:
            return
        now = time.monotonic()
        if now - self._last_buy_ts < self.min_buy_interval:
            logger.info(
                f"Copy-trader: skipping {trader[:8]} buy on {mint[:8]} "
                f"(rate guard {now - self._last_buy_ts:.1f}s < {self.min_buy_interval}s)"
            )
            return

        # R4-P2-1: observe this buy in the ring tracker, then veto targets
        # whose buys co-fire with persistent peer rings across too many
        # launches (on OUR feed, where buy/sell labels are trustworthy).
        self._cofire.observe_buy(mint, trader)
        if self.ring_cofire_threshold > 0:
            # A wallet is "ringy" when it has co-fired across >= threshold
            # distinct launches with ANY single peer (counted via the
            # tracker's per-wallet mint sets).
            peer_overlap = 0
            peer_mints = set(self._cofire._wallet_mints.get(trader, set()))  # noqa: SLF001
            for w, wm in self._cofire._wallet_mints.items():  # noqa: SLF001
                if w == trader:
                    continue
                common = len(peer_mints & set(wm))
                if common > peer_overlap:
                    peer_overlap = common
                    if peer_overlap >= self.ring_cofire_threshold:
                        logger.warning(
                            f"Copy-trader: skipping {trader[:8]} buy on "
                            f"{mint[:8]} — persistent ring co-fire "
                            f"(peer {w[:8]} overlaps {peer_overlap} launches; "
                            f"threshold {self.ring_cofire_threshold})"
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
        # R4-P2-3: when min_leader_score > 0, the profile BECOMES a gate —
        # a leader scoring below the threshold is not mirrored.
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
                    # R5-P0-4: wire the qualification module into the live
                    # mirror path (previously exported but never called).
                    # Deny an unqualified leader regardless of min_leader_score
                    # when the profiler has data; the audit's P0-3.
                    if self.min_leader_score > 0:
                        from screening.leader_score import (
                            LeaderStats,
                            score_leader,
                        )

                        stats = LeaderStats(
                            realized_pnl_quote=_profile_float(
                                windows.get("30d", {}).get("realized_pnl")
                            ),
                            tokens_30d=_profile_int(
                                windows.get("30d", {}).get("tokens_traded")
                            ),
                            total_trades_30d=_profile_int(
                                windows.get("30d", {}).get("trades")
                            ),
                            win_rate=_profile_float(
                                windows.get("30d", {}).get("win_rate")
                            ),
                            pnl_concentration=_profile_float(
                                windows.get("30d", {}).get("pnl_concentration")
                            ),
                            realization_ratio=_profile_float(
                                windows.get("30d", {}).get("realization_ratio")
                            ),
                        )
                        score = score_leader(stats)
                        if score < self.min_leader_score:
                            logger.warning(
                                f"Copy-trader: skipping {trader[:8]} buy on "
                                f"{mint[:8]} — leader score {score:.2f} < "
                                f"min_leader_score {self.min_leader_score}"
                            )
                            return
                        logger.info(
                            f"Copy-trader: leader {trader[:8]} score "
                            f"{score:.2f} >= {self.min_leader_score}"
                        )
                    # R5-P0-4: stale/negative leader veto (qualification.py is
                    # the module the audit found unused; wire it as the
                    # quality gate the engine was missing). Baseline rules:
                    # >= MIN_CLOSED_TRADES_30D closes + both 7d/30d windows
                    # profitable + fresh 24h. Off-baseline when the profiler
                    # only returned partial windows.
                    try:
                        from copy_trader.qualification import (
                            LeaderProfile,
                            LeaderVerdict,
                            TradeSummary,
                            qualify_leader,
                        )

                        # C5.1: the profiler emits period keys as '1d'/'7d'/'30d',
                        # but TradeSummary expects integer day windows. Normalize
                        # here so qualification actually runs instead of raising
                        # int('1d') and silently failing open.
                        def _window_days(k: str) -> int:
                            k = str(k).strip()
                            if k.endswith("d") and k[:-1].isdigit():
                                return int(k[:-1])
                            try:
                                return int(k)
                            except (TypeError, ValueError):
                                return 0

                        lp = LeaderProfile(
                            address=trader,
                            last_seen_days_ago=0.0,
                            windows={
                                _window_days(k): TradeSummary(
                                    window_days=_window_days(k),
                                    closed_trades=_profile_int(
                                        w.get("trades")
                                    )
                                    or 0,
                                    net_pnl_sol=_profile_float(
                                        w.get("realized_pnl")
                                    )
                                    or 0.0,
                                )
                                for k, w in windows.items()
                                if w
                            },
                        )
                        verdict = qualify_leader(lp)
                        if verdict is not LeaderVerdict.QUALIFIED:
                            logger.warning(
                                f"Copy-trader: skipping {trader[:8]} buy on "
                                f"{mint[:8]} — qualification {verdict.value}"
                            )
                            return
                    except Exception:
                        # C5.1: fail CLOSED — a broken profile must not turn
                        # the vetting into a pass. Qualification is off when
                        # no windows came back; construction errors are a
                        # signal, not a reason to mirror.
                        logger.exception(
                            "Copy-trader: qualification failed; refusing "
                            "mirror (fail-closed)"
                        )
                        return
            except Exception:
                logger.exception(
                    f"Copy-trader: profile lookup failed for {trader[:8]}; "
                    f"proceeding (risk filter, not a gate)"
                )

        if self.buy_mirror is not None:
            # R4-P2-2: anti-front-run jitter — randomize the mirror delay so
            # a follower of the leader cannot front-run the full amount
            # (2601.08641 defensive mechanism; 0 = off).
            if self.mirror_jitter_ms > 0:
                import random

                delay = random.uniform(0.0, self.mirror_jitter_ms / 1000.0)
                if delay > 0:
                    await asyncio.sleep(delay)
            # R4-P2-2: split the mirror into quanta fills (1 = single fill).
            # Total size preserved; each quanta is a separate mirror call.
            quanta = self.mirror_split_quanta
            per_quantum = size / quanta
            ok = False
            for q in range(quanta):
                try:
                    result = await self.buy_mirror(token_info, target, per_quantum)
                    ok = ok or bool(result)
                    if q < quanta - 1 and self.mirror_jitter_ms > 0:
                        await asyncio.sleep(delay)  # same jitter between quanta
                except Exception:
                    logger.exception(
                        f"Copy-trader: mirror quanta {q + 1}/{quanta} raised "
                        f"on {mint[:8]}"
                    )
            if ok:
                self._last_buy_ts = time.monotonic()
                self._last_mirrored[f"{trader}:{mint}"] = time.monotonic()
                # R5-P0-4: track the open copy so a later leader sell can
                # reconcile it (per-leader/mint size; add-on buys keep the
                # position open at the accumulated size). C5.3: carry entry
                # metadata (price/time) so the close can report real PnL and
                # hold duration instead of zeroed shadow rows.
                now = time.monotonic()
                prev = self._mirror_positions.get(key)
                prev_size = 0.0
                entry_price = 0.0
                entry_time = now
                if isinstance(prev, dict):
                    prev_size = float(prev.get("size", 0.0) or 0.0)
                    entry_price = float(prev.get("entry_price", 0.0) or 0.0)
                    entry_time = float(prev.get("entry_time", now) or now)
                elif isinstance(prev, (int, float)):  # legacy float rows
                    prev_size = float(prev)
                # Prefer the trade's own price when the feed carries it;
                # keep the older entry_price on add-on buys.
                trade_price = trade.get("price") or trade.get("token_price")
                if trade_price:
                    try:
                        entry_price = float(trade_price)
                    except (TypeError, ValueError):
                        pass
                self._mirror_positions[key] = {
                    "size": prev_size + size,
                    "entry_price": entry_price,
                    "entry_time": entry_time,
                }
                self._save_positions()
                logger.info(
                    f"Copy-trader: mirrored buy on {mint[:8]} "
                    f"({quanta} quanta x {per_quantum:.6f} SOL)"
                )
            else:
                logger.warning(f"Copy-trader: mirror buy failed on {mint[:8]}")

    def _target_for(self, trader: str) -> Any | None:
        """Active target for a trader wallet, or None."""
        try:
            targets = self.store.get_active_wallets()
            return next((t for t in targets if t.wallet == trader), None)
        except Exception:
            return None

    def _token_info_from_trade(self, trade: dict[str, Any]) -> Any | None:
        """Build a minimal TokenInfo for the sell callback from a TradeEvent."""
        mint = trade.get("mint", "")
        try:
            token_info = TokenInfo(
                name=(trade.get("name") or trade.get("symbol") or mint[:6]),
                symbol=(trade.get("symbol") or mint[:6]),
                uri=(trade.get("uri") or ""),
                mint=Pubkey.from_string(mint),
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
                    Pubkey.from_string(trade["creator"])
                    if trade.get("creator")
                    else None
                ),
            )
            return token_info
        except Exception:
            return None

    def _is_duplicate(self, trader: str, mint: str) -> bool:
        last = self._last_mirrored.get(f"{trader}:{mint}", 0.0)
        return time.monotonic() - last < self.min_buy_interval

    def _platform(self, trade: dict[str, Any]):
        from interfaces.core import Platform

        return Platform.PUMP_FUN