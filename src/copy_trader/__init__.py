"""__init__ for copy_trader — Phase 7 scaffold.

The qualification engine is implemented and offline-tested. The chain
reconstruction pipeline and the on-chain executor are separate components
(see docs/IMPLEMENTATION-PLAN.md Phase 7) and ship disabled.
"""

from copy_trader.qualification import (  # noqa: F401
    CopySignal,
    LeaderProfile,
    LeaderVerdict,
    TradeSummary,
    check_entry_drift,
    qualify_leader,
    size_follow_position,
)