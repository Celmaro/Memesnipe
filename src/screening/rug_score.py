"""R4-P1-1: multi-dimensional rug-risk score at entry.

The Solana rug-detection literature (2608.20271: XGBoost on first-5-min
behavioral features, 6.4M tokens; 2608.01609: multi-dimensional feature
design, AUC 0.952) shows a single dimension never separates rugs — the
combination does. This module encodes a *lightweight weighted-sum score*
over the observable features the bot already has at decision time (no new
RPC on the hot path):

- creator cadence (mints/hour — a factory creator is a rug signal,
  2609.10246 + Mazorra's disposable-token finding via 2602.13480)
- curve progress SOL (a stalled launch is a dead launch)
- holder concentration (Gini / top-10 fraction from the snapshot store)
- early buyer set size (few unique buyers = dev-only wash)
- dev balance share (bundle/dev features from 2608.20271's column set)

The score is a weighted sum normalized to 0..1 (higher = riskier). The
trader applies a threshold gate; the calibration path lives in the replay
backtester, so thresholds ship conservative and are tightened by evidence.

This module is pure math — the caller supplies the feature values.
"""

from __future__ import annotations

from dataclasses import dataclass

# Weights default to the literature's emphasis: coordination/behavioral
# features outrank static ones. All weights sum to 1.0.
DEFAULT_WEIGHTS: dict[str, float] = {
    "creator_cadence": 0.25,  # factory / disposable-token history
    "curve_progress": 0.20,  # stalled launch = dead launch
    "holder_concentration": 0.20,  # Gini / top-10 fraction
    "unique_buyers": 0.20,  # dev-only wash
    "dev_balance": 0.15,  # dev retains control of supply
}


@dataclass(frozen=True)
class RugScoreInput:
    """Feature values available at decision time.

    Every feature is Optional: None (or the default 0.0 for floats via
    None semantics below) means UNKNOWN and contributes a benign sub-score
    of 0.0 — the fail-open contract shared with the other gates. A truly
    observed zero (e.g. 0 unique buyers seen) is a genuine signal and is
    passed as the numeric 0.
    """

    creator_cadence_mints_per_hour: float | None = None
    curve_progress_sol: float | None = None
    holder_gini: float | None = None  # observed top-N floor
    top10_fraction: float | None = None
    unique_buyers: int | None = None
    dev_balance_fraction: float | None = None  # 0..1

    # Normalization ceilings (feature value at which the sub-score saturates
    # to 1.0). Determined from the literature + replay calibration.
    cadence_ceiling: float = 60.0  # 60 mints/hour = extreme factory
    progress_floor_sol: float = 48.0  # pump.fun graduation point ~= full
    gini_ceiling: float = 0.8  # 2608.01609 rug threshold
    top10_ceiling: float = 0.9
    buyers_floor: int = 3  # fewer than this = dev-only wash
    dev_ceiling: float = 0.2  # dev holds > 20% = control risk


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _safe_div(a: float, b: float) -> float:
    return a / b if b > 0 else 0.0


def _sub(a: float | None, ceiling: float) -> float:
    """Normalized sub-score; None (unknown) contributes 0.0 (benign)."""
    if a is None:
        return 0.0
    return _clamp01(_safe_div(float(a), ceiling))


def compute_rug_score(
    f: RugScoreInput,
    weights: dict[str, float] | None = None,
) -> float:
    """Weighted rug-risk score in [0, 1]; higher = riskier.

    Sub-scores are the normalized distance toward the risk ceiling:

    - creator cadence: mints/hour / cadence_ceiling (saturates)
    - curve progress: 1 - progress/progress_floor (less progress = riskier)
    - holder concentration: max(gini/gini_ceiling, top10/top10_ceiling)
    - unique buyers: 1 - buyers/buyers_floor (fewer = riskier)
    - dev balance: dev_balance_fraction / dev_ceiling

    UNKNOWN features (None) contribute a benign sub-score of 0.0, matching
    the fail-open contract of the other gates; a genuine observed 0 is a
    real signal (e.g. 0 unique buyers -> full buyer-risk sub-score).
    Calibration can shift weights per replay evidence.
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)
    total_w = sum(w.values()) or 1.0
    inv = 1.0 / total_w

    creator = _sub(f.creator_cadence_mints_per_hour, f.cadence_ceiling)
    progress = (
        1.0 - _sub(f.curve_progress_sol, f.progress_floor_sol)
        if f.curve_progress_sol is not None
        else 0.0
    )
    concentration = _clamp01(
        max(
            _sub(f.holder_gini, f.gini_ceiling),
            _sub(f.top10_fraction, f.top10_ceiling),
        )
    )
    buyers = (
        1.0 - _sub(float(f.unique_buyers), float(f.buyers_floor))
        if f.unique_buyers is not None
        else 0.0
    )
    dev = _sub(f.dev_balance_fraction, f.dev_ceiling)

    return (
        w["creator_cadence"] * creator
        + w["curve_progress"] * progress
        + w["holder_concentration"] * concentration
        + w["unique_buyers"] * buyers
        + w["dev_balance"] * dev
    ) * inv


def rug_score_reason(score: float, threshold: float) -> str | None:
    """Human-readable refusal when score exceeds threshold."""
    if threshold <= 0:
        return None
    if score > threshold:
        return f"rug risk score {score:.3f} exceeds threshold {threshold}"
    return None