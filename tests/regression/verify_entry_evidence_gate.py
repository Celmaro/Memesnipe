"""R5-P1-1: entry-evidence gate — wait for trade history before buying.

Why this exists (live evidence, 2026-10-30 shadow archive): every single
entry fired 3-16 s after launch (median 9 s). At that age the behavioral
filters (wash round-trips, co-fire coordination, holder snapshots) have no
observations, so they fail open and the "quality filters" add no edge. The
gate parks a young candidate, re-queues it at min_token_age_s, and then
requires observed-trade / unique-buyer evidence at PRE-BUY time.

Checks (no network, no funds):

  A. WashLpiDetector.event_count / unique_buyer_count reflect observations.
  B. CoFireTracker.buyer_count reflects early-buyer observations.
  C. Gate fires pre-buy when evidence is below min_observed_trades.
  D. Gate fires pre-buy when evidence is below min_unique_buyers.
  E. Gate passes when both thresholds are met.
  F. require_evidence=False (detection-time call) never applies the gate —
     detection must not permanently skip a mint before its window opened.
  G. _queue_token parks a young candidate (queue stays empty, pending count
     increments) and a duplicate detect does not double-park.
  H. _deferred_enqueue re-queues the token after the delay.
  I. YAML arms 60 s / 20 trades / 10 buyers.
  J. config_loader accepts the keys and rejects a negative age.

Usage:
    uv run tests/regression/verify_entry_evidence_gate.py
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import yaml  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import WSOL_MINT  # noqa: E402
from interfaces.core import Platform  # noqa: E402
from screening.coordination import CoFireTracker  # noqa: E402
from screening.wash_lpi import WashLpiDetector  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

failures = []


def check(label: str, passed: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
    if not passed:
        failures.append(label)


def _token(symbol="EVID", mint=None):
    return SimpleNamespace(
        mint=Pubkey.from_string(
            mint or "37Vnce2mgcjCDqw75X57Xy68DvfFerdxNwB12qetpump"
        ),
        symbol=symbol,
        platform=Platform.PUMP_FUN,
        quote_mint=WSOL_MINT,
        creation_timestamp=None,
        transfer_fee_bps=0,
    )


def _gate_trader(**overrides):
    t = object.__new__(UniversalTrader)
    t._wash_lpi = WashLpiDetector()
    t._cofire = CoFireTracker()
    # every other dynamic gate off, so only the evidence gate can fire
    t.max_coordinated_supply_share = 0.0
    t.max_wash_share = 0.0
    t.rug_score_threshold = 0.0
    t.factory_deploy_rate = 0.0
    t.creator_pnl_store = None
    t.min_unique_holders = 0
    t.max_top10_concentration = 0.0
    t.max_gini_coefficient = 0.0
    t._holder_store = SimpleNamespace(get=lambda _m: None)
    t._rug_buyers = {}
    t._creator_launch_ts = {}
    t.min_metadata_extensions = 0
    t._metadata_store = SimpleNamespace(get=lambda _m: None)
    t.max_transfer_fee_bps = None
    t.min_observed_trades = 0
    t.min_unique_buyers = 0
    for k, v in overrides.items():
        setattr(t, k, v)
    return t


def main() -> int:
    print("R5-P1-1: entry-evidence gate (min age / trades / buyers)")

    # --- A: wash tracker counters ---------------------------------------
    wl = WashLpiDetector()
    mint = "mintA"
    for i in range(7):
        wl.observe_trade(
            mint=mint,
            wallet=f"w{i % 3}",
            is_buy=True,
            quote_lamports=1_000_000,
            token_price=1e-8,
            ts=float(i),
        )
    wl.observe_trade(
        mint=mint,
        wallet="w0",
        is_buy=False,
        quote_lamports=1_000_000,
        token_price=1e-8,
        ts=99.0,
    )
    check("wash event_count counts observations", wl.event_count(mint) == 8)
    check("wash unique_buyer_count counts distinct buyers",
          wl.unique_buyer_count(mint) == 3)
    check("wash unknown mint reads zero", wl.event_count("nope") == 0)

    # --- B: co-fire buyer count ------------------------------------------
    cf = CoFireTracker()
    for w in ("a", "b", "c"):
        cf.observe_buy("mintB", w)
    cf.observe_buy("mintB", "a")  # duplicate wallet
    check("cofire buyer_count counts distinct early buyers",
          cf.buyer_count("mintB") == 3)

    # --- C/D/E: the pre-buy gate -----------------------------------------
    tok = _token()
    t = _gate_trader(min_observed_trades=20, min_unique_buyers=0)
    for i in range(5):
        t._wash_lpi.observe_trade(
            mint=str(tok.mint), wallet=f"w{i}", is_buy=True,
            quote_lamports=1_000_000, token_price=1e-8, ts=float(i),
        )
    r = t._dynamic_screening_reason(tok, require_evidence=True)
    check("gate refuses insufficient trade evidence",
          bool(r) and "insufficient trade evidence" in r, f"-> {r}")

    t2 = _gate_trader(min_observed_trades=0, min_unique_buyers=10)
    for i in range(4):
        t2._cofire.observe_buy(str(tok.mint), f"b{i}")
    r2 = t2._dynamic_screening_reason(tok, require_evidence=True)
    check("gate refuses insufficient buyer evidence",
          bool(r2) and "insufficient buyer evidence" in r2, f"-> {r2}")

    t3 = _gate_trader(min_observed_trades=20, min_unique_buyers=10)
    for i in range(25):
        t3._wash_lpi.observe_trade(
            mint=str(tok.mint), wallet=f"w{i}", is_buy=True,
            quote_lamports=1_000_000, token_price=1e-8, ts=float(i),
        )
    for i in range(12):
        t3._cofire.observe_buy(str(tok.mint), f"buyer{i}")
    r3 = t3._dynamic_screening_reason(tok, require_evidence=True)
    check("gate passes when both thresholds are met", r3 is None, f"-> {r3}")

    # --- F: detection-time call must NOT apply the evidence gate ---------
    t4 = _gate_trader(min_observed_trades=20, min_unique_buyers=10)
    r4 = t4._dynamic_screening_reason(tok)  # require_evidence default False
    check("detection-time call skips the evidence gate", r4 is None, f"-> {r4}")

    # --- G: young token is parked, duplicate detect does not double-park --
    async def park():
        q = asyncio.Queue(maxsize=10)
        t = object.__new__(UniversalTrader)
        t.processed_tokens = set()
        t._queued_mints = set()
        t.token_queue = q
        t.token_timestamps = {}
        t._first_seen_mints = {}
        t.token_launch_timestamps = {}
        t.max_tracked_tokens = 1000
        t.min_token_age_s = 60.0
        t._evidence_pending_count = 0
        t.max_evidence_pending = 256
        tk = _token(symbol="YOUNG")
        await UniversalTrader._queue_token(t, tk)
        await UniversalTrader._queue_token(t, tk)  # duplicate detect
        parked = q.empty() and t._evidence_pending_count == 1
        # let the deferred task start (it sleeps 60s; cancel it after)
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        return parked

    check("young candidate parked once (queue empty)", asyncio.run(park()))

    # --- H: deferred enqueue re-queues after the delay -------------------
    async def defer():
        q = asyncio.Queue(maxsize=10)
        t = object.__new__(UniversalTrader)
        t.processed_tokens = set()
        t._queued_mints = set()
        t.token_queue = q
        t.token_timestamps = {}
        t._first_seen_mints = {}  # seeded after tk is built below
        t.token_launch_timestamps = {}
        t.max_tracked_tokens = 1000
        t.min_token_age_s = 60.0
        t._evidence_pending_count = 1
        t.max_evidence_pending = 256
        tk = _token(symbol="AGED")
        t._queued_mints.add(str(tk.mint))  # held while parked
        from time import monotonic

        t._first_seen_mints[str(tk.mint)] = monotonic() - 60.1  # now "aged"
        await UniversalTrader._deferred_enqueue(t, tk, 0.01)
        return not q.empty() and t._evidence_pending_count == 0

    check("deferred enqueue re-queues the aged token", asyncio.run(defer()))

    # --- H2: clock-boundary re-park loop is prevented --------------------
    async def boundary():
        from time import monotonic

        q = asyncio.Queue(maxsize=10)
        t = object.__new__(UniversalTrader)
        t.processed_tokens = set()
        t._queued_mints = set()
        t.token_queue = q
        t.token_timestamps = {}
        t._first_seen_mints = {}
        t.token_launch_timestamps = {}
        t.max_tracked_tokens = 1000
        t.min_token_age_s = 60.0
        t._evidence_pending_count = 0
        t.max_evidence_pending = 256
        tk = _token(symbol="BOUNDARY")
        # 59.9 s old: remaining is 0.1 s — under the park epsilon, so it
        # must enqueue NOW instead of re-parking (a 0.1 s wait is not
        # evidence; re-parking spins the deferred task loop).
        t._first_seen_mints[str(tk.mint)] = monotonic() - 59.9
        await UniversalTrader._queue_token(t, tk)
        return (not q.empty()) and t._evidence_pending_count == 0

    check(
        "59.9s-old candidate enqueues instead of re-parking",
        asyncio.run(boundary()),
    )

    # --- I: park row emitted (observation capture) -----------------------
    class _FakeCapture:
        def __init__(self):
            self.rows = []

        def park(self, **kw):
            self.rows.append(("park", kw))

        def gate_eval(self, **kw):
            self.rows.append(("gate_eval", kw))

        def detect(self, **kw):
            self.rows.append(("detect", kw))

    async def park_capture():
        q = asyncio.Queue(maxsize=10)
        t = object.__new__(UniversalTrader)
        t.processed_tokens = set()
        t._queued_mints = set()
        t.token_queue = q
        t.token_timestamps = {}
        t._first_seen_mints = {}
        t.token_launch_timestamps = {}
        t.max_tracked_tokens = 1000
        t.min_token_age_s = 60.0
        t._evidence_pending_count = 0
        t.max_evidence_pending = 256
        cap = _FakeCapture()
        t.shadow_capture = cap
        tk = _token(symbol="CAP1")
        await UniversalTrader._queue_token(t, tk)
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()
        return cap.rows

    prows = [kw for kind, kw in asyncio.run(park_capture()) if kind == "park"]
    check(
        "park row emitted with remaining time",
        len(prows) == 1 and 59.0 <= float(prows[0]["remaining_s"]) <= 60.0,
        f"-> {prows}",
    )

    # --- J: gate_eval row carries verdict + observed inputs --------------
    async def gate_capture():
        from time import monotonic

        t = _gate_trader(min_observed_trades=20, min_unique_buyers=10)
        cap = _FakeCapture()
        t.shadow_capture = cap
        t._first_seen_mints = {str(tok.mint): monotonic() - 61.0}
        for i in range(25):
            t._wash_lpi.observe_trade(
                mint=str(tok.mint),
                wallet=f"w{i}",
                is_buy=True,
                quote_lamports=1_000_000,
                token_price=1e-8,
                ts=float(i),
            )
        for i in range(12):
            t._cofire.observe_buy(str(tok.mint), f"buyer{i}")
        await UniversalTrader._capture_gate_eval(t, tok, "pass")
        return [kw for kind, kw in cap.rows if kind == "gate_eval"]

    grows = asyncio.run(gate_capture())
    check(
        "gate_eval row carries verdict + observed inputs",
        len(grows) == 1
        and grows[0]["verdict"] == "pass"
        and int(grows[0].get("observed_trades") or 0) >= 20
        and int(grows[0].get("unique_buyers") or 0) >= 10
        and float(grows[0].get("age_s") or 0) >= 60,
        f"-> {grows}",
    )

    # --- K: trade-feed scoping (candidates only) --------------------------
    class _FakeParser:
        def __init__(self, mint):
            self.mint = mint

        def parse_trade_event_from_logs(self, logs, outer_signer=None):  # noqa: ARG002
            return {
                "mint": self.mint,
                "trader": "trader1",
                "is_buy": True,
                "token_amount": "1000000",
                "sol_amount": "1000000",
                "quote_amount": "1000000",
            }

    def _feed_trader(mint, candidate: bool):
        t = object.__new__(UniversalTrader)
        t._wash_lpi = WashLpiDetector()
        t._cofire = CoFireTracker()
        t._rug_buyers = {}
        t.token_timestamps = {}
        t._buy_reservations = set()
        t._active_exit_mints = set()
        t._unsold_positions = {}
        t._first_seen_mints = {mint: 0.0} if candidate else {}
        t.migrate_watcher = None
        t._creator_sellers = {}
        t._creator_sold_for_exit = set()
        t.exit_on_creator_sell = False
        t.post_entry_advisories_enabled = False
        t.event_driven_exits_enabled = False
        t.wallet = None
        t.platform_implementations = SimpleNamespace(event_parser=_FakeParser(mint))
        return t

    async def scope_case(mint, candidate):
        t = _feed_trader(mint, candidate)
        await UniversalTrader._consume_trade_logs(t, ["log"], None)
        return t._wash_lpi.event_count(mint), t._cofire.buyer_count(mint)

    nc_trades, nc_buyers = asyncio.run(
        scope_case("NONCANDPUMPmintNotTracked111111111111111111", False)
    )
    check(
        "non-candidate mint trades are NOT observed (feed stays bounded)",
        nc_trades == 0 and nc_buyers == 0,
        f"-> trades={nc_trades} buyers={nc_buyers}",
    )

    c_trades, c_buyers = asyncio.run(
        scope_case("CANDPUMPmintTracked222222222222222222222222", True)
    )
    check(
        "candidate mint trades ARE observed (evidence window survives)",
        c_trades == 1 and c_buyers == 1,
        f"-> trades={c_trades} buyers={c_buyers}",
    )

    # --- L: RPC trade backfill (blind-feed workaround) --------------------
    class _FakeBackfillClient:
        def __init__(self):
            self.calls = []
            self.sigs = []
            self.txs = []

        async def post_rpc(self, body, **kwargs):
            self.calls.append(body)
            if isinstance(body, dict) and body.get("method") == "getSignaturesForAddress":
                return {"result": self.sigs}
            if isinstance(body, list):
                return self.txs
            return None

    async def backfill_case(mint_sym, candidate_mint, sigs, txs, min_trades=20, min_buyers=10):
        t = object.__new__(UniversalTrader)
        t.min_observed_trades = min_trades
        t.min_unique_buyers = min_buyers
        t._evidence_backfilled = set()
        t._rpc_evidence = {}
        t._rpc_sig_cache = {}
        t._wash_lpi = WashLpiDetector()
        t._cofire = CoFireTracker()
        t._rug_buyers = {}
        t._backfill_client = _FakeBackfillClient()
        t._backfill_client.sigs = sigs
        t._backfill_client.txs = txs
        t.solana_client = t._backfill_client
        t.platform_implementations = SimpleNamespace(event_parser=_FakeParser(candidate_mint))
        tk = _token(mint_sym, candidate_mint)
        await UniversalTrader._backfill_trades_from_rpc(t, tk)
        await UniversalTrader._backfill_buyers_from_rpc(t, tk)
        return t

    fake_sig = {
        "signature": "FAKESIG" + "0" * 56,
        "err": None,
        "slot": 1,
        "blockTime": 1,
    }
    # 25 successful sigs: sig-only count = 24 trades, enough to trigger buyer decode.
    many_sigs = [dict(fake_sig, signature=f"FAKESIG{i:02d}" + "0" * 54) for i in range(25)]
    few_sigs = many_sigs[:3]
    fake_tx = {
        "jsonrpc": "2.0",
        "id": 0,
        "result": {
            "transaction": {"message": {"accountKeys": ["traderA"]}},
            "meta": {"logMessages": ["Program data: aGVsbG8="]},
        },
    }
    bf = asyncio.run(
        backfill_case(
            "BFILL",
            "37Vnce2mgcjCDqw75X57Xy68DvfFerdxNwB12qetpump",
            many_sigs,
            [fake_tx],
        )
    )
    bf_mint = "37Vnce2mgcjCDqw75X57Xy68DvfFerdxNwB12qetpump"
    check(
        "trade count from signatures + buyer decode from txs",
        bf._rpc_evidence.get(bf_mint, {}).get("trades") == 24
        and bf._rpc_evidence.get(bf_mint, {}).get("buyers") == 1
        and bf_mint in bf._evidence_backfilled,
        f"-> {bf._rpc_evidence.get(bf_mint)}",
    )

    bf2 = asyncio.run(
        backfill_case(
            "BFILL",
            bf_mint,
            many_sigs,
            [fake_tx],
            min_trades=0,
        )
    )
    check(
        "backfill skipped entirely when the evidence gate is off",
        bf2._wash_lpi.event_count(bf_mint) == 0
        and bf2._backfill_client.calls == [],
        f"-> rpc calls={len(bf2._backfill_client.calls)}",
    )

    bf3 = asyncio.run(
        backfill_case(
            "BFILL",
            bf_mint,
            few_sigs,
            [fake_tx],
        )
    )
    check(
        "sig-only when signature count cannot meet buyer gate",
        bf3._rpc_evidence.get(bf_mint, {}).get("trades") == 2
        and bf3._rpc_evidence.get(bf_mint, {}).get("buyers") is None,
        f"-> {bf3._rpc_evidence.get(bf_mint)}",
    )

    gate_reason = UniversalTrader._dynamic_screening_reason(
        bf, _token("BFILL", bf_mint), require_evidence=True
    )
    check(
        "evidence gate consults RPC counts (blind feed)",
        gate_reason == "insufficient buyer evidence: 1 unique < 10 required (min_unique_buyers)",
        f"-> {gate_reason}",
    )

    # --- K: YAML armed values --------------------------------------------
    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    f = cfg["filters"]
    check(
        "YAML arms min_token_age_s 60 / trades 20 / buyers 10",
        f.get("min_token_age_s") == 60
        and f.get("min_observed_trades") == 20
        and f.get("min_unique_buyers") == 10,
        f"-> {f.get('min_token_age_s')}/{f.get('min_observed_trades')}/"
        f"{f.get('min_unique_buyers')}",
    )

    # --- J: config validation --------------------------------------------
    from config_loader import validate_config

    good = {"filters": {"min_token_age_s": 60, "min_observed_trades": 20,
                        "min_unique_buyers": 10, "quote_amounts": {"sol": 0.01},
                        "match_string": "pump", "listener_type": "geyser"}}
    cfg_obj = SimpleNamespace(get=lambda k, d=None: cfg.get(k, d),
                              __getitem__=lambda s, k: cfg[k])
    # validate_config needs a full config; build the minimal real one instead
    base = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    try:
        validate_config(base)
        ok_cfg = True
    except Exception as exc:  # noqa: BLE001
        ok_cfg = f"raised: {exc}"
    neg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    neg["filters"]["min_token_age_s"] = -5
    try:
        validate_config(neg)
        neg_rejected = False
    except Exception:
        neg_rejected = True
    check("config validates armed keys", ok_cfg is True, f"-> {ok_cfg}")
    check("config rejects negative min_token_age_s", neg_rejected)

    if failures:
        print(f"\nR5-P1-1 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR5-P1-1: entry-evidence gate OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
