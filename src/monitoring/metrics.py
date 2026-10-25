"""Lightweight runtime metrics for the sniper (T1.7).

Borrows the shape of carbon's pipeline instrumentation (counters, a queue
gauge, processing histograms) without pulling in a metrics server: these are
plain dicts with monotonic timestamps, logged periodically and exposed via
``snapshot()`` for a future /metrics endpoint or Prometheus export.

No dependency on the trading path: this module is importable from monitoring
and trading alike, and every setter is a no-op-safe call.
"""

from __future__ import annotations

from collections import deque
from typing import Any


class Metrics:
    """Process-wide counters, gauges and a bounded histogram."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.gauges: dict[str, int | float] = {}
        #: Bounded per-event processing durations (seconds). Oldest dropped.
        self._hist: deque[tuple[str, float]] = deque(maxlen=500)

    def incr(self, name: str, by: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + by

    def set_gauge(self, name: str, value: int | float) -> None:
        self.gauges[name] = value

    def record(self, name: str, value: float) -> None:
        self._hist.append((name, value))

    def histogram(self, name: str) -> dict[str, float]:
        """Count/avg/p95 over recorded values for one label."""
        vals = [v for (n, v) in self._hist if n == name]
        if not vals:
            return {"count": 0, "avg": 0.0, "p95": 0.0}
        avg = sum(vals) / len(vals)
        p95 = sorted(vals)[int(len(vals) * 0.95) - 1] if len(vals) > 1 else vals[0]
        return {"count": len(vals), "avg": round(avg, 6), "p95": round(p95, 6)}

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": dict(self.counters),
            "gauges": dict(self.gauges),
            "hist": {
                name: self.histogram(name)  # type: ignore[arg-type]
                for name in {n for n, _ in self._hist}
            },
        }


#: Shared instance. One process, one metric registry.
metrics = Metrics()