"""Staged-then-trail exit state machine (R4-P1-7).

Implements the asymmetric-exit rule calibrated on the 53-position paper
cohort (verified primary source, memeassist.com): fixed TP +11.2% average
vs momentum-fade trailing +52.7%, hard SL -40.1%. The rule this module
encodes is the cohort's combined playbook:

1.  Sell ``initial_tp_share`` of the position at the first +``initial_tp_pct``
    touch (risk paid).
2.  At +``arm_pct`` unrealized the profit floor locks at break-even and
    trails ``trail_pct`` behind every new high.
3.  Exit the remainder when the floor is touched, or the price never
    armed and falls to the hard stop-loss.

This module is pure math — no RPC, no trader state. The caller owns the
``TrailExitState`` per open position and issues the sells the returned
decisions describe, so the same state machine can drive the live monitor
loop AND the shadow strategies ledger in parallel.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TrailDecision:
    """One action the caller must take for a position tick."""

    action: str  # "sell_share" | "exit_all" | "hold"
    reason: str  # e.g. "initial_tp", "trail_floor", "stop_loss", ""
    share: float = 0.0  # fraction of the REMAINING position to sell
    price: float = 0.0  # the price that triggered the decision


@dataclass
class TrailExitState:
    """Mutable per-position state for the staged-then-trail rule."""

    entry_price: float
    initial_tp_share: float = 0.4
    initial_tp_pct: float = 0.25
    second_tp_share: float = 0.0
    second_tp_pct: float = 0.0
    arm_pct: float = 0.10
    trail_pct: float = 0.08
    hard_stop_pct: float = 0.20
    tp1_floor_pct: float = 0.0
    peak_price: float = field(init=False)
    floor_price: float = field(init=False)
    initial_tp_fired: bool = field(init=False, default=False)
    second_tp_fired: bool = field(init=False, default=False)
    armed: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        if self.entry_price <= 0:
            raise ValueError(f"entry_price must be > 0: {self.entry_price!r}")
        if not 0 <= self.initial_tp_share <= 1.0:
            raise ValueError(
                f"initial_tp_share must be in [0, 1]: {self.initial_tp_share!r}"
            )
        if not 0 <= self.second_tp_share <= 1.0:
            raise ValueError(
                f"second_tp_share must be in [0, 1]: {self.second_tp_share!r}"
            )
        if self.initial_tp_share + self.second_tp_share > 1.0:
            raise ValueError(
                "initial_tp_share + second_tp_share must be <= 1.0: "
                f"{self.initial_tp_share} + {self.second_tp_share}"
            )
        self.peak_price = self.entry_price
        self.floor_price = self.entry_price * (1 - self.hard_stop_pct)

    def on_tick(self, current_price: float) -> TrailDecision:
        """Feed one observed price and get the exit decision for it.

        Order of checks (cheapest first):
        - below the armed floor -> exit all (trail floor)
        - below the hard stop while never armed -> exit all
        - initial TP level touched -> sell a share, lock the post-TP1 floor
        - second TP level touched -> sell another share (runner protection)
        - new high -> raise the floor, arm at +arm_pct, hold
        - otherwise hold

        The caller must NOT persist decisions; this is a pure transition.
        """
        if current_price <= 0:
            return TrailDecision("hold", "no_price", price=current_price)

        # 1. Trailing floor (only active once armed).
        if self.armed and current_price <= self.floor_price:
            return TrailDecision("exit_all", "trail_floor", price=current_price)

        # 2. Hard stop before any profit was armed.
        if not self.armed and current_price <= self.floor_price:
            return TrailDecision("exit_all", "stop_loss", price=current_price)

        # 3. Initial take-profit share.
        if not self.initial_tp_fired and self.initial_tp_share > 0:
            tp_level = self.entry_price * (1 + self.initial_tp_pct)
            if current_price >= tp_level:
                self.initial_tp_fired = True
                self.peak_price = max(self.peak_price, current_price)
                # Post-TP1 floor: a banked TP must not be given back. Lock
                # at entry (+ round-trip costs when tp1_floor_pct > 0) even
                # before the +arm_pct trigger would normally arm.
                self.floor_price = max(
                    self.floor_price,
                    self.entry_price * (1 + self.tp1_floor_pct),
                )
                return TrailDecision(
                    "sell_share",
                    "initial_tp",
                    share=self.initial_tp_share,
                    price=current_price,
                )

        # 4. Second take-profit share (runner protection). Share is a
        # fraction of the REMAINING position: second_tp_share is defined
        # against the ORIGINAL position, so convert (e.g. 0.3 of original
        # after 0.4 was sold at TP1 -> 0.5 of the remaining 0.6).
        if (
            self.initial_tp_fired
            and not self.second_tp_fired
            and self.second_tp_share > 0
        ):
            tp2_level = self.entry_price * (1 + self.second_tp_pct)
            if current_price >= tp2_level:
                self.second_tp_fired = True
                self.peak_price = max(self.peak_price, current_price)
                remaining = 1.0 - self.initial_tp_share
                share = self.second_tp_share / remaining if remaining > 0 else 0.0
                return TrailDecision(
                    "sell_share",
                    "second_tp",
                    share=min(1.0, share),
                    price=current_price,
                )

        # 5. New high: raise peak, activate the floor, trail it.
        if current_price > self.peak_price:
            self.peak_price = current_price
            if not self.armed and current_price >= self.entry_price * (
                1 + self.arm_pct
            ):
                self.armed = True
                # Break-even floor: at +arm_pct the trade is not allowed to
                # give back to red (the cohort's "never go back to red").
                self.floor_price = max(self.floor_price, self.entry_price * 1.0)
            new_floor = self.peak_price * (1 - self.trail_pct)
            if self.armed and new_floor > self.floor_price:
                self.floor_price = new_floor
            return TrailDecision("hold", "new_high", price=current_price)

        # 6. Ordinary tick: hold.
        return TrailDecision("hold", "", price=current_price)
