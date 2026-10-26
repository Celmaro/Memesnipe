"""SWQoS / fee ground-truth capture (R3-P2-4).

Port of the sol-parser-sdk cost analysis (src/transaction_cost.rs:255-279,
402-451) into research capture: a registry of known tip-account providers
plus fee math, so shadow-mode events can record the fee the bot WOULD have
paid (priority fee + jito tip) as ground truth for tuning the dynamic fee
auction.

No real transaction is parsed here — shadow mode never sends. The fee is
estimated from what the send path configured: priority fee (microlamports)
times compute units, plus the jito tip (lamports) when a bundle path is on.
When a real `sim_close` carries an actual parsed cost (e.g. a future real-run
ledger row), the parser accepts it as authoritative.

Disabled-first: the trader passes an optional ``estimate_fee_sol`` value into
capture; when absent, the event records ``fee_source: "unknown"``.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Known tip-account providers (prefixes of the tip recipient address). The
#: real registry in sol-parser-sdk covers ~17 providers; this is the subset
#: that maps to the bot's current send paths (block-engine / default RPC).
TIP_PROVIDER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("96gYZc", "jito"),  # Jito tip accounts (block-engine)
    ("3AVi9", "jito"),
    ("HFnUa", "jito"),
    ("ADuT", "jito"),
    ("Cw8", "jito"),
    ("9un", "jito"),
)

#: Default compute-unit budget used by the client when none is configured.
DEFAULT_COMPUTE_UNITS = 85_000
#: pump.fun guidance keeps a static 100k CU budget for snipes.
PUMP_GUIDANCE_COMPUTE_UNITS = 100_000


def identify_tip_provider(tip_account_b58: str) -> str | None:
    """Best-effort provider name for a tip recipient address, else None.

    Uses the address prefix against the known registry. A None result means
    \"not a recognized tip account\" — the caller should treat the transfer as
    ordinary value, not a tip.
    """
    for prefix, provider in TIP_PROVIDER_PREFIXES:
        if tip_account_b58.startswith(prefix):
            return provider
    return None


@dataclass(frozen=True)
class FeeEstimate:
    """What the send path would have paid, in SOL (lamports/1e9)."""

    priority_fee_sol: float = 0.0
    tip_sol: float = 0.0
    compute_units: int = DEFAULT_COMPUTE_UNITS
    tip_provider: str | None = None

    @property
    def total_sol(self) -> float:
        return self.priority_fee_sol + self.tip_sol

    def to_capture_fields(self) -> dict:
        return {
            "paid_fee_sol": self.total_sol,
            "fee_source": (
                "estimate" if self.total_sol > 0 else "unknown"
            ),
            "tip_provider": self.tip_provider or "",
        }


def estimate_send_fee(
    priority_fee_microlamports: int | None,
    *,
    tip_lamports: int = 0,
    tip_account_b58: str | None = None,
    compute_units: int = DEFAULT_COMPUTE_UNITS,
) -> FeeEstimate:
    """Estimate the fee the configured send path would pay.

    Args:
        priority_fee_microlamports: Configured priority fee (microlamports).
            None = no priority fee.
        tip_lamports: Jito tip in lamports when a bundle path is on (0 = off).
        tip_account_b58: Optional tip recipient to identify the provider.
        compute_units: Compute-unit budget the fee applies to.

    Returns:
        FeeEstimate with priority/tip split in SOL.
    """
    priority_sol = 0.0
    if priority_fee_microlamports and priority_fee_microlamports > 0:
        # microlamports/CU * CU / 1e9 lamports per SOL.
        priority_sol = (
            priority_fee_microlamports * compute_units
        ) / 1_000_000 / 1e9
    tip_sol = tip_lamports / 1e9 if tip_lamports and tip_lamports > 0 else 0.0
    provider = identify_tip_provider(tip_account_b58) if tip_account_b58 else None
    return FeeEstimate(
        priority_fee_sol=priority_sol,
        tip_sol=tip_sol,
        compute_units=compute_units,
        tip_provider=provider,
    )
