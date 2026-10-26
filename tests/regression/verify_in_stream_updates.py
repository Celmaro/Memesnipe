"""R3-P2-15 regression: in-stream filter update + continuity revision.

Covers:
- continuity_revision starts 0, bumps on _bump_continuity (reconnect path
  and update_subscription both call it).
- update_subscription with in-stream updates ENABLED queues a fresh
  SubscribeRequest into the request stream (no reconnect) and updates the
  watched program IDs.
- update_subscription with in-stream updates DISABLED (default) still
  records the filter change and bumps the revision, but queues nothing —
  the change applies on the next reconnect (today's behavior, unchanged).
- A consumer cache keyed on the revision discards pre-gap state when the
  revision changes.
- Config wiring: YAML default false + BOOL_FIELDS entry.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from monitoring.universal_geyser_listener import (  # noqa: E402
    UniversalGeyserListener,
)
import config_loader  # noqa: E402


class _Listener(UniversalGeyserListener):
    """Constructor stub: no platform registration, no network."""

    def __init__(self, in_stream: bool):
        self.geyser_endpoint = "grpc://dummy"
        self.geyser_api_token = "dummy"
        self.auth_type = "x-token"
        self.platforms = []
        self.platform_parsers = {}
        self.platform_program_ids = {"old-program"}
        self._last_slot = None
        self._last_event_time = None
        self._dead_slots = set()
        self._dead_slots_max = 4096
        self._stream_healthy = True
        self._watchdog_task = None
        self._watchdog_poll_interval = 2.0
        self._watchdog_idle_threshold = 5.0
        self._watchdog_max_stale_slots = 60
        self._reconnect_delay = 1.0
        self._reconnect_delay_max = 30.0
        self._force_reconnect = asyncio.Event()
        self._continuity_revision = 0
        self._in_stream_updates = in_stream
        self._update_queue: asyncio.Queue | None = None
        self._stream_request_iter = None
        self.trade_callback = None


class _Cache:
    """Mirror of the curve-cache/shadow-capture consumer contract."""

    def __init__(self):
        self.revision = -1
        self.entries = {"mintA": {"price": 1.0}}

    def sync(self, revision: int) -> None:
        if revision != self.revision:
            self.entries.clear()  # pre-gap state is stale: drop it
            self.revision = revision


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P2-15 in-stream filter update + continuity revision")

    # Default: in-stream updates off; revision still bumps; nothing queued.
    l_off = _Listener(in_stream=False)
    check("revision starts 0", l_off.continuity_revision == 0)
    rev = asyncio.run(l_off.update_subscription(program_ids={"new-program"}))
    check("off: filter change recorded", l_off.platform_program_ids == {"new-program"})
    check("off: revision bumped", rev == 1 and l_off.continuity_revision == 1)
    check(
        "off: nothing queued (no reconnect gap created)",
        l_off._update_queue is None,  # noqa: SLF001
    )

    # Enabled: update queues a request for the live stream.
    l_on = _Listener(in_stream=True)
    l_on._update_queue = asyncio.Queue()  # noqa: SLF001  (live stream)
    rev2 = asyncio.run(l_on.update_subscription(program_ids={"new-program"}))
    check("on: filter change recorded", l_on.platform_program_ids == {"new-program"})
    check("on: revision bumped", rev2 == 1)
    check(
        "on: update queued for in-stream delivery",
        not l_on._update_queue.empty(),  # noqa: SLF001
    )

    # Reconnect path bumps the revision (source-level contract check).
    src = Path(
        "src/monitoring/universal_geyser_listener.py"
    ).read_text()
    check(
        "reconnect path bumps revision",
        "self._bump_continuity()" in src,
    )
    check(
        "request iterator exists (queue-fed stream)",
        "async def _request_iterator" in src,
    )

    # Consumer cache discards stale state on revision change.
    cache = _Cache()
    cache.sync(0)  # cache primed for revision 0
    cache.entries["mintA"] = {"price": 2.0}  # pre-gap state
    cache.sync(1)  # stream reconnected
    check(
        "consumer cache drops pre-gap state on revision bump",
        cache.revision == 1 and cache.entries == {},
    )

    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML default in_stream_updates_enabled false",
        cfg["research"].get("in_stream_updates_enabled") is False,
        f"-> {cfg['research'].get('in_stream_updates_enabled')!r}",
    )
    check(
        "BOOL_FIELDS includes research.in_stream_updates_enabled",
        "research.in_stream_updates_enabled" in config_loader.BOOL_FIELDS,
    )

    if failures:
        print(f"\nR3-P2-15 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P2-15 in-stream filter update + continuity revision OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())