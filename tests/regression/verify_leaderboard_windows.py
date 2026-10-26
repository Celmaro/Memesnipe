"""R3-P2-7 regression: cross-window (24h/7d/30d) leaderboard qualification.

Covers:
- qualify_leader now requires the 24h window positive when a 24h summary is
  present (keep-all-windows-positive); a 7d-negative wallet rejects.
- WalletProfiler.profile_multiwindow fetches 1d/7d/30d and merges them.
- Multi-window wiring: engine logs the multi-window profile.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from copy_trader.qualification import (  # noqa: E402
    MIN_CLOSED_TRADES_30D,
    LeaderProfile,
    LeaderVerdict,
    qualify_leader,
    TradeSummary,
)


def _profile(thirty_net: float, seven_net: float, one_net: float | None) -> LeaderProfile:
    p = LeaderProfile(address="walletX", last_seen_days_ago=0.1)
    p.window(30).closed_trades = MIN_CLOSED_TRADES_30D
    p.window(30).net_pnl_sol = thirty_net
    p.window(7).closed_trades = 50
    p.window(7).net_pnl_sol = seven_net
    if one_net is not None:
        p.window(1).closed_trades = 10
        p.window(1).net_pnl_sol = one_net
    return p


class FakeHttp:
    def __init__(self, responses: dict[str, dict]):
        self._responses = responses

    async def get_json(self, url: str) -> dict | None:
        # route by ?period=
        if "?period=" in url:
            period = url.split("?period=")[1]
            return self._responses.get(period)
        return self._responses.get("1d")


async def test_multiwindow_fetch() -> list[str]:
    failures = []
    from copy_trader.gmgn_profiler import WalletProfiler

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    http = FakeHttp(
        {
            "1d": {"data": {"wallet": "w", "realized_pnl": 1.0}},
            "7d": {"data": {"wallet": "w", "realized_pnl": 2.0}},
            "30d": {"data": {"wallet": "w", "realized_pnl": 3.0}},
        }
    )
    profiler = WalletProfiler()

    async def _fake_fetch(wallet: str, period: str | None = None) -> dict:
        payload = await http.get_json(
            wallet + (f"?period={period}" if period else "")
        )
        if not payload:
            return {}
        data = payload.get("data", payload)
        return {
            "wallet": str(data.get("wallet", "")),
            "realized_pnl": data.get("realized_pnl"),
        }

    profiler._fetch = _fake_fetch  # noqa: SLF001

    merged = await profiler.profile_multiwindow("walletX")
    windows = merged.get("windows", {})
    check(
        "multiwindow fetches 1d/7d/30d",
        set(windows) == {"1d", "7d", "30d"},
        f"-> {sorted(windows)}",
    )
    check(
        "30d pnl carried",
        windows.get("30d", {}).get("realized_pnl") == 3.0,
    )
    return failures


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P2-7 cross-window leaderboard qualification")

    # 30d/7d/24h all positive -> qualified.
    v = qualify_leader(_profile(10.0, 5.0, 1.0))
    check("all windows positive -> QUALIFIED", v is LeaderVerdict.QUALIFIED, f"-> {v}")

    # 7d negative -> reject.
    v2 = qualify_leader(_profile(10.0, -1.0, 1.0))
    check("7d negative -> NEGATIVE_NET", v2 is LeaderVerdict.NEGATIVE_NET, f"-> {v2}")

    # 24h negative (present) -> reject (R3-P2-7).
    v3 = qualify_leader(_profile(10.0, 5.0, -0.5))
    check("24h negative -> NEGATIVE_NET", v3 is LeaderVerdict.NEGATIVE_NET, f"-> {v3}")

    # 24h absent (old single-window caller) -> still passes (fail-open).
    v4 = qualify_leader(_profile(10.0, 5.0, None))
    check("24h absent -> qualified (back-compat)", v4 is LeaderVerdict.QUALIFIED, f"-> {v4}")

    # Multi-window engine wiring (alias fetch works).
    failures += asyncio.run(test_multiwindow_fetch())

    if failures:
        print(f"\nR3-P2-7 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P2-7 cross-window leaderboard qualification OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())