"""Slot-ordered event delivery (R3-P2-14).

Port of sol-parser-sdk's SlotBuffer + late-drop semantics
(src/grpc/buffers.rs:44-59,118-175) for the monitor/copy path: geyser can
deliver updates out of order (network reordering, parallel subscribers), and
an out-of-order fill corrupts position state (a TP evaluated before the buy
fill it depends on). This buffer holds events per slot and releases them in
ascending slot order; a late event for a slot below the watermark is DROPPED
(marked late), never replayed after newer state was consumed.

The snipe hot path stays unordered (it wants the raw event ASAP); only the
exit-monitor / copy-trader consumers use this buffer.

Disabled-first: a consumer that does not install a buffer is unaffected.
"""

from __future__ import annotations

import asyncio
import heapq
from dataclasses import dataclass, field


@dataclass
class SlotBuffer:
    """Min-heap of (slot, seq, payload); releases in ascending order.

    Attributes:
        watermark: Highest slot emitted so far. Events at or below it are
            late (dropped).
        dropped_late: Count of dropped late events (the size of the gap the
            consumer never sees).
    """

    watermark: int = -1
    dropped_late: int = 0
    _heap: list[tuple[int, int, object]] = field(default_factory=list)
    _seq: int = 0
    _max_buffered: int = 4096

    def push(self, slot: int, payload: object) -> bool:
        """Buffer one event; False when it is late (dropped).

        Args:
            slot: The slot the event belongs to (ascending across the stream).
            payload: The event (TradeEvent dict, fill result, ...).

        Returns:
            True when buffered (will be yielded by drain in order), False
            when dropped as late.
        """
        if slot <= self.watermark:
            self.dropped_late += 1
            return False
        # Binding tiebreaker so two events in the same slot keep arrival order.
        self._seq += 1
        heapq.heappush(self._heap, (slot, self._seq, payload))
        # Bound the buffer: if it overflows, drop the oldest (lowest slot) —
        # they are the most stale and the consumer is not keeping up.
        if len(self._heap) > self._max_buffered:
            _, _, dropped = heapq.heappop(self._heap)
            self.dropped_late += 1
            return False
        return True

    def drain(self, max_items: int = 100) -> list[object]:
        """Release buffered events in ascending slot order.

        The min-heap is already sorted by slot; release everything it holds
        (missing slots simply had no events — they are not gaps to wait for).
        The watermark only serves late-drop: an event pushed AFTER a higher
        slot was consumed is dropped.
        """
        out: list[object] = []
        while len(out) < max_items and self._heap:
            slot, _seq, payload = heapq.heappop(self._heap)
            out.append(payload)
            self.watermark = slot
        return out

    def advance_watermark(self, slot: int) -> int:
        """Mark everything at/below ``slot`` as consumed (late-drop).

        Used when a consumer skips ahead (e.g. a fresh subscription starting
        at a later slot). Returns the number of buffered events dropped.
        """
        self.watermark = max(self.watermark, slot)
        kept: list[tuple[int, int, object]] = []
        dropped = 0
        for item in self._heap:
            if item[0] <= self.watermark:
                dropped += 1
                self.dropped_late += 1
            else:
                kept.append(item)
        self._heap = kept
        heapq.heapify(self._heap)
        return dropped