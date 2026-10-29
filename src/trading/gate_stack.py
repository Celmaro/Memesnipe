"""Gate stack (C12.3 decomposition): screening gates for UniversalTrader.

Extracted from ``src/trading/universal_trader.py`` — the creator-cadence hook,
``_skip_reason``, the dynamic pre-buy screening recheck, and the curve gate.
``GateStackMixin`` is mixed into ``UniversalTrader`` so the class keeps its
method surface while the monolith shrinks. No behavior change: all state (gate
thresholds, stores, caches) is read through ``self`` on the composed trader,
exactly as before.

The rug-score/factory wiring strings that ``verify_rug_score_factory.py``
greps for live here now (``compute_rug_score(``, ``is a factory``);
``universal_trader.py`` imports this module.
"""

from __future__ import annotations

import time
from time import monotonic
from typing import Any

from core.pubkeys import normalize_quote_mint
from interfaces.core import TokenInfo
from screening.honeypot import honeypot_reason
from screening.grad_timing import assess as grad_timing_assess  # R4-P1-4
from platforms import get_platform_implementations
from utils.logger import get_logger

logger = get_logger(__name__)


class GateStackMixin:
    """Screening-gate methods mixed into UniversalTrader (C12.3)."""

    def _record_creator_launch(self, token_info: TokenInfo) -> None:
        """C4.3: record one creator launch cadence observation (idempotent).

        Called exactly once per detected mint (guarded by
        `_cadence_recorded_mints`) from `_skip_reason`, so a mint that is
        re-evaluated — detection, backfill, pre-buy recheck — never
        double-counts its creator's launch rate. The factory gate and the
        rug score both read the same trailing-hour window. Gate-off path
        (both gates 0) returns before touching any state, so verifier stubs
        and the default config skip this hook entirely.
        """
        if not (self.factory_deploy_rate > 0 or self.rug_score_threshold > 0):
            return
        creator_key = str(token_info.creator or "") or (
            str(token_info.user) if token_info.user else ""
        )
        mint_key = str(token_info.mint)
        if not creator_key or mint_key in self._cadence_recorded_mints:
            return
        self._cadence_recorded_mints.add(mint_key)
        now = monotonic()
        window = self._creator_launch_ts.setdefault(creator_key, [])
        window.append(now)
        # Keep only the trailing hour (bounded memory).
        cutoff = now - 3600.0
        window[:] = [t for t in window if t >= cutoff]

    def _skip_reason(self, token_info: TokenInfo) -> str | None:
        """Why the configured filters refuse this coin, or None to trade it.

        Checked on detection as well as before the buy: single-token mode would
        otherwise spend its one token on a coin it was never going to buy.
        """
        quote_mint = normalize_quote_mint(token_info.quote_mint)
        if self.blocked_creators and (
            str(token_info.creator) in self.blocked_creators
            or str(token_info.user) in self.blocked_creators
        ):
            return f"creator {token_info.creator or token_info.user} is blocked"
        # P2-STRUC-2: creator whose realized net PnL is non-positive over
        # enough trades goes on the persistent skip list. Only consulted when
        # the feature is enabled (store exists); a missing store fails open.
        if self.creator_pnl_store is not None:
            creator = str(token_info.creator or "") or (
                str(token_info.user) if token_info.user else ""
            )
            if creator and self.creator_pnl_store.is_blacklisted(
                creator,
                min_trades=self.creator_pnl_min_trades,
                max_net_sol=self.creator_pnl_max_net_sol,
            ):
                trades, net = self.creator_pnl_store.get_stats(creator)
                return (
                    f"creator {creator} on PnL blacklist "
                    f"({trades} trades, net {net:.4f} SOL)"
                )
        # R4-P1-2: creator "factory" — too many launches per hour = a
        # mass-deployer (2609.10246); Mazorra (via 2602.13480): most rugs
        # come from wallets with prior disposable-token history. The cadence
        # clock records every detected launch for the creator; a rate above
        # factory_deploy_rate refuses the coin. Off by default (0).
        # C4.3: record the launch observation exactly once. Dynamic gates
        # are evaluated by the helper below, avoiding a second identical
        # factory/rug/coordination/wash pass in this detection call.
        # Lazy import avoids a gate_stack -> universal_trader cycle.
        from trading.universal_trader import UniversalTrader

        UniversalTrader._record_creator_launch(self, token_info)
        if (
            self.allowed_quote_mints is not None
            and quote_mint not in self.allowed_quote_mints
        ):
            return f"quote mint {quote_mint} not in allowed_quote_mints"
        if quote_mint not in self.quote_amounts:
            return f"no buy amount configured for quote mint {quote_mint}"
        if not self.allow_mayhem_mode and getattr(token_info, "is_mayhem_mode", False):
            return "mayhem-mode coin (distinct asset class; allow_mayhem_mode is off)"
        if self.holder_reward_mode == "skip" and getattr(
            token_info, "is_holder_reward", False
        ):
            return "holder-reward coin (holder_reward_mode=skip)"
        if (
            self.max_transfer_fee_bps is not None
            and token_info.transfer_fee_bps > self.max_transfer_fee_bps
        ):
            return (
                f"transfer fee {token_info.transfer_fee_bps} bps exceeds "
                f"max_transfer_fee_bps {self.max_transfer_fee_bps}"
            )
        # Static config gates (creation-time fields; can never change between
        # detection and buy, so they live here, not in the pre-buy recheck).
        return self._dynamic_screening_reason(token_info, quote_mint)

    def _dynamic_screening_reason(
        self, token_info: TokenInfo, quote_mint: Any = None
    ) -> str | None:
        """Re-check the DATA-DEPENDENT gates with a fresh snapshot (R5-P0-2).

        These gates evaluate live observations (holder snapshot, buyer set,
        wash volume, metadata) and can change between detection and buy —
        the audit's P0-2: `_skip_reason()` ran them at detection, then only
        the curve gate re-ran before the buy, so a coin could pass on stale
        (or empty) data. This helper is stateless (no creator-cadence side
        effect) and is safe to call again immediately before submission.

        Parent controls it: the cadence bookkeeping in the factory/rug block
        lives ONLY in `_skip_reason()` so re-evaluation never double-counts.
        C4.3: the pre-buy recheck also re-runs the factory/creator-PnL/
        rug-score gates (fresh cadence window, fresh holder snapshot and
        buyer set, refreshed blacklist) with the same fail-closed contracts.
        """
        if quote_mint is None:
            quote_mint = normalize_quote_mint(token_info.quote_mint)
        # R4-P1-2: creator "factory" recheck (C4.3) — cadence can only
        # grow, so a creator whose rate was under the fence at detection may
        # have crossed it by pre-buy (a sibling launch landed meanwhile).
        # getattr off-defaults so verifier stubs without the attr read as 0.
        if getattr(self, "factory_deploy_rate", 0.0) > 0:
            creator_key = str(token_info.creator or "") or (
                str(token_info.user) if token_info.user else ""
            )
            from screening.rug_score import cadence_mints_per_hour

            window = self._creator_launch_ts.get(creator_key, [])
            cadence = cadence_mints_per_hour(window, monotonic())
            if cadence is not None and cadence > self.factory_deploy_rate:
                return (
                    f"creator {creator_key[:12]}... is a factory "
                    f"({cadence:.1f} mints/hr > {self.factory_deploy_rate})"
                )
        # C4.3: pre-buy recheck of the PnL blacklist — a sibling close may
        # have pushed the creator net-negative since detection.
        if getattr(self, "creator_pnl_store", None) is not None:
            creator = str(token_info.creator or "") or (
                str(token_info.user) if token_info.user else ""
            )
            if creator and getattr(self, "creator_pnl_min_trades", 0):
                trades_now, net_now = self.creator_pnl_store.get_stats(creator)
                if self.creator_pnl_store.is_blacklisted(
                    creator,
                    min_trades=self.creator_pnl_min_trades,
                    max_net_sol=(
                        getattr(self, "creator_pnl_max_net_sol", 0.0) or 0.0
                    ),
                ):
                    return (
                        f"creator {creator} on PnL blacklist "
                        f"({trades_now} trades, net {net_now:.4f} SOL)"
                    )
        # C4.3: pre-buy rug-score recheck with the freshest holder snapshot
        # and buyer set (the same C4.7 fail-closed contract: insufficient
        # coverage refuses rather than waves through).
        if getattr(self, "rug_score_threshold", 0.0) > 0:
            from screening.rug_score import (
                RugScoreInput,
                compute_rug_score,
                rug_score_reason,
            )

            creator_key = str(token_info.creator or "") or (
                str(token_info.user) if token_info.user else ""
            )
            snap = self._holder_store.get(str(token_info.mint))
            buyers = self._rug_buyers.get(str(token_info.mint))
            from screening.rug_score import cadence_mints_per_hour

            window = self._creator_launch_ts.get(creator_key, [])
            cadence_hr = cadence_mints_per_hour(window, monotonic())
            score = compute_rug_score(
                RugScoreInput(
                    creator_cadence_mints_per_hour=cadence_hr,
                    holder_gini=snap.gini if snap else None,
                    top10_fraction=snap.top10_fraction if snap else None,
                    unique_buyers=len(buyers) if buyers else None,
                ),
                min_coverage=0.6,
            )
            if score is None:
                return (
                    "rug score refused (feature coverage below 4/6: "
                    f"cadence {cadence_hr if cadence_hr is not None else 0:.1f}/hr, "
                    f"holders {snap.unique_holders if snap else 0}, "
                    f"buyers {len(buyers) if buyers else 0})"
                )
            reason = rug_score_reason(score, self.rug_score_threshold)
            if reason:
                return (
                    f"{reason} (cadence {cadence_hr if cadence_hr is not None else 0:.1f}/hr, "
                    f"holders {snap.unique_holders if snap else 0}, "
                    f"buyers {len(buyers) if buyers else 0})"
                )
        # R4-P1-3: insider-coordination supply gate (0 = off). Fraction of
        # the mint's early buyers that co-fire across >= 3 OTHER launches is
        # a coordination signal (MELT 36.5% average). Fail-open: no
        # observations -> share 0.
        if self.max_coordinated_supply_share > 0:
            share = self._cofire.coordinated_share(str(token_info.mint))
            if share > self.max_coordinated_supply_share:
                return (
                    f"insider-coordination: {share:.1%} of early buyers "
                    f"co-fire across launches "
                    f"(max_coordinated_supply_share="
                    f"{self.max_coordinated_supply_share:.1%})"
                )
        # R4-P1-6: wash-trade share gate (0 = off). Recent quote volume from
        # wallets that round-trip the same mint = fake volume (2507.01963).
        if self.max_wash_share > 0:
            wash = self._wash_lpi.wash_share(str(token_info.mint))
            if wash > self.max_wash_share:
                return (
                    f"wash-trading: {wash:.1%} of quote volume is "
                    f"round-trip (max_wash_share={self.max_wash_share:.1%})"
                )
        # R3-P1-5: holder-concentration gates (0 = off). Local snapshot lookup
        # only — the background refresher owns the RPC. Fail-open: a mint with
        # no snapshot yet is allowed (the refresher may simply not have run).
        if (
            self.min_unique_holders > 0
            or self.max_top10_concentration > 0
            or self.max_gini_coefficient > 0
        ):
            snap = self._holder_store.get(str(token_info.mint))
            if snap is not None and not snap.passes(
                min_unique_holders=self.min_unique_holders,
                max_top10_concentration=self.max_top10_concentration,
                max_gini=self.max_gini_coefficient,
            ):
                return (
                    f"holder distribution: {snap.unique_holders} unique "
                    f"holders, top-10 {snap.top10_fraction:.1%} of supply, "
                    f"Gini {snap.gini:.3f} "
                    f"(min_unique_holders={self.min_unique_holders}, "
                    f"max_top10_concentration={self.max_top10_concentration}, "
                    f"max_gini_coefficient={self.max_gini_coefficient})"
                )
        # R3-P2-8: metadata socials gate (0 = off). Local snapshot lookup
        # only — the background refresher owns the URI fetch. Fail-open: a
        # mint with no snapshot yet is allowed.
        if self.min_metadata_extensions > 0:
            meta = self._metadata_store.get(str(token_info.mint))
            if meta is not None and (
                meta.non_empty_extensions < self.min_metadata_extensions
            ):
                return (
                    f"metadata has {meta.non_empty_extensions} non-empty "
                    f"extension(s) < min_metadata_extensions "
                    f"{self.min_metadata_extensions}"
                )
        if (
            self.max_transfer_fee_bps is not None
            and token_info.transfer_fee_bps > self.max_transfer_fee_bps
        ):
            return (
                f"transfer fee {token_info.transfer_fee_bps} bps exceeds "
                f"max_transfer_fee_bps {self.max_transfer_fee_bps}"
            )
        return None

    async def _curve_gate_reason(self, token_info: TokenInfo) -> str | None:
        """Why the curve refuses this coin, or None to trade it.

        Applies the optional `max_market_cap_sol`, `min_market_cap_sol` and
        `min_curve_progress_sol` filters. Market cap is price * total supply
        in SOL; curve progress is the SOL actually paid into the curve by
        buyers (real_quote_reserves) — a launch nobody touched sits at ~0.
        Non-SOL-paired coins are left alone because the gates are denominated
        in SOL.
        """
        if (
            self.max_market_cap_sol is None
            and self.min_curve_progress_sol is None
            and self.min_market_cap_sol is None
        ):
            return None
        try:
            implementations = get_platform_implementations(
                token_info.platform, self.solana_client
            )
            curve_manager = implementations.curve_manager
            pool_address = (
                token_info.bonding_curve
                or token_info.pool_state
                or token_info.associated_bonding_curve
            )
            if pool_address is None:
                # C4.1: data-missing branch — curve filters are on (we only
                # reach here with one of the SOL gates configured) but there
                # is no pool to evaluate, so the gate cannot verify the
                # coin. Fail closed under the fail_closed_gates policy,
                # matching the RPC-error branch below.
                if getattr(self, "fail_closed_gates", True):
                    return "curve gate check failed (no pool address) - fail-closed"
                return None
            state = await curve_manager.get_pool_state(pool_address)
            if not state.get("is_sol_paired", False):
                return None
            if state.get("graduated"):
                return "curve already graduated (on PumpSwap)"
            price = state.get("price_per_token")
            total_supply = state.get("token_total_supply", 0)
            if price is None or total_supply <= 0:
                # C4.1: data-missing branch — the gate cannot compute a
                # market cap from a missing price/supply, so it cannot
                # verify the configured filters. Fail closed under the
                # fail_closed_gates policy.
                if getattr(self, "fail_closed_gates", True):
                    return (
                        "curve gate check failed (missing price/supply) - "
                        "fail-closed"
                    )
                return None
            mc_sol = price * (total_supply / 1e6)
            # Improvement #4: breakout mode — buy only after the market cap
            # has risen THROUGH min_market_cap_sol (momentum confirmation),
            # complementing the max cap that rejects already-pumped coins.
            if self.min_market_cap_sol is not None and mc_sol < self.min_market_cap_sol:
                return (
                    f"market cap {mc_sol:.2f} SOL below "
                    f"min_market_cap_sol {self.min_market_cap_sol} (breakout not triggered)"
                )
            if self.max_market_cap_sol is not None and mc_sol > self.max_market_cap_sol:
                return (
                    f"market cap {mc_sol:.2f} SOL exceeds "
                    f"max_market_cap_sol {self.max_market_cap_sol}"
                )
            real_quote = state.get("real_quote_reserves", 0)
            progress_sol = real_quote / 1e9
            if (
                self.min_curve_progress_sol is not None
                and progress_sol < self.min_curve_progress_sol
            ):
                return (
                    f"curve progress {progress_sol:.6f} SOL below "
                    f"min_curve_progress_sol {self.min_curve_progress_sol}"
                )
            # R4-P1-4: graduation-band advisory (log-only). Same `state` read
            # as the progress gate, zero extra RPC: report which historical
            # graduation-outcome band this curve progress falls in so the
            # replay can correlate entries with grad outcomes. NEVER gates.
            if self.grad_timing_enabled and progress_sol > 0:
                timing = grad_timing_assess(progress_sol, self._grad_band_stats)
                logger.info(
                    f"[GRAD-TIMING] {token_info.symbol} progress "
                    f"{progress_sol:.1f} SOL -> band '{timing.band}'"
                    + (
                        f" (hist median {timing.median_multiple}x, "
                        f"nuke {timing.nuke_rate:.0%})"
                        if timing.median_multiple is not None
                        else ""
                    )
                )

            # T2.5 honeypot gates: freeze authority + curve supply-hold ratio.
            # Uses the state already fetched above (no extra curve RPC); the
            # mint read is the one additional call, only when enabled.
            if self.honeypot_freeze_authority or self.min_curve_hold_fraction > 0:
                mint_key = str(token_info.mint)
                # R3-P2-1: warm snapshot lookup — a verdict cached within the
                # TTL is reused without any RPC on the hot path.
                cached = self._honeypot_cache.get(mint_key)
                if (
                    cached is not None
                    and (monotonic() - cached[0]) < self._honeypot_cache_ttl
                ):
                    if cached[1]:
                        return cached[1]
                else:
                    hp_reason = await honeypot_reason(
                        self.solana_client,
                        token_info,
                        state,
                        check_freeze_authority=self.honeypot_freeze_authority,
                        min_curve_hold_fraction=self.min_curve_hold_fraction,
                    )
                    self._honeypot_cache[mint_key] = (
                        monotonic(),
                        hp_reason,
                    )
                    if hp_reason:
                        return hp_reason
        except Exception:
            logger.exception("Curve gate check failed")
            if getattr(self, "fail_closed_gates", True):
                return "curve gate check failed (RPC error) - fail-closed"
            return None
        return None
