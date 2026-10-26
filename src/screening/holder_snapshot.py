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
background refresher stamps each entry with the slot it read; callers can
reject stale entries if they choose (the trader keeps a max-age window).

Largest-account semantics: `getTokenLargestAccounts` returns up to 20
accounts by balance. Unique-holder count is NOT the number of returned
accounts — it is a floor, since the top-20 list is the visible distribution.
The math here treats the returned list as the observed set (documented).
"""

from __future__ import annotations

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
    slot: int = 0

    def passes(
        self, *, min_unique_holders: int, max_top10_concentration: float
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
        return True


@dataclass
class HolderSnapshotStore:
    """mint -> HolderSnapshot, refreshed by a background loop."""

    snapshots: dict[str, HolderSnapshot] = field(default_factory=dict)

    def get(self, mint: str) -> HolderSnapshot | None:
        return self.snapshots.get(mint)

    def put(self, snapshot: HolderSnapshot) -> None:
        self.snapshots[snapshot.mint] = snapshot


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
        Snapshot with unique_holders = len(rows) (the observed top-N floor)
        and top10_fraction = sum(top-10 balances) / supply, clamped to [0,1].
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
        slot=slot,
    )
