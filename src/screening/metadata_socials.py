"""Metadata socials gate (R3-P2-8).

Scam tokens routinely launch with empty or zero-extension metadata — no
socials, no description, no website. The solanart/`mutable.filter.ts`
reference refuses coins whose metadata extensions are empty. This module
reads the token's SPL metadata account (the standard Token Metadata program,
9Kb account at the metadata PDA) and extracts the `extensions` object.

Design:
- The READ goes through the background refresher (off the hot path) and is
  stored as a simple snapshot; the gate (`has_non_empty_extensions`) is a
  pure local check — no RPC from `_skip_reason`.
- Fail-open: a mint with no snapshot yet (refresher not run / RPC down /
  non-metadata program) is ALLOWED. The gate only refuses on positive
  evidence that the metadata has zero non-empty extensions AND the gate is
  enabled (min_metadata_extensions > 0).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: Token Metadata program (Metaplex) — the metadata account owner.
METADATA_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"


@dataclass
class MetadataSnapshot:
    """One mint's parsed metadata extensions (off-hot-path snapshot)."""

    mint: str
    extensions: dict[str, Any] = field(default_factory=dict)
    symbol: str = ""
    name: str = ""

    @property
    def non_empty_extensions(self) -> int:
        """Count of extension entries whose value is non-empty (str/list)."""
        n = 0
        for k, v in self.extensions.items():
            if isinstance(v, str):
                if v.strip():
                    n += 1
            elif isinstance(v, list):
                if v:
                    n += 1
            elif v not in (None, False, 0):
                n += 1
        return n


@dataclass
class MetadataStore:
    """mint -> MetadataSnapshot, filled by the background refresher."""

    snapshots: dict[str, MetadataSnapshot] = field(default_factory=dict)

    def get(self, mint: str) -> MetadataSnapshot | None:
        return self.snapshots.get(mint)

    def put(self, snapshot: MetadataSnapshot) -> None:
        self.snapshots[snapshot.mint] = snapshot


def parse_metadata_json(raw: str, mint: str) -> MetadataSnapshot:
    """Parse the metadata URI JSON into a snapshot.

    The metadata JSON (off-chain, from the token's `uri`) is the source of
    the `extensions` object (twitter/telegram/website/description). A
    non-JSON / unparseable body yields an empty snapshot.
    """
    if not raw or not raw.strip():
        return MetadataSnapshot(mint=mint)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return MetadataSnapshot(mint=mint)
    if not isinstance(data, dict):
        return MetadataSnapshot(mint=mint)
    exts = data.get("extensions") or data.get("links") or {}
    return MetadataSnapshot(
        mint=mint,
        extensions=exts if isinstance(exts, dict) else {},
        symbol=str(data.get("symbol", "")),
        name=str(data.get("name", "")),
    )