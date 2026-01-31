from __future__ import annotations

from src.nutrition import compute_targets_with_meta


def test_loss_has_deficit() -> None:
    targets, meta = compute_targets_with_meta(
        sex="male",
        age=28,
        height_cm=190,
        weight_kg=118,
        activity="medium",
        goal="loss",
        deficit_pct=0.15,
    )
    assert meta.tdee_kcal > targets.calories
    assert meta.deficit_kcal > 0


def test_maintain_no_deficit() -> None:
    targets, meta = compute_targets_with_meta(
        sex="male",
        age=28,
        height_cm=190,
        weight_kg=118,
        activity="medium",
        goal="maintain",
        deficit_pct=0.0,
    )
    assert meta.deficit_kcal == 0
    assert meta.tdee_kcal == targets.calories


def test_gain_surplus() -> None:
    targets, meta = compute_targets_with_meta(
        sex="male",
        age=28,
        height_cm=190,
        weight_kg=118,
        activity="medium",
        goal="gain",
        deficit_pct=-0.10,
    )
    assert targets.calories > meta.tdee_kcal
    assert meta.deficit_kcal < 0

