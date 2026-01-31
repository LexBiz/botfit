from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class IngredientRow:
    name: str
    grams: int
    calories: float
    protein_g: float
    fat_g: float
    carbs_g: float


_NUM = r"(\d+(?:[.,]\d+)?)"


def _f(x: str) -> float:
    return float(x.replace(",", "."))


def parse_ingredient_line(line: str) -> IngredientRow | None:
    s = line.strip()
    if not s:
        return None

    # grams: prefer "... 200г" or "... 200 g"
    mg = re.search(rf"{_NUM}\s*(?:г|g)\b", s, flags=re.IGNORECASE)
    if not mg:
        return None
    grams = int(round(_f(mg.group(1))))

    # calories: "... 250ккал" / "250 kcal"
    mk = re.search(rf"{_NUM}\s*(?:ккал|kcal)\b", s, flags=re.IGNORECASE)
    if not mk:
        return None
    calories = _f(mk.group(1))

    # macros: Б/Ж/У
    mp = re.search(rf"(?:\bб|\bprotein)\s*[:=]?\s*{_NUM}", s, flags=re.IGNORECASE)
    mf = re.search(rf"(?:\bж|\bfat)\s*[:=]?\s*{_NUM}", s, flags=re.IGNORECASE)
    mc = re.search(rf"(?:\bу|\bcarb|\bcarbs)\s*[:=]?\s*{_NUM}", s, flags=re.IGNORECASE)
    if not (mp and mf and mc):
        return None

    protein_g = _f(mp.group(1))
    fat_g = _f(mf.group(1))
    carbs_g = _f(mc.group(1))

    # name = text before grams occurrence
    name = s[: mg.start()].strip(" -–—:\t")
    if not name:
        name = "ингредиент"

    return IngredientRow(
        name=name,
        grams=grams,
        calories=calories,
        protein_g=protein_g,
        fat_g=fat_g,
        carbs_g=carbs_g,
    )


def parse_ingredients_block(text: str) -> list[IngredientRow]:
    rows: list[IngredientRow] = []
    for line in text.splitlines():
        r = parse_ingredient_line(line)
        if r:
            rows.append(r)
    return rows


def compute_totals(rows: list[IngredientRow]) -> dict:
    total_weight = sum(r.grams for r in rows)
    total_kcal = sum(r.calories for r in rows)
    total_p = sum(r.protein_g for r in rows)
    total_f = sum(r.fat_g for r in rows)
    total_c = sum(r.carbs_g for r in rows)

    per100 = None
    if total_weight > 0:
        per100 = {
            "calories": total_kcal / total_weight * 100,
            "protein_g": total_p / total_weight * 100,
            "fat_g": total_f / total_weight * 100,
            "carbs_g": total_c / total_weight * 100,
        }

    return {
        "total_weight_g": total_weight,
        "calories": total_kcal,
        "protein_g": total_p,
        "fat_g": total_f,
        "carbs_g": total_c,
        "per_100g": per100,
    }

