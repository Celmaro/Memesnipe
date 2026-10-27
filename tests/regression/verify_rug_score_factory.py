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

    # 1. Score is 0 for a pristine coin with unknown dimensions.
    s0 = compute_rug_score(RugScoreInput())
    check("unknown dims -> 0", s0 == 0.0, f"-> {s0:.4f}")

    # 2. Factory cadence pushes the score to its weight (0.25 saturates).
    s_factory = compute_rug_score(
        RugScoreInput(creator_cadence_mints_per_hour=120.0)  # 2/min
    )
    check(
        "factory cadence saturates its weight",
        0.24 < s_factory <= 0.26,
        f"-> {s_factory:.4f}",
    )

    # 3. Rug-shaped holders push the score to their weight (0.20).
    s_holders = compute_rug_score(
        RugScoreInput(holder_gini=0.85, top10_fraction=0.95)
    )
    check(
        "unequal holders saturate their weight",
        0.19 < s_holders <= 0.21,
        f"-> {s_holders:.4f}",
    )

    # 4. Zero buyers push the score to their weight (0.20).
    s_buyers = compute_rug_score(RugScoreInput(unique_buyers=0))
    check(
        "zero buyers saturate their weight",
        0.19 < s_buyers <= 0.21,
        f"-> {s_buyers:.4f}",
    )

    # 5. Stalled curve pushes the score up (progress 5/48 -> sub 0.896
    # scaled by its 0.20 weight).
    s_curve = compute_rug_score(RugScoreInput(curve_progress_sol=5.0))
    check(
        "stalled curve raises score",
        0.15 < s_curve <= 0.20,
        f"-> {s_curve:.4f}",
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
        )
    )
    check(
        "combined bad features > 0.5",
        s_combo > 0.5,
        f"-> {s_combo:.4f}",
    )
    # Strong triangulation: cadence + holders together cross the 0.4 level.
    s_pair = compute_rug_score(
        RugScoreInput(
            creator_cadence_mints_per_hour=120.0,
            holder_gini=0.85,
            top10_fraction=0.95,
        )
    )
    check(
        "paired signals exceed either alone",
        s_pair > 0.4,
        f"-> {s_pair:.4f}",
    )

    # 6. Threshold gate + reason.
    reason = rug_score_reason(0.7, 0.5)
    check("score above threshold refuses", reason is not None)
    reason_ok = rug_score_reason(0.4, 0.5)
    check("score below threshold passes", reason_ok is None)
    reason_off = rug_score_reason(0.9, 0.0)
    check("threshold 0 = off", reason_off is None)

    # 7. Trader wiring: ctor params + _skip_reason uses the score.
    trader_src = Path("src/trading/universal_trader.py").read_text()
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
        "compute_rug_score(" in trader_src,
    )
    check(
        "trader applies factory gate in _skip_reason",
        "is a factory" in trader_src,
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