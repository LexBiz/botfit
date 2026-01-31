from __future__ import annotations

from tabulate import tabulate


def macros_line(kcal: int | None, p: int | None, f: int | None, c: int | None) -> str:
    if kcal is None:
        return "КБЖУ: —"
    return f"КБЖУ: {kcal} ккал | Б {p} г | Ж {f} г | У {c} г"


def recipe_table(rows: list[dict]) -> str:
    tbl_rows = []
    for r in rows:
        tbl_rows.append(
            [
                r.get("name", ""),
                r.get("grams", ""),
                r.get("calories", ""),
                r.get("protein_g", ""),
                r.get("fat_g", ""),
                r.get("carbs_g", ""),
            ]
        )

    return tabulate(
        tbl_rows,
        headers=["Продукт", "г", "ккал", "Б", "Ж", "У"],
        tablefmt="github",
    )

