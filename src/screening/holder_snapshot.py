"""Holder-concentration gate (R3-P1-5).

Distribution risk on pump.fun is measurable from token-account holders:
- <N unique holders in the early window = sniper-only (no real distribution).
- Top-10 accounts holding >X% of supply = whale concentration / rug risk.

This module keeps the MATH pure and offline-testable, and a snapshot store
that a background refresher fills via `getTokenLargestAccounts`. The hot
path (`_skip_reason`) reads only the immutable snapshot — never RPC.

Fail-open contract (mirrors the plan's pre-buy risk gate): a mint with NO
snapshot entry (refresher not run / RPC down / mint unknown) is ALLOWED.
The gate only refuses on positive evidence of bad distribution. The
background refresher stamps each entry with the slot it read, and the
store additionally stamps a per-mint monotonic record time; callers can
reject stale entries via ``is_stale(mint, max_age_s)`` (the trader keeps
a max-age window). A mint with no snapshot is never stale (fail-open).

Largest-account semantics: `getTokenLargestAccounts` returns up to 20
accounts by balance. Unique-holder count is NOT the number of returned
accounts — it is a floor, since the top-20 list is the visible distribution.
The math here treats the returned list as the observed set (documented).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

#: Cap on the top-N considered when computing concentration.
TOP_N = 10


@dataclass(frozen=True)
class HolderSnapshot:
    """Immutable distribution snapshot for one mint."""

    mint: str
    unique_holders: int
    top10_fraction: float  # 0..1 fraction of total supply held by top-10
    total_supply_raw: int
    # R4-P1-10: Gini coefficient over the OBSERVED balance vector (the
    # top-N accounts returned by getTokenLargestAccounts). This is a floor
    # on the true Gini (hidden small accounts lower it), documented — the
    # 2608.01609 thresholds (>0.8 rug / <0.5 normal) still separate
    # distributions cleanly on the visible set.
    gini: float = 0.0
    slot: int = 0

    def passes(
        self,
        *,
        min_unique_holders: int,
        max_top10_concentration: float,
        max_gini: float = 0.0,
    ) -> bool:
        """True when the snapshot satisfies the configured gates.

        A gate value <= 0 means that gate is off (not checked).
        """
        if min_unique_holders > 0 and self.unique_holders < min_unique_holders:
            return False
        if max_top10_concentration > 0 and (
            self.top10_fraction > max_top10_concentration
        ):
            return False
        if max_gini > 0 and self.gini > max_gini:
            return False
        return True


@dataclass
class HolderSnapshotStore:
    """mint -> HolderSnapshot, refreshed by a background loop.

    Every store write stamps the per-mint monotonic record time so the
    gate read can enforce a max-age window (the age checks live here, not
    on the frozen snapshot, because ``compute_snapshot`` callers never
    pass a wall time). No snapshot = no age = never stale (fail-open).
    """

    snapshots: dict[str, HolderSnapshot] = field(default_factory=dict)
    #: mint -> time.monotonic() at the latest record (per-mint timestamp).
    _recorded_at: dict[str, float] = field(default_factory=dict)

    def get(self, mint: str) -> HolderSnapshot | None:
        return self.snapshots.get(mint)

    def put(self, snapshot: HolderSnapshot) -> None:
        """Backward-compatible alias of :meth:`record` (kept for existing
        refresher call sites; identical behavior, also stamps the clock)."""
        self.record(snapshot)

    def record(self, snapshot: HolderSnapshot) -> None:
        """Store the snapshot and stamp its per-mint monotonic record time.

        The snapshots themselves are immutable; the timestamp is tracked
        per mint here so ``is_stale`` can reject old reads on the hot path.
        """
        self.snapshots[snapshot.mint] = snapshot
        self._recorded_at[snapshot.mint] = time.monotonic()

    def last_recorded_at(self, mint: str) -> float | None:
        """Monotonic-clock time of the latest snapshot for mint; None when
        the mint has no snapshot (never recorded)."""
        return self._recorded_at.get(mint)

    def age_seconds(self, mint: str) -> float | None:
        """Seconds since the latest snapshot for mint; None when the mint
        has no snapshot (nothing to age). Never negative."""
        t = self._recorded_at.get(mint)
        if t is None:
            return None
        return max(0.0, time.monotonic() - t)

    def is_stale(self, mint: str, max_age_s: float) -> bool:
        """True when the mint HAS a snapshot older than ``max_age_s``.

        Fail-open contract: a mint with NO snapshot is never stale (the
        refresher may simply not have run). ``max_age_s <= 0`` disables
        the check entirely (never stale) — same off-switch convention as
        the other gates.
        """
        if max_age_s <= 0:
            return False
        age = self.age_seconds(mint)
        if age is None:
            return False
        return age > max_age_s


def gini_coefficient(balances: list[int]) -> float:
    """Gini coefficient over a balance vector: 0 = equal, 1 = one holder.

    R4-P1-10 (2608.01609): rug projects typically run Gini > 0.8 pre-
    attack vs < 0.5 for normal projects. Computed on the OBSERVED vector
    (top-N from getTokenLargestAccounts) — a floor on the true Gini.
    """
    if not balances:
        return 0.0
    # Zero-balance entries are noise; drop them so a sparse observation
    # doesn't fake equality.
    vals = sorted(int(b) for b in balances if b > 0)
    n = len(vals)
    if n == 0:
        return 0.0
    if n == 1:
        return 1.0
    total = sum(vals)
    if total <= 0:
        return 0.0
    # Gini = 2 * sum(i * b_i) / (n * total) - (n+1)/n  (sorted ascending)
    weighted = sum((i + 1) * b for i, b in enumerate(vals))
    return (2 * weighted) / (n * total) - (n + 1) / n


def compute_snapshot(
    mint: str,
    largest_accounts: list[tuple[str, int]],
    total_supply_raw: int,
    *,
    slot: int = 0,
) -> HolderSnapshot:
    """Build a HolderSnapshot from largest-account rows.

    Args:
        mint: The token mint (string form).
        largest_accounts: Rows of (owner/account address, raw balance) from
            `getTokenLargestAccounts`, highest balance first.
        total_supply_raw: Token total supply in raw units (9-decimal, etc.)
            from `getTokenSupply`.
        slot: Slot the read came from (0 when unknown).

    Returns:
        Snapshot with unique_holders = len(rows) (the observed top-N floor),
        top10_fraction = sum(top-10 balances) / supply clamped to [0,1], and
        gini over the observed balance vector (R4-P1-10).
    """
    unique = len(largest_accounts)
    top10 = largest_accounts[:TOP_N]
    top10_sum = sum(balance for _addr, balance in top10)
    fraction = (
        (top10_sum / total_supply_raw) if total_supply_raw > 0 else 0.0
    )
    fraction = max(0.0, min(1.0, fraction))
    return HolderSnapshot(
        mint=mint,
        unique_holders=unique,
        top10_fraction=fraction,
        total_supply_raw=total_supply_raw,
        gini=gini_coefficient([b for _addr, b in largest_accounts]),
        slot=slot,
    )
