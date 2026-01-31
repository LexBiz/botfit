from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Sex = Literal["male", "female"]
ActivityLevel = Literal["low", "medium", "high"]
Goal = Literal["loss", "maintain", "gain"]


@dataclass(frozen=True)
class Targets:
    calories: int
    protein_g: int
    fat_g: int
    carbs_g: int


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


def calorie_target_from_goal(tdee_kcal: float, goal: Goal) -> int:
    if goal == "loss":
        return int(round(tdee_kcal * 0.85))  # ~15% deficit
    if goal == "gain":
        return int(round(tdee_kcal * 1.10))  # ~10% surplus
    return int(round(tdee_kcal))


def macros_for_targets(calories: int, weight_kg: float, goal: Goal) -> Targets:
    # pragmatic default split:
    # protein: 1.6g/kg (loss/maintain), 1.8g/kg (gain)
    # fat: 0.8g/kg
    # carbs: remainder
    protein = int(round((1.8 if goal == "gain" else 1.6) * weight_kg))
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
    b = bmr_mifflin_st_jeor(sex=sex, age=age, height_cm=height_cm, weight_kg=weight_kg)
    td = tdee(b, activity=activity)
    cal = calorie_target_from_goal(td, goal=goal)
    return macros_for_targets(cal, weight_kg=weight_kg, goal=goal)

