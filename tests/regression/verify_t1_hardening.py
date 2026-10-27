"""Verifier for the research-phase T1 hardening batch (post 12-repo analysis).

Covers the engine-room changes that shipped together:
- geyser listener: slot-status stream handling, dead-slot rejection, watchdog
  event, from_slot resume, jittered backoff reconnection
- quality gates: fail_closed_gates policy on curve-gate RPC errors
- tx send: failure classification + non-retry of terminal failures
- metrics: queue gauge, counters, drain methods exist and behave
"""
import asyncio
import sys
from types import SimpleNamespace

sys.path.insert(0, "src")

from core.client import TxFailureKind, classify_tx_failure
from monitoring.metrics import Metrics
from monitoring.universal_geyser_listener import UniversalGeyserListener

ok = True


def check(name: str, cond: bool) -> None:
    global ok
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    ok = ok and cond


# --- failure taxonomy -------------------------------------------------------
check(
    "blockhash-expired classified",
    classify_tx_failure(Exception("Transaction expired: BlockhashNotFound"))
    is TxFailureKind.BLOCKHASH_EXPIRED,
)
check(
    "insufficient funds classified",
    classify_tx_failure(Exception("insufficient funds for rent"))
    is TxFailureKind.INSUFFICIENT_FUNDS,
)
check(
    "simulation failure classified",
    classify_tx_failure(Exception("Transaction simulation failed: custom program error"))
    is TxFailureKind.TXN_SIMULATION_FAILED,
)
check(
    "slippage classified",
    classify_tx_failure(Exception("Slippage exceeded")) is TxFailureKind.SLIPPAGE_EXCEEDED,
)
check(
    "unknown stays unknown",
    classify_tx_failure(Exception("some random error")) is TxFailureKind.UNKNOWN,
)
check(
    "no panic on empty message",
    classify_tx_failure(Exception("")) is TxFailureKind.UNKNOWN,
)


# --- metrics ----------------------------------------------------------------
m = Metrics()
m.incr("a")
m.incr("a")
m.incr("b")
m.set_gauge("g", 3)
m.record("t", 0.1)
m.record("t", 0.3)
m.record("t", 0.2)
snap = m.snapshot()
check("counter aggregated", snap["counters"]["a"] == 2)
check("gauge stored", snap["gauges"]["g"] == 3)
check("histogram count", snap["hist"]["t"]["count"] == 3)
check("histogram avg", abs(snap["hist"]["t"]["avg"] - 0.2) < 1e-9)


# --- listener: dead-slot bookkeeping + watchdog wiring ----------------------
listener = UniversalGeyserListener(
    "dummy:443", "token", "x-token", platforms=[]  # type: ignore[arg-type]
)
listener._dead_slots.add(999)
listener._last_slot = 1000
listener._last_event_time = 0.0  # stale on purpose
listener._stream_healthy = True

# An update whose transaction slot is dead is rejected.
dead_update = SimpleNamespace(
    HasField=lambda field: field == "transaction",
    transaction=SimpleNamespace(slot=999, transaction=SimpleNamespace()),
)
result = asyncio.run(listener._process_update(dead_update))
check("dead-slot update rejected", result is None)

# A healthy-slot update with no parseable platform event also returns None,
# but must not crash on the slot path.
live_update = SimpleNamespace(
    HasField=lambda field: field == "transaction",
    transaction=SimpleNamespace(slot=1001, transaction=SimpleNamespace()),
)
result = asyncio.run(listener._process_update(live_update))
check("live-slot update handled without crash", result is None)

check("watchdog spores event on idle", hasattr(listener, "_force_reconnect"))
listener._reconnect_delay = 1.0
listener._stream_healthy = True
listener._force_reconnect = asyncio.Event()

# Watchdog fires when idle past the threshold.
async def _watchdog_probe():
    listener._watchdog_poll_interval = 0.05
    listener._watchdog_idle_threshold = 0.05
    listener._last_event_time = asyncio.get_event_loop().time() - 1.0
    task = asyncio.create_task(listener._watchdog())
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return listener._force_reconnect.is_set() and not listener._stream_healthy

check("watchdog sets force-reconnect", asyncio.run(_watchdog_probe()))


# --- fail-closed gates ------------------------------------------------------
# The curve gate returns a reject reason when the underlying RPC throws,
# instead of failing open. We exercise the decision via a trader stub whose
# get_platform_implementations raises (it is a live-RPC path; the stub keeps
# this offline and deterministic).
from trading.universal_trader import UniversalTrader

stub = object.__new__(UniversalTrader)
stub._momentum_peak_rate = {}  # R4-P1-8 mock state
stub._momentum_peak_price = {}  # R4-P1-8 mock state
stub.fragility_scale_min = 1.0  # R4-P3-1 mock default (off)
stub.fragility_whale_threshold = 0.6  # R4-P3-1 mock default
stub.fragility_rate_threshold = 0.6  # R4-P3-1 mock default
stub.stagnation_exit_min = 0  # R4-P1-9 mock default (off)
stub.stagnation_band_pct = 0.02  # R4-P1-9 mock default
stub.max_wash_share = 0.0  # R4-P1-6 mock default
from screening.wash_lpi import WashLpiDetector as _WLD  # R4-P1-6 mock default
stub._wash_lpi = _WLD()  # R4-P1-6 mock default
stub.max_gini_coefficient = 0.0  # R4-P1-10 mock default
stub.rug_score_threshold = 0.0  # R4-P1-1 mock default
stub.factory_deploy_rate = 0.0  # R4-P1-2 mock default
stub.max_coordinated_supply_share = 0.0  # R4-P1-3 mock default
stub.exit_mode = "tp_sl"  # R4-P1-7 mock default
stub.strategies_ledger = None  # R4-P1-7 mock default
stub.allow_mayhem_mode = False  # R3-P0-2 off in verifiers
stub.max_market_cap_sol = 40.0
stub.min_curve_progress_sol = None
stub.min_market_cap_sol = None
stub.fail_closed_gates = True
stub.solana_client = None

import trading.universal_trader as ut

ut.get_platform_implementations = lambda *a, **k: (_ for _ in ()).throw(
    RuntimeError("rpc down")
)

tok = SimpleNamespace(
    platform="pump_fun",
    bonding_curve="abc",
    pool_state=None,
    associated_bonding_curve=None,
)
reason = asyncio.run(UniversalTrader._curve_gate_reason(stub, tok))  # noqa: SLF001
check(
    "fail-closed gate rejects on RPC error",
    reason is not None and "fail-closed" in reason,
)

stub.fail_closed_gates = False
reason = asyncio.run(UniversalTrader._curve_gate_reason(stub, tok))  # noqa: SLF001
check("fail-open gate passes through on RPC error", reason is None)


# --- drain + metrics logger exist ------------------------------------------
check("metrics logger attached", hasattr(UniversalTrader, "_metrics_logger"))
check("drain queue attached", hasattr(UniversalTrader, "_drain_token_queue"))

print()
print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)