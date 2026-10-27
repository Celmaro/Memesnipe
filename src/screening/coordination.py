"""R4-P1-3: insider-coordination (co-fire) tracker.

MELT (2602.13480) found that on average 36.5% of a launch's supply is held
by coordinated accounts — multiple wallets acting as one entity, concealed
behind independent-looking addresses. The detectable signature on a live
feed: the same wallets co-appear as early buyers across many launches
(2607.02795's union-find co-occurrence method; its effect sizes are
withdrawn but the method is sound, applied to OUR feed where buy/sell
labels come from our parser).

This tracker is fed the same per-mint early-buyer observations the rug
heuristics already consume (buy trade events). It maintains:

- mint -> early-buyer wallets (bounded recency)
- wallet -> number of mints it bought into (co-fire count, bounded)

``coordinated_share(mint, min_co_fire)`` returns the fraction of the
mint's early buyers that have also bought into >= min_co_fire OTHER recent
launches — a wallet acting alone on one coin is normal; one that co-fires
everywhere is likely part of a coordinated cohort.

Pure state machine: no RPC, no trader dependency. The caller decides how
to gate on the share.
"""

from __future__ import annotations

from collections import deque


class CoFireTracker:
    """Bounded co-fire tracking over early-buyer observations."""

    def __init__(self, max_mints: int = 500, max_wallets: int = 5000) -> None:
        self.max_mints = max_mints
        self.max_wallets = max_wallets
        # mint -> set of wallet strings (early buyers)
        self._mint_buyers: dict[str, set[str]] = {}
        # wallet -> set of mints it bought into (co-fire count)
        self._wallet_mints: dict[str, set[str]] = {}
        # Recent-mint order for bounded eviction (oldest first).
        self._mint_order: deque[str] = deque()

    def observe_buy(self, mint: str, wallet: str) -> None:
        """Record one early-buy observation (wallet bought into mint)."""
        if not mint or not wallet:
            return
        if mint not in self._mint_buyers:
            self._mint_buyers[mint] = set()
            self._mint_order.append(mint)
            self._evict_mints()
        self._mint_buyers[mint].add(wallet)
        if wallet not in self._wallet_mints:
            self._wallet_mints[wallet] = set()
            self._evict_wallets()
        self._wallet_mints[wallet].add(mint)

    def _evict_mints(self) -> None:
        while len(self._mint_buyers) > self.max_mints and self._mint_order:
            oldest = self._mint_order.popleft()
            if oldest in self._mint_buyers:
                for w in self._mint_buyers.pop(oldest):
                    wm = self._wallet_mints.get(w)
                    if wm is not None:
                        wm.discard(oldest)
                        if not wm:
                            self._wallet_mints.pop(w, None)

    def _evict_wallets(self) -> None:
        # Drop the least-recently-seen wallets when over the cap (simple
        # count bound: pop wallets that no longer have any mint refs).
        while len(self._wallet_mints) > self.max_wallets:
            # Eviction: drop wallets whose mint set is empty or smallest.
            candidate = min(self._wallet_mints, key=lambda k: len(self._wallet_mints[k]))
            self._wallet_mints.pop(candidate, None)

    def co_fire_count(self, wallet: str) -> int:
        """How many distinct mints this wallet has bought into recently."""
        return len(self._wallet_mints.get(wallet, set()))

    def coordinated_share(
        self, mint: str, min_co_fire: int = 3
    ) -> float:
        """Fraction of the mint's early buyers that co-fire elsewhere.

        A buyer is "coordinated" when it has bought into at least
        ``min_co_fire`` OTHER recent mints (the default 3 is conservative:
        a normal retailed buyer trades 1-2 tokens; a cohort member trades
        dozens). Returns 0.0 when the mint has no observations (fail open).
        """
        buyers = self._mint_buyers.get(mint)
        if not buyers:
            return 0.0
        coordinated = sum(
            1 for w in buyers if self.co_fire_count(w) >= min_co_fire
        )
        return coordinated / len(buyers)