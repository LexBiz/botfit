from __future__ import annotations

from dataclasses import asdict
from typing import Any

from src.jsonutil import dumps, loads
from src.openfoodfacts import FoodCandidate, get_by_barcode, make_search_url, search
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
                image_url=nutr.get("image_url"),
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
                    "image_url": cand.image_url,
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
                            "image_url": c.image_url,
                            "raw": c.raw,
                        }
                    ),
                )
        return cands

    async def best_image_url(self, query: str) -> str:
        cands = await self.search(query)
        for c in cands:
            if c.image_url:
                return c.image_url
        return make_search_url(query)

    async def best_product_assets(self, query: str, *, store: str | None = None) -> dict[str, Any]:
        """
        Returns best-effort assets:
        - img_url: direct image (if available)
        - off_url: openfoodfacts product page (if barcode known)
        - store_url: store-specific search link (always)
        """
        # prefer a candidate that has BOTH barcode and image (more likely "exact photo")
        cands = await self.search(query)
        best: FoodCandidate | None = None
        best_score = -1
        for c in cands:
            score = 0
            if c.image_url:
                score += 3
            if c.barcode:
                score += 2
            if c.brand:
                score += 1
            if len(c.name or "") >= 6:
                score += 1
            if score > best_score:
                best_score = score
                best = c

        img_url: str | None = best.image_url if best and best.image_url else None
        barcode: str | None = best.barcode if best and best.barcode else None
        off_url: str | None = None
        if barcode:
            off_url = f"https://world.openfoodfacts.org/product/{barcode}"
        best_name: str | None = best.name if best and best.name else None
        search_query = barcode or best_name or query
        return {
            "img_url": img_url,
            "off_url": off_url,
            "barcode": barcode,
            "search_query": search_query,
            "store_url": make_store_search_url(store or "", search_query),
        }


def _has_cyrillic(s: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in s)


def _translit_ru(s: str) -> str:
    # minimal RU->LAT translit for search safety
    m = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
        "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
        "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    out = []
    for ch in s:
        lo = ch.lower()
        if lo in m:
            rep = m[lo]
            out.append(rep.upper() if ch.isupper() else rep)
        else:
            out.append(ch)
    return "".join(out)


def make_store_search_url(store: str, query: str) -> str:
    from urllib.parse import quote_plus

    q0 = (query or "").strip()
    if _has_cyrillic(q0):
        q0 = _translit_ru(q0)
    q = quote_plus(q0)
    s = (store or "").strip().lower()
    # NOTE: Store sites change often; keep simple + safe fallbacks.
    if "kaufl" in s:
        # Czech "in-store offer" catalog with product cards/photos
        return "https://prodejny.kaufland.cz/"
    if "albert" in s:
        return f"https://www.albert.cz/vyhledavani?q={q}"
    if "penny" in s or "peni" in s:
        return f"https://www.penny.cz/vyhledavani?query={q}"
    if "lidl" in s:
        return f"https://www.lidl.cz/hledat?q={q}"
    # default: best-effort single-store fallback (avoid random sites)
    return f"https://www.kaufland.cz/hledat.html?search_value={q}"


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
        "image_url": cand.image_url,
        "per_100g": {
            "kcal": cand.kcal_100g,
            "protein_g": cand.protein_100g,
            "fat_g": cand.fat_100g,
            "carbs_g": cand.carbs_100g,
        },
    }

