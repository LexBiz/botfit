from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Sex = Literal["male", "female"]
ActivityLevel = Literal["low", "medium", "high"]
Goal = Literal["loss", "maintain", "gain", "recomp"]


@dataclass(frozen=True)
class Targets:
    calories: int
    protein_g: int
    fat_g: int
    carbs_g: int


@dataclass(frozen=True)
class CalcMeta:
    bmr_kcal: int
    tdee_kcal: int
    goal: Goal
    deficit_pct: float  # negative for surplus
    deficit_kcal: int   # negative for surplus


def _activity_multiplier(level: ActivityLevel) -> float:
    # simplified multipliers
    return {
        "low": 1.2,
        "medium": 1.55,
        "high": 1.725,
    }[level]


def bmr_mifflin_st_jeor(sex: Sex, age: int, height_cm: float, weight_kg: float) -> float:
    # BMR = 10W + 6.25H - 5A + s
    s = 5 if sex == "male" else -161
    return 10 * weight_kg + 6.25 * height_cm - 5 * age + s


def tdee(bmr: float, activity: ActivityLevel) -> float:
    return bmr * _activity_multiplier(activity)


def default_deficit_pct(goal: Goal) -> float:
    # positive = deficit, negative = surplus
    if goal == "loss":
        return 0.15
    if goal == "recomp":
        return 0.10
    if goal == "gain":
        return -0.10
    return 0.0


def clamp_deficit_pct(goal: Goal, pct: float) -> float:
    # keep it realistic + safe
    if goal == "gain":
        return max(min(pct, -0.05), -0.15)
    if goal == "maintain":
        return 0.0
    if goal == "recomp":
        return max(min(pct, 0.15), 0.05)
    # loss
    return max(min(pct, 0.30), 0.10)


def calorie_target_from_tdee(tdee_kcal: float, *, goal: Goal, deficit_pct: float | None = None) -> tuple[int, float]:
    pct = default_deficit_pct(goal) if deficit_pct is None else deficit_pct
    pct = clamp_deficit_pct(goal, float(pct))
    cal = int(round(tdee_kcal * (1.0 - pct)))
    return cal, pct


def macros_for_targets(calories: int, weight_kg: float, goal: Goal) -> Targets:
    # pragmatic default split:
    # protein: 1.6g/kg (loss/maintain), 1.8g/kg (gain/recomp)
    # fat: 0.8g/kg
    # carbs: remainder
    protein = int(round((1.8 if goal in {"gain", "recomp"} else 1.6) * weight_kg))
    fat = int(round(0.8 * weight_kg))

    # kcal from protein/fat
    kcal_pf = protein * 4 + fat * 9
    carbs_kcal = max(calories - kcal_pf, 0)
    carbs = int(round(carbs_kcal / 4))
    return Targets(calories=calories, protein_g=protein, fat_g=fat, carbs_g=carbs)


def compute_targets(
    sex: Sex,
    age: int,
    height_cm: float,
    weight_kg: float,
    activity: ActivityLevel,
    goal: Goal,
) -> Targets:
    t, _ = compute_targets_with_meta(
        sex=sex,
        age=age,
        height_cm=height_cm,
        weight_kg=weight_kg,
        activity=activity,
        goal=goal,
        deficit_pct=None,
    )
    return t


def compute_targets_with_meta(
    *,
    sex: Sex,
    age: int,
    height_cm: float,
    weight_kg: float,
    activity: ActivityLevel,
    goal: Goal,
    deficit_pct: float | None,
) -> tuple[Targets, CalcMeta]:
    b = bmr_mifflin_st_jeor(sex=sex, age=age, height_cm=height_cm, weight_kg=weight_kg)
    td = tdee(b, activity=activity)
    cal, pct = calorie_target_from_tdee(td, goal=goal, deficit_pct=deficit_pct)
    targets = macros_for_targets(cal, weight_kg=weight_kg, goal=goal)
    meta = CalcMeta(
        bmr_kcal=int(round(b)),
        tdee_kcal=int(round(td)),
        goal=goal,
        deficit_pct=float(pct),
        deficit_kcal=int(round(int(round(td)) - cal)),
    )
    return targets, meta

