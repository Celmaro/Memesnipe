"""Staged TP/SL range math (R3-P1-2).

Sells a fixed `share` of the position at each price step inside a band,
instead of one all-or-nothing TP/SL. Two band kinds:

- TP band: steps go UP from ``from_pct`` to ``to_pct`` (e.g. +40% .. +120%),
  each step sells a share — takes profits as the run extends.
- SL band: steps go DOWN from ``from_pct`` to ``to_pct`` (e.g. -10% .. -50%),
  each step sells a share — protects gains / cuts losses as the price falls.

Semantics (from the plan's MC-breakout source material):

- ``from_pct == to_pct`` is a SINGLE step. It fires whenever the price is at
  or past the level — including a jump from below to above (jump-past).
- "Everything at band bottom": when the price crosses the final (bottom) step
  of an SL band, the remaining position is sold in full (share is irrelevant
  for the last step; the caller is expected to close).
- Steps are ordered lowest-to-highest for TP (each is a price floor to
  trigger) and highest-to-lowest for SL.

This module is pure math — no RPC, no trader state. The caller tracks which
steps already fired (a step fires once) and issues the sells.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShareRange:
    """One band: sell ``share`` of the remaining position at each pct step.

    Percentages are FRACTIONS (0.4 == +40%). ``step`` > 0 is the spacing
    between consecutive triggers; ``share`` is the fraction of the ORIGINAL
    position sold per step (0 < share <= 1.0).
    """

    from_pct: float
    to_pct: float
    step: float
    share: float

    @classmethod
    def from_dict(cls, raw: dict) -> "ShareRange":
        try:
            from_pct = float(raw["from_pct"])
            to_pct = float(raw["to_pct"])
            step = float(raw["step"])
            share = float(raw["share"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"staged range needs from_pct/to_pct/step/share: {raw!r}"
            ) from exc
        if step <= 0:
            raise ValueError(f"staged range step must be > 0: {step!r}")
        if not 0 < share <= 1.0:
            raise ValueError(f"staged range share must be in (0, 1]: {share!r}")
        return cls(from_pct=from_pct, to_pct=to_pct, step=step, share=share)


def build_steps(entry_price: float, rng: ShareRange) -> list[float]:
    """Trigger prices for a band, ordered low-to-high as price levels.

    For an SL band the caller walks this list in reverse (the top step fires
    first as price falls). ``from_pct == to_pct`` yields exactly one trigger.
    """
    if entry_price <= 0:
        return []
    if rng.from_pct == rng.to_pct:
        # Single step; fires at/after the level (jump-past included).
        return [entry_price * (1 + rng.from_pct)]
    lo = min(rng.from_pct, rng.to_pct)
    hi = max(rng.from_pct, rng.to_pct)
    levels = []
    pct = lo
    while pct <= hi + 1e-12:
        levels.append(entry_price * (1 + pct))
        pct += rng.step
    # Always end exactly at the far edge (band bottom / ceiling).
    if levels[-1] < entry_price * (1 + hi) - 1e-12:
        levels.append(entry_price * (1 + hi))
    return levels


@dataclass(frozen=True)
class DueStep:
    """One step that has fired: which band, the trigger price, the share."""

    band_index: int
    trigger_price: float
    share: float
    is_final: bool  # band bottom: caller should close the rest


def due_tp_steps(
    entry_price: float,
    current_price: float,
    ranges: list[ShareRange],
    already_fired: set[tuple[int, float]],
) -> list[DueStep]:
    """TP steps whose trigger level the current price has reached.

    Walked lowest-to-highest; a single-step band (from==to) fires on a jump
    past it (current >= trigger). Each firing step is returned once — the
    caller adds (band_index, trigger) to ``already_fired`` to consume it.
    """
    due: list[DueStep] = []
    for bi, rng in enumerate(ranges):
        for level in build_steps(entry_price, rng):
            if (bi, level) in already_fired:
                continue
            if current_price >= level:
                is_final = level >= (entry_price * (1 + rng.to_pct)) - 1e-12
                due.append(
                    DueStep(
                        band_index=bi,
                        trigger_price=level,
                        share=rng.share,
                        is_final=is_final,
                    )
                )
    return due


def due_sl_steps(
    entry_price: float,
    current_price: float,
    ranges: list[ShareRange],
    already_fired: set[tuple[int, float]],
) -> list[DueStep]:
    """SL steps whose trigger level the price has fallen to.

    Walked highest-to-lowest (top step first as price falls); the bottom
    step of each band is final and sells the remaining position.
    """
    due: list[DueStep] = []
    for bi, rng in enumerate(ranges):
        levels = build_steps(entry_price, rng)
        for level in reversed(levels):
            if (bi, level) in already_fired:
                continue
            if current_price <= level:
                is_final = level <= (entry_price * (1 + rng.to_pct)) + 1e-12
                due.append(
                    DueStep(
                        band_index=bi,
                        trigger_price=level,
                        share=rng.share,
                        is_final=is_final,
                    )
                )
    return due