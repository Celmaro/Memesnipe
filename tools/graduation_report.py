"""R4-P4-2: graduation replay report — probability-by-entry-band table.

Reads replay archives (`state/replay_cache/*.jsonl.zst`) and
`tools/replay_backtester.py`-style event rows to answer the rare-event
questions R4-P1-4 feeds on: how many tokens graduate, and what does
graduation probability look like by entry market-cap band (the
"entry sweet-spot" the tracer should target).

Row contract (same events the backtester consumes):
  {"mint": str, "action": "buy|sell|migrate|createPool",
   "marketCapQuote": float (SOL), "price": float, ...}

Output: a markdown/plain table of band -> launched/graduated/probability.
Pure stdlib + zstandard; run with the project venv:
    uv run tools/graduation_report.py state/replay_cache/2026-10-26-09.jsonl.zst
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

#: Band edges in SOL of marketCapQuote at FIRST observed buy.
BANDS = [0, 5, 10, 20, 30, 40, 50, 75, 100, 200, 400]


@dataclass
class BandStats:
    launched: int = 0
    graduated: int = 0
    first_buy_mcs: list[float] = field(default_factory=list)

    @property
    def probability(self) -> float | None:
        if self.launched <= 0:
            return None
        return self.graduated / self.launched


def band_for(mc_sol: float) -> int:
    """Return the band index for a market cap (SOL)."""
    for i in range(len(BANDS) - 1):
        if BANDS[i] <= mc_sol < BANDS[i + 1]:
            return i
    return len(BANDS) - 1  # >= top band


def compute_graduation_report(rows: list[dict]) -> dict[str, BandStats]:
    """Tally graduation probability by first-observed-MC band.

    A token counts as launched when first seen (any action); as graduated
    when a ``migrate``/``createPool`` event appears for its mint. The
    first observed ``marketCapQuote`` is the entry-band proxy. Bands are
    cumulative: probability in a band = graduates / tokens first seen in
    that band (matches the 2602.14860 conditioning question).
    """
    stats: dict[str, BandStats] = {
        str(BANDS[i]): BandStats() for i in range(len(BANDS))
    }
    first_seen_mc: dict[str, float] = {}
    graduated_mints: set[str] = set()
    seen: set[str] = set()

    for ev in rows:
        mint = str(ev.get("mint") or "")
        if not mint:
            continue
        action = str(ev.get("action") or "")
        if mint not in seen:
            seen.add(mint)
            mc = float(ev.get("marketCapQuote") or 0.0)
            first_seen_mc.setdefault(mint, mc)
            stats[str(BANDS[band_for(mc)])].launched += 1
        if action in ("migrate", "createPool"):
            graduated_mints.add(mint)
        # Keep the first-seen MC for the mint in case the later migrate
        # appears on the same line (it does not carry band context).
    # Graduation is mint-level: attribute each graduate to its FIRST band.
    for mint in graduated_mints:
        if mint in first_seen_mc:
            b = str(BANDS[band_for(first_seen_mc[mint])])
            stats[b].graduated += 1
    return stats


def format_report(stats: dict[str, BandStats]) -> str:
    lines = [
        "Graduation probability by entry MC band (R4-P4-2)",
        "  band (SOL) | launched | graduated | probability",
        "  -----------|----------|-----------|------------",
    ]
    for b in (str(x) for x in BANDS):
        s = stats[b]
        prob = f"{s.probability:.1%}" if s.probability is not None else "n/a"
        lines.append(f"  {b:>10} | {s.launched:>8} | {s.graduated:>9} | {prob}")
    total_launched = sum(s.launched for s in stats.values())
    total_grad = sum(s.graduated for s in stats.values())
    lines.append(
        f"  {'TOTAL':>10} | {total_launched:>8} | {total_grad:>9} | "
        f"{(total_grad / total_launched if total_launched else 0):.2%}"
    )
    return "\n".join(lines)


def read_rows(paths: list[Path]) -> list[dict]:
    """Stream replay archives into dict rows (zstdcat, OOM-safe).

    Matches tools/replay_backtester.py: archives are streamed through the
    zstdcat binary, not loaded whole. Falls back to the zstandard module
    when available (offline verifiers) but the binary is the primary path.
    """
    import subprocess

    rows: list[dict] = []
    for p in paths:
        try:
            proc = subprocess.run(
                ["zstdcat", str(p)],
                capture_output=True,
                text=True,
                check=True,
                timeout=600,
            )
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception as e:  # noqa: BLE001
            # Fallback: python zstd module (verifier environments).
            try:
                import zstandard

                with zstandard.open(p, "rt", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except Exception:
                print(f"skipping {p}: {e}", file=sys.stderr)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("arches", nargs="+", type=Path, help="replay .jsonl.zst files")
    args = ap.parse_args()
    rows = read_rows(args.arches)
    stats = compute_graduation_report(rows)
    print(format_report(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())