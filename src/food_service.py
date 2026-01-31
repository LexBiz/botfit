from __future__ import annotations

from dataclasses import asdict
from typing import Any

from src.jsonutil import dumps, loads
from src.openfoodfacts import FoodCandidate, get_by_barcode, search
from src.repositories import FoodRepo


class FoodService:
    def __init__(self, food_repo: FoodRepo):
        self.food_repo = food_repo

    async def resolve_by_barcode(self, barcode: str) -> FoodCandidate | None:
        cached = await self.food_repo.get_by_barcode("openfoodfacts", barcode)
        if cached:
            nutr = loads(cached.nutriments_json) or {}
            return FoodCandidate(
                source=cached.source,
                barcode=cached.barcode,
                name=cached.name,
                brand=cached.brand,
                kcal_100g=nutr.get("kcal_100g"),
                protein_100g=nutr.get("protein_100g"),
                fat_100g=nutr.get("fat_100g"),
                carbs_100g=nutr.get("carbs_100g"),
                raw=nutr.get("raw") or {},
            )

        cand = await get_by_barcode(barcode)
        if not cand:
            return None

        await self.food_repo.upsert(
            source=cand.source,
            barcode=cand.barcode,
            name=cand.name,
            brand=cand.brand,
            nutriments_json=dumps(
                {
                    "kcal_100g": cand.kcal_100g,
                    "protein_100g": cand.protein_100g,
                    "fat_100g": cand.fat_100g,
                    "carbs_100g": cand.carbs_100g,
                    "raw": cand.raw,
                }
            ),
        )
        return cand

    async def search(self, query: str) -> list[FoodCandidate]:
        cands = await search(query)
        # cache best-effort by barcode
        for c in cands:
            if c.barcode:
                await self.food_repo.upsert(
                    source=c.source,
                    barcode=c.barcode,
                    name=c.name,
                    brand=c.brand,
                    nutriments_json=dumps(
                        {
                            "kcal_100g": c.kcal_100g,
                            "protein_100g": c.protein_100g,
                            "fat_100g": c.fat_100g,
                            "carbs_100g": c.carbs_100g,
                            "raw": c.raw,
                        }
                    ),
                )
        return cands


def compute_item_macros(*, grams: float, cand: FoodCandidate) -> dict[str, Any] | None:
    if grams <= 0:
        return None
    if cand.kcal_100g is None or cand.protein_100g is None or cand.fat_100g is None or cand.carbs_100g is None:
        return None
    factor = grams / 100.0
    return {
        "name": cand.name,
        "brand": cand.brand,
        "barcode": cand.barcode,
        "grams": int(round(grams)),
        "calories": float(cand.kcal_100g) * factor,
        "protein_g": float(cand.protein_100g) * factor,
        "fat_g": float(cand.fat_100g) * factor,
        "carbs_g": float(cand.carbs_100g) * factor,
        "per_100g": {
            "kcal": cand.kcal_100g,
            "protein_g": cand.protein_100g,
            "fat_g": cand.fat_100g,
            "carbs_g": cand.carbs_100g,
        },
    }

