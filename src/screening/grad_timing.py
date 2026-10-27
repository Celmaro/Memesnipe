"""R4-P1-4: graduation-probability entry timing (advisory, log-only).

The graduation monitor's live recut established the band effect: <50 SOL
pools are the nuke zone (median 0.85x at 30 min), 85-120 SOL the sweet
spot (median 1.83x) — a dead-zone effect, not linear (raw grad-size vs
outcome correlation ~0). This module turns those bands into an entry
TIMING advisory: given the candidate's curve progress in SOL, report the
historical graduation-outcome band it falls in.

Log-only by contract: it never gates a buy. The advisory is data for the
replay/arming decision; arming a real entry-timing gate needs more regime
data (see the skill note: band medians shift day to day).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Bands from the graduation monitor recut (quote_in_pool, pure SOL).
GRAD_BANDS: list[tuple[float, float, str]] = [
    (0.0, 50.0, "nuke_zone"),  # median 0.85x, 41% <0.5x
    (50.0, 85.0, "transition"),
    (85.0, 120.0, "sweet_spot"),  # median 1.83x, 18% nuked
    (120.0, 250.0, "healthy"),
    (250.0, float("inf"), "flat_edge"),  # edge flattens above
]


@dataclass(frozen=True)
class GradTiming:
    band: str
    curve_progress_sol: float
    # From the loaded band stats (None when no stats file): historical
    # graduation median multiple and nuke rate for this band.
    median_multiple: float | None = None
    nuke_rate: float | None = None


def band_for(curve_progress_sol: float) -> str:
    for lo, hi, name in GRAD_BANDS:
        if lo <= curve_progress_sol < hi:
            return name
    return "unknown"


def load_band_stats(path: str | Path) -> dict[str, dict]:
    """Load band stats produced by the graduation toolchain.

    Expected JSON shape: {"<band>": {"median_multiple": float,
    "nuke_rate": float, "n": int}, ...}. Returns {} when the file is
    missing (advisory still works, without historical numbers).
    """
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def assess(curve_progress_sol: float, stats: dict[str, dict] | None = None) -> GradTiming:
    """Assess one candidate's graduation band (pure, no I/O)."""
    band = band_for(curve_progress_sol)
    entry = (stats or {}).get(band) or {}
    return GradTiming(
        band=band,
        curve_progress_sol=curve_progress_sol,
        median_multiple=entry.get("median_multiple"),
        nuke_rate=entry.get("nuke_rate"),
    )
