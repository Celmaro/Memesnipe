"""Strategies ledger (R4-P1-7): parallel exit-strategy testing for shadow mode.

Shadow mode currently replays one live exit path (the configured
``trade.exit_mode``). The research value of a shadow run is much higher if
every open position is ALSO evaluated against every registered exit
strategy, so a single price path yields one result row per strategy side by
side — "what would tp_sl have done vs staged_trail vs time_based".

This module writes an append-only JSONL ledger
(``shadow_data/strategies/YYYY-MM-DD.jsonl``) with three event classes:

- ``strategy_open``: a shadow position opened; one row with the entry
  context (price, quote, pool snapshot) and every strategy that will be
  simulated against it.
- ``strategy_decide``: one row per strategy per evaluated price mark when
  that strategy WOULD have fired an exit (sell_share / exit_all). Marked
  ``would_=true`` so a replay can compare hypothetical exits against the
  live exit that actually ran.
- ``strategy_close``: the final per-strategy outcome for the position —
  what each strategy's PnL would have been, the simulated sell price, and
  the reason.

Write failures are never fatal (same contract as ShadowDataCapture). The
ledger is only ever active in shadow mode; real-mode runs never construct
it (the caller wires it the same way as ``shadow_capture``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading.trail_exit import TrailDecision, TrailExitState

from research.async_jsonl import AsyncJsonlWriter  # C8.5 buffered writer
from research.cost_model import net_round_trip  # R5-P1-3 / R5-P0-1


def _now() -> str:
    return datetime.now(UTC).isoformat()


# Strategy constructors registry. Each entry builds a fresh per-position
# evaluator callable: (current_price: float) -> TrailDecision.
# Only pure/stateless config here — the ledger stores state per position.


def _tp_sl_evaluator(entry_price: float, **cfg: Any) -> Any:
    """The classic single TP/SL evaluated as a decision maker.

    A TP/SL strategy in the ledger is simply the current production rule:
    exit all at +take_profit_pct or -stop_loss_pct (whichever comes first).
    """
    tp_pct = float(cfg.get("take_profit_pct", 0.20))
    sl_pct = float(cfg.get("stop_loss_pct", 0.20))

    class _TPSL:
        def __init__(self, entry: float) -> None:
            self.entry = entry
            self.tp: float | None = None  # price level that triggered
            self.sl: float | None = None

        def on_tick(self, price: float, hold_seconds: float | None = None) -> TrailDecision:
            if price <= 0:
                return TrailDecision("hold", "no_price", price=0.0)
            # Same semantics as Position.should_exit: TP wins on ties.
            if price >= self.entry * (1 + tp_pct):
                self.tp = price
                return TrailDecision("exit_all", "take_profit", price=price)
            if price <= self.entry * (1 - sl_pct):
                self.sl = price
                return TrailDecision("exit_all", "stop_loss", price=price)
            return TrailDecision("hold", "", price=price)

    return _TPSL(entry_price)


def _staged_trail_evaluator(entry_price: float, **cfg: Any) -> Any:
    """The staged-then-trail rule; wraps TrailExitState as an evaluator."""
    # Fallback defaults mirror the LIVE config (bots/memesnipe.yaml trade.*);
    # a partial spec must not silently evaluate a historical exit policy.
    st = TrailExitState(
        entry_price=entry_price,
        initial_tp_share=float(cfg.get("initial_tp_share", 0.4)),
        initial_tp_pct=float(cfg.get("initial_tp_pct", 0.20)),
        arm_pct=float(cfg.get("arm_pct", 0.10)),
        trail_pct=float(cfg.get("trail_pct", 0.12)),
        hard_stop_pct=float(cfg.get("hard_stop_pct", 0.25)),
    )

    class _StagedSim:
        """Adapter: works when `hold_seconds` is passed by decide()."""

        def __init__(self, state: TrailExitState) -> None:
            self._st = state

        def on_tick(
            self, price: float, hold_seconds: float | None = None
        ) -> TrailDecision:
            return self._st.on_tick(price)

    return _StagedSim(st)


def _time_exit_evaluator(entry_price: float, **cfg: Any) -> Any:
    """R5-P1-2: exit-all after a maximum hold (time control).

    Pure time exit: once ``hold_seconds >= max_hold_s`` the position exits
    regardless of price. Optional ``stagnation_band_pct`` refines the trigger
    (exit only if price stayed within +/-band of entry until the timeout).
    """

    max_hold_s = float(cfg.get("max_hold_s", 300.0))
    band = float(cfg.get("stagnation_band_pct", 0.0))

    class _TimeExit:
        def __init__(self, entry: float, max_hold: float, band: float) -> None:
            self.entry = entry
            self.max_hold = max_hold
            self.band = band

        def on_tick(
            self, price: float, hold_seconds: float | None = None
        ) -> TrailDecision:
            if price <= 0:
                return TrailDecision("hold", "no_price", price=0.0)
            if hold_seconds is None or hold_seconds < self.max_hold:
                return TrailDecision("hold", "", price=price)
            if self.band > 0:
                dev = abs(price / self.entry - 1.0) if self.entry else 0.0
                if dev > self.band:
                    return TrailDecision("hold", "", price=price)
                return TrailDecision("exit_all", "stagnation", price=price)
            return TrailDecision("exit_all", "max_hold", price=price)

    return _TimeExit(entry_price, max_hold_s, band)


class _StrategySpec:
    """One registered strategy: name + evaluator constructor."""

    __slots__ = ("config", "constructor", "name")

    def __init__(self, name: str, constructor: Any, config: dict[str, Any]) -> None:
        self.name = name
        self.constructor = constructor
        self.config = config


class StrategiesLedger:
    """Append-only JSONL writer for parallel strategy simulation."""

    def __init__(
        self,
        dir_path: str | Path = "shadow_data/strategies",
        enabled: bool = True,
        strategies: list[dict[str, Any]] | None = None,
        tp_pct: float = 0.20,
        sl_pct: float = 0.20,
    ) -> None:
        self.dir_path = Path(dir_path)
        self.enabled = enabled
        # Default strategies: the production TP/SL rule + the staged_trail.
        self._tp_pct = tp_pct
        self._sl_pct = sl_pct
        self._specs: list[_StrategySpec] = []
        for spec in strategies or []:
            name = str(spec.get("name", ""))
            if not name:
                raise ValueError("strategy spec needs a name")
            kind = str(spec.get("kind", "staged_trail"))
            cfg = dict(spec.get("config") or {})
            if kind == "tp_sl":
                self._specs.append(_StrategySpec(name, _tp_sl_evaluator, cfg))
            elif kind == "staged_trail":
                self._specs.append(_StrategySpec(name, _staged_trail_evaluator, cfg))
            elif kind == "time_exit":
                # R5-P1-2: time-controlled exits (max_hold_s / stagnation).
                self._specs.append(_StrategySpec(name, _time_exit_evaluator, cfg))
            elif kind == "hold":
                # A hold-everything strategy is the counterfactual baseline
                # (what ignoring exits would have done).
                def _hold(entry_price: float, **_c: Any) -> Any:
                    class _H:
                        def __init__(self) -> None:
                            self.entry = entry_price

                        def on_tick(self, price: float, hold_seconds: float | None = None) -> TrailDecision:
                            return TrailDecision("hold", "", price=price)

                    return _H()

                self._specs.append(_StrategySpec(name, _hold, cfg))
            else:
                raise ValueError(f"unknown strategy kind: {kind!r}")
        if not self._specs:
            # Default set: production TP/SL + staged_trail.
            self._specs.append(_StrategySpec("tp_sl", _tp_sl_evaluator, {}))
            self._specs.append(
                _StrategySpec("staged_trail", _staged_trail_evaluator, {})
            )
        # Per-position evaluator instances live here until close.
        self._positions: dict[str, dict[str, Any]] = {}
        # C8.5: buffered persistent-handle writer (one open fd per UTC day,
        # flushed every N rows or 1s) instead of open-per-emit writes on the
        # asyncio loop.
        self._writer = AsyncJsonlWriter(
            dir_path=dir_path, enabled=enabled
        )

    def _emit(self, event_type: str, **fields: Any) -> None:
        """Buffer one row; flush policy handled inside the writer."""
        if not self.enabled:
            return
        # Research data only; the writer swallows write failures so a
        # disk/JSON failure never breaks the shadow trade.
        self._writer.write(event_type, ts=_now(), **fields)

    def flush(self) -> None:
        """Persist buffered rows now (never raises)."""
        self._writer.flush()

    def close(self) -> None:
        """Flush and release the daily file handle (idempotent)."""
        self._writer.close()

    def rotate(self) -> None:
        """Force a daily-file rotation boundary (rows flushed first)."""
        self._writer.rotate()

    def open_position(
        self,
        mint: str,
        symbol: str,
        platform: str,
        entry_price: float,
        amount_tokens: float,
        quote_spent: float,
        pool_snapshot: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        """Register a shadow open; create one evaluator per strategy."""
        if not self.enabled:
            return
        sims: dict[str, Any] = {}
        for spec in self._specs:
            try:
                sims[spec.name] = spec.constructor(entry_price, **spec.config)
            except Exception:
                # A broken strategy must not poison the ledger run.
                continue
        self._positions[mint] = {
            "symbol": symbol,
            "platform": platform,
            "entry_price": entry_price,
            "amount_tokens": amount_tokens,
            "quote_spent": quote_spent,
            "pool_snapshot": pool_snapshot or {},
            "sims": sims,
            "firsts": {},  # strategy name -> first fire (R5-P0-1)
            "extra": extra,
        }
        self._emit(
            "strategy_open",
            mint=mint,
            symbol=symbol,
            platform=platform,
            entry_price=entry_price,
            amount_tokens=amount_tokens,
            quote_spent=quote_spent,
            strategies=[s.name for s in self._specs],
            **extra,
        )

    def decide(self, mint: str, current_price: float, hold_seconds: float) -> None:
        """Evaluate every strategy at one price mark; record would-fires."""
        if not self.enabled:
            return
        pos = self._positions.get(mint)
        if pos is None:
            return
        for name, sim in pos["sims"].items():
            try:
                decision = sim.on_tick(current_price, hold_seconds)  # R5-P1-2: time controls need hold duration
            except TypeError:
                # Older evaluators that only accept price.
                try:
                    decision = sim.on_tick(current_price)
                except Exception:
                    continue
            except Exception:
                continue
            if decision.action in ("sell_share", "exit_all"):
                # R5-P0-1: record the strategy's OWN first exit (price, time,
                # reason, share). Later marks keep emitting decide rows but
                # never overwrite the first fire.
                first = pos["firsts"].setdefault(
                    name,
                    {
                        "first_exit_price": current_price,
                        "first_exit_reason": decision.reason,
                        "first_exit_hold_s": hold_seconds,
                        "first_action": decision.action,
                        "first_share": decision.share,
                    },
                )
                self._emit(
                    "strategy_decide",
                    mint=mint,
                    symbol=pos["symbol"],
                    platform=pos["platform"],
                    strategy=name,
                    action=decision.action,
                    reason=decision.reason,
                    price=current_price,
                    share=decision.share,
                    hold_seconds=hold_seconds,
                    would=True,  # hypothetical: the live path may differ
                    first_exit_price=first["first_exit_price"],
                    first_exit_reason=first["first_exit_reason"],
                    first_exit_hold_s=first["first_exit_hold_s"],
                    first_share=first["first_share"],
                )

    def close_position(
        self,
        mint: str,
        close_price: float,
        exit_reason: str,
        hold_seconds: float,
        **extra: Any,
    ) -> None:
        """Final per-strategy outcome for a closed shadow position.

        C8.2: each strategy's own exit path is simulated, so the emitted
        per-strategy PnL reflects the strategy's own out, not the shared
        live close. A strategy with a recorded first exit (from decide())
        realizes on its OWN first-exit economics: an ``exit_all`` first
        action realizes the whole position at ``first_exit_price``; a
        staged ``sell_share`` first action blends the initial TP leg
        (``first_share`` at ``first_exit_price``) with the remaining share
        at the live close. The live close is kept as the common
        ``pnl_quote``/``pnl_pct`` reference ("would" semantics, unchanged)
        and as ``close_price``/``would_exit_reason``; the strategy-specific
        realized gross and net are emitted separately so downstream can
        verify the per-strategy accounting.
        """
        if not self.enabled:
            return
        pos = self._positions.pop(mint, None)
        if pos is None:
            return
        entry_price = pos["entry_price"] or 1e-30
        amount = pos["amount_tokens"] or 0.0
        quote_spent = pos.get("quote_spent") or (entry_price * amount)
        for name, sim in pos["sims"].items():
            # Final decision at the close price (a strategy that would have
            # exited earlier still reports its FIRST exit, stored by decide;
            # here we report the close-price hypothetical for completeness).
            try:
                final = sim.on_tick(close_price)
            except Exception:
                final = None
            # Live-close reference (C8.2: kept as the uniform "would"
            # baseline for cross-strategy comparison — universal_trader
            # reprices every strategy at this same price).
            pnl_quote = (close_price - pos["entry_price"]) * amount
            pnl_pct = (close_price - pos["entry_price"]) / entry_price
            # R5-P0-1: this strategy's own first exit (recorded by decide()).
            first = pos["firsts"].get(name)
            first_pnl_quote = 0.0
            if first is not None:
                first_pnl_quote = (
                    first["first_exit_price"] - pos["entry_price"]
                ) * amount
            # C8.2: simulate the strategy's OWN exit path on the quote spent.
            # - exit_all first action: full amount exits at first_exit_price.
            # - staged sell_share: initial TP leg (first_share) at the
            #   strategy's first-exit price + remaining share at live close.
            # - no first exit (never fired): realize at the live close.
            first_share: float | None = None
            realized_gross_quote: float
            if first is not None:
                first_price = first["first_exit_price"]
                if first.get("first_action") == "sell_share":
                    share = float(first.get("first_share") or 0.0)
                    share = min(1.0, max(0.0, share))
                    first_share = share
                    # Blend: TP leg at the strategy's own exit price, the
                    # remainder at the live close (the position's last
                    # price, identical for every strategy).
                    realized_gross_quote = (
                        share * amount * (first_price - pos["entry_price"])
                        + (1.0 - share)
                        * amount
                        * (close_price - pos["entry_price"])
                    )
                else:
                    # exit_all: the whole position exits at the strategy's
                    # own first-exit price, not at the live close.
                    first_share = 0.0
                    realized_gross_quote = (
                        amount * (first_price - pos["entry_price"])
                    )
            else:
                realized_gross_quote = (
                    (close_price - pos["entry_price"]) * amount
                )
            realized_gross_pct = (
                realized_gross_quote / quote_spent if quote_spent > 0 else 0.0
            )
            net_pnl_quote = net_round_trip(
                realized_gross_pct, quote_spent
            ) * (quote_spent if quote_spent > 0 else 0.0)
            self._emit(
                "strategy_close",
                mint=mint,
                symbol=pos["symbol"],
                platform=pos["platform"],
                strategy=name,
                close_price=close_price,
                exit_reason=exit_reason,
                would_exit_reason=(
                    final.reason if final is not None else "unavailable"
                ),
                pnl_quote=pnl_quote,
                pnl_pct=pnl_pct,
                hold_seconds=hold_seconds,
                first_exit_price=(
                    first["first_exit_price"] if first is not None else None
                ),
                first_exit_reason=(
                    first["first_exit_reason"] if first is not None else None
                ),
                first_exit_hold_s=(
                    first["first_exit_hold_s"] if first is not None else None
                ),
                first_exit_pnl_quote=first_pnl_quote,
                first_share=first_share,
                realized_gross_pnl_quote=realized_gross_quote,
                realized_gross_pnl_pct=realized_gross_pct,
                net_pnl_quote=net_pnl_quote,
                **extra,
            )

    def summary(self, **fields: Any) -> None:
        """One aggregate row at session end."""
        self._emit("summary", **fields)
