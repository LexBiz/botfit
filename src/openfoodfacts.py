from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp

from src.config import settings


@dataclass(frozen=True)
class FoodCandidate:
    source: str
    barcode: str | None
    name: str
    brand: str | None
    kcal_100g: float | None
    protein_100g: float | None
    fat_100g: float | None
    carbs_100g: float | None
    raw: dict[str, Any]


def _f(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _nutrients_from_product(prod: dict[str, Any]) -> tuple[float | None, float | None, float | None, float | None]:
    nutr = prod.get("nutriments") or {}
    kcal = _f(nutr.get("energy-kcal_100g")) or _f(nutr.get("energy-kcal_value"))
    p = _f(nutr.get("proteins_100g"))
    fat = _f(nutr.get("fat_100g"))
    carbs = _f(nutr.get("carbohydrates_100g"))
    return kcal, p, fat, carbs


async def get_by_barcode(barcode: str) -> FoodCandidate | None:
    base = settings.off_base_url.rstrip("/")
    url = f"{base}/api/v2/product/{barcode}.json"
    params = {"fields": "code,product_name,brands,nutriments"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    if data.get("status") != 1:
        return None
    prod = data.get("product") or {}
    kcal, p, fat, carbs = _nutrients_from_product(prod)
    return FoodCandidate(
        source="openfoodfacts",
        barcode=str(prod.get("code")) if prod.get("code") else str(barcode),
        name=str(prod.get("product_name") or "").strip() or f"barcode {barcode}",
        brand=str(prod.get("brands") or "").strip() or None,
        kcal_100g=kcal,
        protein_100g=p,
        fat_100g=fat,
        carbs_100g=carbs,
        raw=prod,
    )


async def search(query: str, *, page_size: int | None = None) -> list[FoodCandidate]:
    base = settings.off_base_url.rstrip("/")
    url = f"{base}/cgi/search.pl"
    ps = page_size or settings.off_page_size
    params = {
        "search_terms": query,
        "search_simple": 1,
        "action": "process",
        "json": 1,
        "page_size": ps,
        "fields": "code,product_name,brands,nutriments",
        # region hint
        "cc": settings.off_country.lower(),
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()

    out: list[FoodCandidate] = []
    for prod in (data.get("products") or [])[:ps]:
        kcal, p, fat, carbs = _nutrients_from_product(prod)
        name = str(prod.get("product_name") or "").strip()
        if not name:
            continue
        out.append(
            FoodCandidate(
                source="openfoodfacts",
                barcode=str(prod.get("code")) if prod.get("code") else None,
                name=name,
                brand=str(prod.get("brands") or "").strip() or None,
                kcal_100g=kcal,
                protein_100g=p,
                fat_100g=fat,
                carbs_100g=carbs,
                raw=prod,
            )
        )
    return out

