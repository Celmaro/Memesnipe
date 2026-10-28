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

``coordinated_wallet_share(mint, min_co_fire)`` returns the fraction of
the mint's early buyers that have also bought into >= min_co_fire OTHER
recent launches — a wallet acting alone on one coin is normal; one that
co-fires everywhere is likely part of a coordinated cohort. NOTE the
units: this is a WALLET-COUNT fraction (coordinated wallets / observed
early-buyer wallets), NOT a token-supply fraction — MELT's 36.5% is a
supply statistic and should only be compared against
``coordinated_supply_share`` (see its docstring for when holdings are
available).

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

    def coordinated_wallet_share(
        self, mint: str, min_co_fire: int = 3
    ) -> float:
        """Fraction of the mint's early-buyer WALLETS that co-fire elsewhere.

        UNITS: wallet-count fraction — ``# coordinated wallets / # observed
        early-buyer wallets`` — NOT a token-supply share. Compare this
        against a wallet-share threshold (e.g. ``max_coordinated_wallet_
        share``); do NOT compare it to MELT's 36.5% supply statistic, which
        is computed by :meth:`coordinated_supply_share` instead.

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

    def coordinated_share(
        self, mint: str, min_co_fire: int = 3
    ) -> float:
        """Deprecated alias of :meth:`coordinated_wallet_share`.

        Kept so existing callers (trader gate reads, regression verifiers)
        keep working while the wallet-vs-supply rename propagates. Same
        behavior, same WALLET-COUNT units — do not confuse with the supply
        share from :meth:`coordinated_supply_share`.
        """
        return self.coordinated_wallet_share(mint, min_co_fire=min_co_fire)

    def coordinated_supply_share(
        self,
        mint: str,
        balances: dict[str, int | float] | None = None,
        min_co_fire: int = 3,
    ) -> float | None:
        """Token-SUPPLY share held by the mint's coordinated wallets.

        The true supply statistic matching MELT (2602.13480: 36.5% of
        supply held by coordinated accounts on average): sum of holdings
        of wallets that co-fire across >= ``min_co_fire`` OTHER mints,
        divided by total supply.

        Returns None when balances are NOT available — the early-buyer
        observation feed (``observe_buy(mint, wallet)``) carries wallet
        identities only, never per-wallet holdings, so the tracker cannot
        see supply. Callers that DO hold balances (e.g. a holder snapshot)
        must pass ``balances={wallet: raw_balance}``; without them the
        supply share is unknowable and None (cannot score) is returned
        instead of a misleading 0.0.
        """
        if balances is None:
            return None
        buyers = self._mint_buyers.get(mint)
        if not buyers:
            return 0.0
        total = sum(balances.values())
        if total <= 0:
            return 0.0
        held = sum(
            balances.get(w, 0)
            for w in buyers
            if self.co_fire_count(w) >= min_co_fire
        )
        return min(1.0, held / total)