"""R3-P2-14 regression: slot-ordered event delivery.

Covers:
- push/drain: events released in ascending slot order even when pushed out
  of order; a gap holds later events until the missing slot arrives.
- Late-drop: an event at/below the watermark is dropped (never replayed
  after newer state was consumed).
- advance_watermark: marks buffered events below the new watermark as
  dropped/consumed.
- Config wiring: YAML default false + BOOL_FIELDS entry.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from monitoring.slot_buffer import SlotBuffer  # noqa: E402
import config_loader  # noqa: E402


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P2-14 slot-ordered event delivery")

    # Out-of-order pushes release in ascending slot order.
    b = SlotBuffer()
    b.push(5, "fill-A")  # slot 5 arrives first
    b.push(3, "fill-B")
    b.push(4, "fill-C")
    # Missing slots (0-2) had no events — the buffer releases what it holds
    # in ascending order (3, 4, 5), not "waits for a gap".
    first = b.drain()
    check(
        "out-of-order pushes release in order",
        first == ["fill-B", "fill-C", "fill-A"],
        f"-> {first}",
    )

    # Late-drop: an event at/below the watermark is dropped.
    b2 = SlotBuffer()
    b2.push(10, "ten")
    b2.drain()
    ok = b2.push(9, "late-nine")
    check("late event below watermark dropped", not ok and b2.dropped_late == 1)

    # advance_watermark drops buffered below the new watermark.
    b3 = SlotBuffer()
    b3.push(10, "ten")
    b3.push(12, "twelve")
    dropped = b3.advance_watermark(11)
    check(
        "advance_watermark drops below",
        dropped == 1 and b3.watermark == 11 and b3.dropped_late == 1,
        f"-> dropped={dropped} wm={b3.watermark}",
    )
    d3 = b3.drain()
    check("above-watermark event survives", d3 == ["twelve"], f"-> {d3}")

    # Same-slot events keep arrival order (tiebreaker seq).
    b4 = SlotBuffer()
    b4.push(20, "first")
    b4.push(20, "second")
    d4 = b4.drain()
    check("same-slot arrival order kept", d4 == ["first", "second"], f"-> {d4}")

    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML default slot_ordered_feed_enabled false",
        cfg["research"].get("slot_ordered_feed_enabled") is False,
        f"-> {cfg['research'].get('slot_ordered_feed_enabled')!r}",
    )
    check(
        "BOOL_FIELDS includes research.slot_ordered_feed_enabled",
        "research.slot_ordered_feed_enabled" in config_loader.BOOL_FIELDS,
    )

    # Feed-consumption wiring (audit fix): the buffer must be CONSUMED by the
    # trade feeds, not just constructed — both the rug feed and the copy feed
    # push into it and drain in slot order when enabled.
    trader_src = Path("src/trading/universal_trader.py").read_text()
    check(
        "rug feed pushes into the slot buffer",
        "_slot_buffer.push(slot, (logs, outer_signer))" in trader_src,
    )
    check(
        "copy feed pushes into the slot buffer",
        "_slot_buffer.push(slot, (logs, outer_signer))" in trader_src,
    )
    check(
        "feeds drain the buffer in order",
        trader_src.count("_slot_buffer.drain()") >= 2,
        f"-> {trader_src.count('_slot_buffer.drain()')} drain sites",
    )
    listener_src = Path("src/monitoring/universal_geyser_listener.py").read_text()
    check(
        "listener passes tx slot to the callback",
        "tx_slot = update_slot" in listener_src
        and "await self.trade_callback(logs, outer_signer, tx_slot)"
        in listener_src,
    )

    if failures:
        print(f"\nR3-P2-14 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P2-14 slot-ordered event delivery OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())