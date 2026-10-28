"""R4-P1-1 / R4-P1-2 regression: rug-risk score gate + creator-factory filter.

- R4-P1-1: compute_rug_score() is a weighted 0..1 score over local
  features (creator cadence, holder concentration, buyer set, curve
  progress, dev balance). Unknown (None) dimensions stay benign (0),
  matching the fail-open contract. The trader gate refuses entry when the
  score exceeds filters.rug_score_threshold (0 = off).
- R4-P1-2: creator-factory filter refuses creators launching faster than
  factory_deploy_rate mints/hour (2609.10246 factories; Mazorra via
  2602.13480 disposable-token rugs).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import yaml  # noqa: E402

import config_loader  # noqa: E402
from screening.rug_score import (  # noqa: E402
    RugScoreInput,
    compute_rug_score,
    rug_score_reason,
)


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R4-P1-1/2: rug-risk score + creator-factory filter")

    # A benign-but-fully-observed profile: every dimension present with a
    # non-signal value, so sub-score tests isolate ONE dimension while
    # C4.7 feature coverage is met (6/6 observed).
    def benign() -> RugScoreInput:
        return RugScoreInput(
            creator_cadence_mints_per_hour=0.0,
            curve_progress_sol=48.0,  # full progress = benign
            holder_gini=0.0,
            top10_fraction=0.0,
            unique_buyers=100,
            dev_balance_fraction=0.0,
        )

    # 1. C4.7: no observed features -> coverage 0/6 < 1.0 -> REFUSE (None),
    # instead of the old fail-open 0.0 (missing data must not read as pass).
    s0 = compute_rug_score(RugScoreInput())
    check("no coverage -> refuse (None)", s0 is None, f"-> {s0!r}")
    # Coverage disabled (min_coverage=0) keeps the old math as an escape
    # hatch for callers that feed features later.
    s0_off = compute_rug_score(RugScoreInput(), min_coverage=0.0)
    check("min_coverage=0 disables refusal", s0_off == 0.0, f"-> {s0_off!r}")

    # 2. Factory cadence pushes the score to its weight (0.25 saturates).
    s_factory = compute_rug_score(
        RugScoreInput(
            **{**benign().__dict__, "creator_cadence_mints_per_hour": 120.0}
        )
    )
    check(
        "factory cadence saturates its weight",
        s_factory is not None and 0.24 < s_factory <= 0.26,
        f"-> {s_factory:.4f}" if s_factory is not None else f"-> {s_factory!r}",
    )

    # 3. Rug-shaped holders push the score to their weight (0.20).
    s_holders = compute_rug_score(
        RugScoreInput(
            **{
                **benign().__dict__,
                "holder_gini": 0.85,
                "top10_fraction": 0.95,
            }
        )
    )
    check(
        "unequal holders saturate their weight",
        s_holders is not None and 0.19 < s_holders <= 0.21,
        f"-> {s_holders:.4f}" if s_holders is not None else f"-> {s_holders!r}",
    )

    # 4. Zero buyers push the score to their weight (0.20).
    s_buyers = compute_rug_score(
        RugScoreInput(**{**benign().__dict__, "unique_buyers": 0})
    )
    check(
        "zero buyers saturate their weight",
        s_buyers is not None and 0.19 < s_buyers <= 0.21,
        f"-> {s_buyers:.4f}" if s_buyers is not None else f"-> {s_buyers!r}",
    )

    # 5. Stalled curve pushes the score up (progress 5/48 -> sub 0.896
    # scaled by its 0.20 weight).
    s_curve = compute_rug_score(
        RugScoreInput(**{**benign().__dict__, "curve_progress_sol": 5.0})
    )
    check(
        "stalled curve raises score",
        s_curve is not None and 0.15 < s_curve <= 0.20,
        f"-> {s_curve:.4f}" if s_curve is not None else f"-> {s_curve!r}",
    )

    # 5b. Combined bad features exceed any single weight (the whole point
    # of a MULTI-dimensional score; single dims can't cross ~0.25).
    s_combo = compute_rug_score(
        RugScoreInput(
            creator_cadence_mints_per_hour=120.0,
            holder_gini=0.85,
            top10_fraction=0.95,
            unique_buyers=0,
            curve_progress_sol=5.0,
            dev_balance_fraction=0.1,
        )
    )
    check(
        "combined bad features > 0.5",
        s_combo is not None and s_combo > 0.5,
        f"-> {s_combo:.4f}" if s_combo is not None else f"-> {s_combo!r}",
    )
    # Strong triangulation: cadence + holders together cross the 0.4 level.
    s_pair = compute_rug_score(
        RugScoreInput(
            **{
                **benign().__dict__,
                "creator_cadence_mints_per_hour": 120.0,
                "holder_gini": 0.85,
                "top10_fraction": 0.95,
            }
        )
    )
    check(
        "paired signals exceed either alone",
        s_pair is not None and s_pair > 0.4,
        f"-> {s_pair:.4f}" if s_pair is not None else f"-> {s_pair!r}",
    )

    # 5c. C4.7 partial-coverage refusal: 2 of 6 features observed is below
    # the 1.0 default but above a 0.3 bar -> None at default, score when
    # the caller lowers the bar.
    s_partial = compute_rug_score(
        RugScoreInput(
            creator_cadence_mints_per_hour=120.0,
            holder_gini=0.85,
            top10_fraction=0.95,
        )
    )
    check("partial coverage refuses at default", s_partial is None, f"-> {s_partial!r}")
    s_partial_ok = compute_rug_score(
        RugScoreInput(
            creator_cadence_mints_per_hour=120.0,
            holder_gini=0.85,
            top10_fraction=0.95,
        ),
        min_coverage=0.3,
    )
    check(
        "lower bar scores partial profile",
        s_partial_ok is not None and s_partial_ok > 0.4,
        f"-> {s_partial_ok:.4f}" if s_partial_ok is not None else f"-> {s_partial_ok!r}",
    )

    # 6. Threshold gate + reason.
    reason = rug_score_reason(0.7, 0.5)
    check("score above threshold refuses", reason is not None)
    reason_ok = rug_score_reason(0.4, 0.5)
    check("score below threshold passes", reason_ok is None)
    reason_off = rug_score_reason(0.9, 0.0)
    check("threshold 0 = off", reason_off is None)

    # 7. Trader wiring: ctor params live on UniversalTrader; the rug-score
    # evaluation + factory gate sit in the GateStackMixin (C12.3 extraction),
    # so grep both files.
    trader_src = Path("src/trading/universal_trader.py").read_text()
    gate_src = Path("src/trading/gate_stack.py").read_text()
    check(
        "trader ctor has rug_score_threshold",
        "rug_score_threshold: float = 0.0" in trader_src,
    )
    check(
        "trader ctor has factory_deploy_rate",
        "factory_deploy_rate: float = 0.0" in trader_src,
    )
    check(
        "trader applies rug score in _skip_reason",
        "compute_rug_score(" in gate_src or "compute_rug_score(" in trader_src,
    )
    check(
        "trader applies factory gate in _skip_reason",
        "is a factory" in gate_src or "is a factory" in trader_src,
    )
    # 8. Config wiring (4 places).
    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML rug_score_threshold 0",
        cfg["filters"].get("rug_score_threshold") == 0.0,
        f"-> {cfg['filters'].get('rug_score_threshold')!r}",
    )
    check(
        "YAML factory_deploy_rate 0",
        cfg["filters"].get("factory_deploy_rate") == 0.0,
        f"-> {cfg['filters'].get('factory_deploy_rate')!r}",
    )
    check(
        "CONFIG_VALIDATION_RULES has rug_score_threshold",
        any(
            r[0] == "filters.rug_score_threshold"
            for r in config_loader.CONFIG_VALIDATION_RULES
        ),
    )
    check(
        "CONFIG_VALIDATION_RULES has factory_deploy_rate",
        any(
            r[0] == "filters.factory_deploy_rate"
            for r in config_loader.CONFIG_VALIDATION_RULES
        ),
    )
    runner = Path("src/bot_runner.py").read_text()
    check(
        "bot_runner passes rug_score_threshold",
        "rug_score_threshold=cfg" in runner,
    )
    check(
        "bot_runner passes factory_deploy_rate",
        "factory_deploy_rate=cfg" in runner,
    )

    if failures:
        print(f"\nR4-P1-1/2 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nRug-risk score + creator-factory filter OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())