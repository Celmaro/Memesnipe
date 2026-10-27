"""R4-P3-1: fragility-scaled position sizing (ME2F; 2512.00377).

ME2F measures memecoin fragility in three dimensions — Volatility Dynamics
(VDS), Whale Dominance (WDS), and Sentiment Amplification (SAS) — and its
action rule is explicitly two-signal: "If VDS and SAS are both high,
tighten risk: reduce exposure, slow position growth."

The trader sizes positions from config (buy_amount); when fragility is
elevated the size is scaled down toward fragility_scale_min. This module
is pure math:

- volatility_score(prices): coefficient-of-variation proxy for VDS, the
  cheapest defensible estimate available from a short price path.
- whale_score(snapshot): gini/top10 proxy for WDS.
- sentiment_score(mint_events): trade-rate volatility proxy for SAS.
- fragility_scale(fragile, scale_min) -> multiplier; fragile is the
  both-high trigger (VDS and SAS both >= their thresholds, per ME2F's
  "spike close together" action bucket). Single-signal spikes do NOT
  scale — the paper's rule, and the verifier's contract.

All inputs are Optional/bytes-like so unknown dims fail open (no scale).
"""

from __future__ import annotations

from statistics import mean, pstdev


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def volatility_score(
    prices: list[float] | None,
    cv_ceiling: float = 0.8,
) -> float | None:
    """VDS proxy: coefficient of variation of a price path, clamped 0..1.

    None for an empty/short path (cannot judge -> no scale). CV saturates
    at ``cv_ceiling`` — a CV of 0.8 (std = 0.8x the mean price) is already
    an extreme swing for a 30-60s observation window, so the default
    ceiling keeps the score discriminative instead of pinning everything
    near zero (a 2-3x pump-and-dump in minutes reads CV ~0.6-1.2).
    """
    if not prices or len(prices) < 3:
        return None
    m = mean(prices)
    if m <= 0:
        return None
    cv = pstdev(prices) / m
    return _clamp01(cv / cv_ceiling)


def whale_score(
    gini: float | None,
    top10_fraction: float | None,
    gini_ceiling: float = 0.8,
    top10_ceiling: float = 0.9,
) -> float | None:
    """WDS proxy: max(gini, top10) normalized. None when both unknown."""
    vals = []
    if gini is not None:
        vals.append(_clamp01(gini / gini_ceiling))
    if top10_fraction is not None:
        vals.append(_clamp01(top10_fraction / top10_ceiling))
    return max(vals) if vals else None


def sentiment_score(
    fills_per_min: list[float] | None,
    rate_ceiling: float = 30.0,
) -> float | None:
    """SAS proxy: mean trade rate normalized. None when unknown."""
    if not fills_per_min:
        return None
    return _clamp01(mean(fills_per_min) / rate_ceiling)


def fragility_scale(
    vds: float | None,
    sas: float | None,
    *,
    scale_min: float = 0.5,
    vds_threshold: float = 0.6,
    sas_threshold: float = 0.6,
) -> float:
    """Position-size multiplier from the ME2F both-high rule.

    Returns ``scale_min`` when VDS AND SAS are both above their thresholds
    (two-score spike — the paper's tighten-risk bucket); 1.0 otherwise
    (single-signal spikes leave size alone; unknown dims fail open).

    ``scale_min`` 1.0 = feature off (flat sizing, current behavior).
    """
    if scale_min >= 1.0:
        return 1.0
    if vds is None or sas is None:
        return 1.0
    if vds >= vds_threshold and sas >= sas_threshold:
        return max(0.05, min(1.0, scale_min))
    return 1.0