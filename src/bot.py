from __future__ import annotations

import asyncio
import datetime as dt
import math
import re
import traceback
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message

from src.config import settings
from src.db import SessionLocal
from src.init_db import init_db
from src.jsonutil import dumps, loads
from aiogram.types import ReplyKeyboardRemove

from src.nutrition import compute_targets, compute_targets_with_meta, macros_for_targets
from src.audio import ogg_opus_to_wav_bytes
from src.openai_client import text_json, text_output, transcribe_audio, vision_json
from src.prompts import (
    COACH_ONBOARD_JSON,
    COACH_MEMORY_JSON,
    COACH_CHAT_GUIDE,
    PROGRESS_PHOTO_JSON,
    DAILY_CHECKIN_JSON,
    DAY_PLAN_JSON,
    MEAL_ITEMS_JSON,
    MEAL_FROM_PHOTO_FINAL_JSON,
    MEAL_FROM_TEXT_JSON,
    PLAN_EDIT_JSON,
    PHOTO_ANALYSIS_JSON,
    PHOTO_TO_ITEMS_JSON,
    ROUTER_JSON,
    SYSTEM_COACH,
    SYSTEM_NUTRITIONIST,
    WEEKLY_ANALYSIS_JSON,
)
from src.food_service import FoodService, compute_item_macros
from src.food_service import make_store_search_url
from src.keyboards import (
    BTN_CANCEL,
    BTN_DAYS_1,
    BTN_DAYS_3,
    BTN_DAYS_7,
    BTN_HELP,
    BTN_LOG_MEAL,
    BTN_MENU,
    BTN_PLAN_AFTER_TOMORROW,
    BTN_PLAN_OTHER_DATE,
    BTN_PLAN_TODAY,
    BTN_PLAN_TOMORROW,
    BTN_PHOTO_HELP,
    BTN_PLAN,
    BTN_PROFILE,
    BTN_PROGRESS,
    BTN_REMINDERS,
    BTN_TARGETS_AUTO,
    BTN_TARGETS_CUSTOM,
    BTN_WEEK,
    BTN_WEIGHT,
    goal_tempo_kb,
    main_menu_kb,
    plan_days_kb,
    plan_edit_kb,
    plan_store_kb,
    plan_when_kb,
    cancel_kb,
    BTN_STORE_ALBERT,
    BTN_STORE_ANY,
    BTN_STORE_KAUFLAND,
    BTN_STORE_LIDL,
    BTN_STORE_PENNY,
    BTN_PLAN_APPROVE,
    BTN_PLAN_REGEN,
    BTN_PLAN_EDIT_CANCEL,
    targets_mode_kb,
)
from src.render import recipe_table
from src.recipe_calc import compute_totals, parse_ingredients_block
from src.repositories import (
    CoachNoteRepo,
    DailyCheckinRepo,
    FoodRepo,
    GoalRepo,
    MealRepo,
    PlanRepo,
    PreferenceRepo,
    StatRepo,
    UserRepo,
    WeightLogRepo,
)
from src.tg_files import download_telegram_file
from src.models import CoachNote, DailyCheckin, Goal, Meal, Plan, Stat, User, WeightLog


router = Router()

def _utcnow_naive() -> dt.datetime:
    # avoid deprecated datetime.utcnow(); store as naive UTC for SQLite
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


ONBOARDING_QUESTIONS = {
    1: "Возраст? (число)",
    2: "Пол? (м/ж)",
    3: "Рост (см)?",
    4: "Вес (кг)?",
    5: "Уровень активности? (низкий/средний/высокий)",
    6: "Цель? (можно своими словами: похудение / набор / поддержка / рекомпозиция и т.п.)",
    7: "Аллергии? (если нет — напиши «нет»)",
    8: "Ограничения? (например: без свинины/халяль/веган/и т.п.; если нет — «нет»)",
    9: "Любимые продукты? (списком)",
    10: "Нелюбимые продукты? (списком)",
}


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _sanitize_ai_text(s: str) -> str:
    """
    Telegram is in HTML parse_mode. Models sometimes return Markdown with '*' which looks ugly.
    Convert common Markdown emphasis to HTML and remove remaining '*'/'_'.
    """
    if not s:
        return s
    t = s.strip()
    # convert common markdown emphasis
    try:
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
        t = re.sub(r"__(.+?)__", r"<b>\1</b>", t, flags=re.S)
        # italics: single * or _
        t = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", t, flags=re.S)
        t = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"<i>\1</i>", t, flags=re.S)
    except Exception:
        pass
    # normalize bullets a bit
    t = t.replace("•", "- ")
    # remove leftover markdown tokens
    t = t.replace("*", "").replace("_", "")
    return t


def _safe_nonempty_text(s: str | None, *, fallback: str) -> str:
    t = (s or "").strip()
    return t if t else fallback


async def _send_html_lines(
    message: Message,
    *,
    header: str,
    lines: list[str],
    reply_markup: Any = None,
    limit: int = 3900,
) -> None:
    """
    Telegram has a hard 4096 limit; we keep some headroom.
    IMPORTANT: never cut HTML tags (e.g. <a href="...">) by slicing mid-string.
    We chunk by whole lines only.
    """
    chunks: list[str] = []
    cur = header.strip()
    for ln in lines:
        ln = str(ln or "").strip()
        if not ln:
            continue
        cand = cur + ("\n" if cur else "") + ln
        if len(cand) <= limit:
            cur = cand
            continue
        # flush current chunk
        if cur:
            chunks.append(cur)
        # start new chunk with header repeated for clarity
        cur = header.strip() + "\n" + ln
        if len(cur) > limit:
            # if a single line is too long, drop links safely (avoid malformed HTML)
            safe_ln = re.sub(r"\s*<a href=\"[^\"]+\">[^<]+</a>\s*(\|\s*)?", " ", ln).strip()
            cur = header.strip() + "\n" + safe_ln[: max(0, limit - len(header) - 1)]
    if cur:
        chunks.append(cur)

    for ch in chunks[:5]:  # safety: don't spam
        await message.answer(ch, reply_markup=reply_markup or main_menu_kb())


def _has_cyrillic_text(s: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in (s or ""))


def _coerce_number(x: Any) -> float | None:
    """
    Best-effort number parser for model outputs.
    Accepts: 120, 120.5, "120", "120 г", "120g", "≈120", "120,5".
    Returns float or None.
    """
    try:
        if isinstance(x, bool):
            return None
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, str):
            s = x.strip().replace(",", ".")
            m = re.search(r"-?\d+(?:\.\d+)?", s)
            if m:
                return float(m.group(0))
    except Exception:
        return None
    return None


def _scrub_secrets(s: str) -> str:
    """
    Avoid leaking tokens in user-facing errors/logs.
    Very simple masking for OpenAI-style keys.
    """
    if not s:
        return s
    return re.sub(r"\bsk-[A-Za-z0-9]{10,}\b", "sk-***", s)


def _escape_html(s: str) -> str:
    # minimal HTML escaping for user-facing debug snippets
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _normalize_day_plan(plan: dict[str, Any], *, store_only: str | None) -> dict[str, Any]:
    """
    Make the day plan more tolerant to slightly-off model JSON.
    - Coerce numeric fields from strings ("120 г" -> 120.0)
    - Ensure required structures are lists/dicts where possible
    - Enforce store_only if provided (do not persist; only for this plan output)
    """
    if not isinstance(plan, dict):
        return {}

    so = (store_only or "").strip()
    if so.lower() == "any":
        so = ""

    meals = plan.get("meals")
    if not isinstance(meals, list):
        meals = []

    norm_meals: list[dict[str, Any]] = []
    for m in meals:
        if not isinstance(m, dict):
            continue
        prods = m.get("products")
        if not isinstance(prods, list):
            prods = []
        norm_prods: list[dict[str, Any]] = []
        for p in prods:
            if not isinstance(p, dict):
                continue
            name = str(p.get("name") or "").strip()
            grams = _coerce_number(p.get("grams"))
            if not name or grams is None or grams <= 0:
                continue
            store = str(p.get("store") or "").strip()
            if so:
                store = so
            if not store:
                store = "Lidl"
            norm_prods.append({"name": name, "grams": float(grams), "store": store})

        kcal = _coerce_number(m.get("kcal"))
        prot = _coerce_number(m.get("protein_g"))
        fat = _coerce_number(m.get("fat_g"))
        carbs = _coerce_number(m.get("carbs_g"))
        recipe = m.get("recipe")
        if not isinstance(recipe, list):
            recipe = []
        recipe2 = [str(x).strip() for x in recipe if str(x or "").strip()]

        nm: dict[str, Any] = {
            "time": str(m.get("time") or "").strip(),
            "title": str(m.get("title") or "").strip(),
            "products": norm_prods,
            "recipe": recipe2,
        }
        if kcal is not None:
            nm["kcal"] = float(kcal)
        if prot is not None:
            nm["protein_g"] = float(prot)
        if fat is not None:
            nm["fat_g"] = float(fat)
        if carbs is not None:
            nm["carbs_g"] = float(carbs)
        if nm["products"]:
            norm_meals.append(nm)

    totals = plan.get("totals")
    if not isinstance(totals, dict):
        totals = {}
    tot_kcal = _coerce_number(totals.get("kcal"))
    tot_p = _coerce_number(totals.get("protein_g"))
    tot_f = _coerce_number(totals.get("fat_g"))
    tot_c = _coerce_number(totals.get("carbs_g"))
    if tot_kcal is None:
        tot_kcal = sum(float(mm.get("kcal") or 0) for mm in norm_meals)
    norm_totals: dict[str, Any] = {"kcal": float(tot_kcal)}
    if tot_p is not None:
        norm_totals["protein_g"] = float(tot_p)
    if tot_f is not None:
        norm_totals["fat_g"] = float(tot_f)
    if tot_c is not None:
        norm_totals["carbs_g"] = float(tot_c)

    sl = plan.get("shopping_list")
    if not isinstance(sl, list):
        sl = []
    norm_sl: list[dict[str, Any]] = []
    for it in sl:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        grams = _coerce_number(it.get("grams"))
        if not name or grams is None or grams <= 0:
            continue
        store = str(it.get("store") or "").strip()
        if so:
            store = so
        if not store:
            store = "Lidl"
        norm_sl.append({"name": name, "grams": float(grams), "store": store})
    if not norm_sl:
        for mm in norm_meals:
            for pp in (mm.get("products") or []):
                if isinstance(pp, dict):
                    norm_sl.append(pp)

    return {"meals": norm_meals, "totals": norm_totals, "shopping_list": norm_sl}


def _plan_quality_ok(plan: dict[str, Any], kcal_target: int) -> bool:
    try:
        meals = plan.get("meals") or []
        if not isinstance(meals, list) or not meals:
            return False
        # ban supplements / powders unless explicitly requested (common low-quality failure)
        banned = ["whey", "protein powder", "mass gainer", "gainer", "bca", "bcaa", "creatine", "протеин", "сыворот", "гейнер", "креатин"]
        for m in meals:
            prods = (m or {}).get("products") or []
            if not isinstance(prods, list) or not prods:
                return False
            for p in prods:
                name = str((p or {}).get("name") or "").strip()
                grams = _coerce_number((p or {}).get("grams"))
                if not name or grams is None or grams <= 0:
                    return False
                low = name.lower()
                if any(b in low for b in banned):
                    return False
        totals = plan.get("totals") or {}
        kcal = _coerce_number(totals.get("kcal"))
        if kcal is None:
            kcal = sum(float(_coerce_number((m or {}).get("kcal")) or 0) for m in meals)
        kcal = float(kcal or 0)
        return abs(kcal - float(kcal_target)) <= float(kcal_target) * 0.07
    except Exception:
        return False


async def _load_day_plans(*, plan_repo: PlanRepo, user_id: int, start_date: dt.date, days: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(days):
        d = start_date + dt.timedelta(days=i)
        p = await plan_repo.get_day_plan_json(user_id, d)
        out.append(p or {})
    return out


async def _send_plans(
    message: Message,
    *,
    db: Any,
    user: Any,
    start_date: dt.date,
    day_plans: list[dict[str, Any]],
    store_only: str | None,
) -> None:
    food_service = FoodService(FoodRepo(db))

    def _norm_name(n: str) -> str:
        return re.sub(r"\s+", " ", n.strip().lower())

    def _suggest_buy(name: str, grams: float) -> str:
        n = _norm_name(name)
        g = max(float(grams), 0.0)
        if "яйц" in n:
            pcs = max(1, int(math.ceil(g / 60.0)))
            packs = int(math.ceil(pcs / 10.0))
            return f"Купить: ~{packs}×10 шт (нужно ~{pcs} шт)"
        step = 100.0
        if any(k in n for k in ["рис", "паст", "овся", "греч", "мук", "круп", "макарон"]):
            step = 500.0
        elif any(k in n for k in ["кур", "индей", "говя", "свини", "рыб", "лосос", "тунец"]):
            step = 500.0
        elif any(k in n for k in ["йогур", "творог", "скыр", "сыр", "молок", "кефир"]):
            step = 200.0
        buy = int(math.ceil(g / step) * step)
        packs = int(math.ceil(g / step))
        return f"Купить: ~{buy:.0f} г ({packs}×{step:.0f} г) — ориентир"

    # aggregate shopping list across days
    if store_only:
        store_only = str(store_only).strip() or None
        if store_only and store_only.lower() == "any":
            store_only = None

    agg: dict[tuple[str, str], float] = {}
    display: dict[tuple[str, str], str] = {}
    for plan in day_plans:
        sl = plan.get("shopping_list")
        if not sl:
            sl = []
            for m in (plan.get("meals") or []):
                for p in (m.get("products") or []):
                    sl.append(p)
        for it in (sl or []):
            name = str(it.get("name") or "").strip()
            store = str(it.get("store") or "").strip() or "Lidl"
            if store_only:
                store = store_only
            grams = float(it.get("grams") or 0)
            if not name or grams <= 0:
                continue
            key = (_norm_name(name), store)
            agg[key] = agg.get(key, 0.0) + grams
            display.setdefault(key, name)

    items_sorted = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)
    shopping_lines: list[str] = []
    for (norm, store), grams in items_sorted[:25]:
        orig_name = display.get((norm, store), norm)
        # Speed/UX: user asked we can drop photo/OFF lookups. Keep only store search link + grams.
        display_name = orig_name
        store_url = make_store_search_url(store, orig_name)
        search_query = orig_name
        buy_hint = _suggest_buy(display_name, grams)
        links: list[str] = []
        if isinstance(store_url, str) and store_url:
            links.append(f"<a href=\"{store_url}\">🛒 {store}</a>")
        q_hint = f" 🔎 запрос: <code>{search_query}</code>" if isinstance(search_query, str) and search_query else ""
        shopping_lines.append(f"- <b>{display_name}</b> — {grams:.0f} г ({store}). {buy_hint}. " + " | ".join(links) + q_hint)

    days = len(day_plans)
    parts: list[str] = [f"🍽️ <b>Рацион на {days} дн.</b> 📅 Старт: <b>{start_date.isoformat()}</b>"]
    for di, plan in enumerate(day_plans):
        d = start_date + dt.timedelta(days=di)
        meals = plan.get("meals") or []
        totals = plan.get("totals") or {}
        parts.append(f"\n📅 <b>День {di+1} — {d.isoformat()}</b>")
        for i, m in enumerate(meals, start=1):
            tm = str(m.get("time") or "").strip()
            tm_txt = f"{tm} — " if tm else ""
            parts.append(
                f"\n🍽️ <b>{i}. {tm_txt}{m.get('title')}</b>\n"
                f"🔥 КБЖУ: {m.get('kcal')} ккал | 🥩 Б {m.get('protein_g')} | 🧈 Ж {m.get('fat_g')} | 🍚 У {m.get('carbs_g')}\n"
                "🧺 Продукты:\n"
                + "\n".join([f"- {p.get('name')} — {p.get('grams')} г ({p.get('store')})" for p in (m.get("products") or [])])
                + "\n👨‍🍳 Рецепт:\n"
                + "\n".join([f"- {s}" for s in (m.get("recipe") or [])])
            )
        parts.append(
            f"\n✅ <b>Итого дня</b>: 🔥 {totals.get('kcal')} ккал | 🥩 {totals.get('protein_g')} | 🧈 {totals.get('fat_g')} | 🍚 {totals.get('carbs_g')}"
        )

    await message.answer("\n".join(parts)[:3900], reply_markup=main_menu_kb())
    if shopping_lines:
        await _send_html_lines(
            message,
            header="🛒 <b>Список покупок (суммарно)</b>:",
            lines=shopping_lines,
            reply_markup=main_menu_kb(),
        )

def _active_targets(
    *,
    prefs: dict[str, Any],
    user: Any,
    date_local: dt.date,
) -> dict[str, Any]:
    """
    Resolve "active targets" for a given local date from preferences.targets.
    Returns dict: kcal, protein_g, fat_g, carbs_g, source, store
    """
    targ = prefs.get("targets") if isinstance(prefs.get("targets"), dict) else {}
    source = str(prefs.get("targets_source") or "").strip().lower() or "coach"

    def _k(d: dt.date) -> int | None:
        if isinstance(targ, dict):
            wd = d.weekday()
            is_weekday = wd < 5
            if is_weekday and isinstance(targ.get("calories_weekdays"), (int, float)):
                return int(targ.get("calories_weekdays"))
            if (not is_weekday) and isinstance(targ.get("calories_weekends"), (int, float)):
                return int(targ.get("calories_weekends"))
            if isinstance(targ.get("calories"), (int, float)):
                return int(targ.get("calories"))
        return int(user.calories_target) if user.calories_target is not None else None

    kcal = _k(date_local)
    p = int(targ.get("protein_g")) if isinstance(targ, dict) and isinstance(targ.get("protein_g"), (int, float)) else (user.protein_g_target)
    f = int(targ.get("fat_g")) if isinstance(targ, dict) and isinstance(targ.get("fat_g"), (int, float)) else (user.fat_g_target)
    c = int(targ.get("carbs_g")) if isinstance(targ, dict) and isinstance(targ.get("carbs_g"), (int, float)) else (user.carbs_g_target)

    # If macros missing but kcal present -> compute deterministic macros
    if kcal is not None and (p is None or f is None or c is None) and user.weight_kg and user.goal:
        try:
            mt = macros_for_targets(int(kcal), weight_kg=float(user.weight_kg), goal=user.goal)  # type: ignore[arg-type]
            p = mt.protein_g
            f = mt.fat_g
            c = mt.carbs_g
        except Exception:
            pass

    store = str(prefs.get("preferred_store") or "any")
    return {"kcal": kcal, "protein_g": p, "fat_g": f, "carbs_g": c, "source": source, "store": store}


def _parse_int(s: str) -> int | None:
    s = _norm_text(s)
    m = re.search(r"(\d+)", s)
    if not m:
        return None
    return int(m.group(1))


def _parse_float(s: str) -> float | None:
    s = _norm_text(s).replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    return float(m.group(1))


def _map_sex(s: str) -> str | None:
    s = _norm_text(s)
    if s in {"м", "m", "male", "муж", "мужчина", "мужской"}:
        return "male"
    if s in {"ж", "f", "female", "жен", "женщина", "женский"}:
        return "female"
    return None


def _map_activity(s: str) -> str | None:
    s = _norm_text(s)
    if "низ" in s:
        return "low"
    if "сред" in s:
        return "medium"
    if "выс" in s:
        return "high"
    return None


def _map_goal(s: str) -> str | None:
    s = _norm_text(s)
    if "пох" in s or "суш" in s or "сниз" in s:
        return "loss"
    if "подд" in s or "поддерж" in s or "держ" in s:
        return "maintain"
    if "набор" in s or "мас" in s:
        return "gain"
    if "рекомп" in s or "рекомпоз" in s or "recomp" in s or "recomposition" in s:
        return "recomp"
    if "подтян" in s or "тонус" in s:
        return "recomp"
    return None


def _parse_tempo_choice(s: str) -> tuple[str, float] | None:
    t = _norm_text(s)
    if "жест" in t or "жёст" in t or "быстр" in t or "🔥" in s:
        return "hard", 0.25
    if "станд" in t or "✅" in s:
        return "standard", 0.15
    if "мяг" in t or "🟢" in s:
        return "soft", 0.10
    if "рекомп" in t or "🧱" in s:
        return "recomp", 0.10
    if "поддерж" in t or "⚖" in s:
        return "maintain", 0.0
    if "набор" in t or "📈" in s:
        return "gain", -0.10
    return None


def _fmt_goal(goal: str) -> str:
    return {
        "loss": "похудение",
        "maintain": "поддержание",
        "gain": "набор",
        "recomp": "рекомпозиция",
    }.get(goal, goal)


def _fmt_pct(p: float) -> str:
    return f"{abs(p)*100:.0f}%"


GOAL_TEMPO = {
    "soft": ("Мягко", 0.10),
    "standard": ("Стандарт", 0.15),
    "hard": ("Жёстко", 0.25),
    "recomp": ("Рекомпозиция", 0.10),
    "maintain": ("Поддержание", 0.0),
    "gain": ("Набор", -0.10),
}


async def _start_onboarding(message: Message, user_repo: UserRepo, user: Any) -> None:
    await user_repo.set_dialog(user, state="onboarding", step=1, data={"answers": {}})
    await message.answer(
        "Привет! Я твой персональный AI‑нутриционист.\n"
        "Сейчас задам 10 вопросов и рассчитаю норму калорий и БЖУ.\n\n"
        f"1/10 — {ONBOARDING_QUESTIONS[1]}"
    )

async def _start_coach_onboarding(message: Message, user_repo: UserRepo, user: Any) -> None:
    # AI-first onboarding: user can answer freely, we extract + ask only what missing
    await user_repo.set_dialog(user, state="coach_onboarding", step=1, data={"profile": {}, "prefs": {}})
    await message.answer(
        "Ок, делаем по-взрослому — как с тренером.\n"
        "Напиши одним сообщением (можно своими словами):\n"
        "- возраст, пол, рост, вес\n"
        "- активность (сидячая/средняя/высокая)\n"
        "- цель (похудение/поддержание/набор/рекомпозиция) и темп (мягко/стандарт/жёстко)\n"
        "- режим дня: во сколько встаёшь/ложишься, сколько приёмов пищи, есть ли перекусы\n"
        "- аллергии/ограничения, любимое/нелюбимое\n\n"
        "Пример: «29 м 190/118, активность средняя, хочу рекомпозицию, темп стандарт, 3 приёма + перекус, "
        "встаю 07:30, сплю 23:30, без лактозы, люблю курицу/рис, не люблю рыбу».",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    async with SessionLocal() as db:
        repo = UserRepo(db)
        user = await repo.get_or_create(message.from_user.id, message.from_user.username if message.from_user else None)

        if user.profile_complete:
            await message.answer(
                "Ты уже заполнил профиль.\n"
                "Команды: /profile, /reset, /help\n"
                "Можешь прислать фото еды / написать прием пищи / попросить рацион."
                ,
                reply_markup=main_menu_kb(),
            )
            return
        # default to AI-coach onboarding
        await _start_coach_onboarding(message, repo, user)
        await db.commit()


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    async with SessionLocal() as db:
        repo = UserRepo(db)
        pref_repo = PreferenceRepo(db)
        user = await repo.get_or_create(message.from_user.id, message.from_user.username if message.from_user else None)
        if not user.profile_complete:
            await message.answer("Профиль не заполнен. Напиши /start чтобы пройти анкету.")
            return

        prefs = await pref_repo.get_json(user.id)
        tz = _tz_from_prefs(prefs)
        today_local = dt.datetime.now(dt.timezone.utc).astimezone(tz).date()
        active = _active_targets(prefs=prefs, user=user, date_local=today_local)

        deficit_pct = prefs.get("deficit_pct")
        coach_t, meta = compute_targets_with_meta(
            sex=user.sex,  # type: ignore[arg-type]
            age=user.age,
            height_cm=user.height_cm,
            weight_kg=user.weight_kg,
            activity=user.activity_level,  # type: ignore[arg-type]
            goal=user.goal,  # type: ignore[arg-type]
            deficit_pct=float(deficit_pct) if deficit_pct is not None else None,
        )

        await message.answer(
            "👤 <b>Твой профиль</b> 📋\n"
            f"🎂 Возраст: <b>{user.age}</b>\n"
            f"🚻 Пол: <b>{user.sex}</b>\n"
            f"📏 Рост: <b>{user.height_cm} см</b>\n"
            f"⚖️ Вес: <b>{user.weight_kg} кг</b>\n"
            f"🏃 Активность: <b>{user.activity_level}</b>\n"
            f"🎯 Цель: <b>{_fmt_goal(user.goal)}</b>\n\n"
            "🎯 <b>Активные цели (как договорились)</b>\n"
            f"🔥 Калории: <b>{active.get('kcal')}</b> ккал\n"
            f"🥩🧈🍚 БЖУ: <b>{active.get('protein_g')}/{active.get('fat_g')}/{active.get('carbs_g')} г</b>\n"
            f"🧠 Источник: <b>{'custom' if active.get('source')=='custom' else 'coach'}</b>\n"
            f"🛒 Магазин: <b>{active.get('store')}</b>\n\n"
            "🧮 <b>Расчёт тренера (справочно)</b>\n"
            f"⚡ TDEE: <b>{meta.tdee_kcal} ккал</b>\n"
            f"📉 Дефицит: <b>{meta.deficit_kcal} ккал/день</b> ({_fmt_pct(meta.deficit_pct)})\n"
            f"🎯 Норма тренера: <b>{coach_t.calories} ккал</b>\n"
            f"🥩🧈🍚 БЖУ тренера: <b>{coach_t.protein_g}/{coach_t.fat_g}/{coach_t.carbs_g} г</b>",
            reply_markup=main_menu_kb(),
        )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Команды:\n"
        "- /start — анкета и расчет нормы\n"
        "- /profile — профиль и текущая норма\n"
        "- /weight 82.5 — обновить вес и пересчитать\n"
        "- /plan — рацион на день (Чехия: Lidl/Kaufland/Albert)\n"
        "- /week — анализ дневника за 7 дней\n"
        "- /recipe — расчет рецепта по ингредиентам (КБЖУ)\n"
        "- /reset — сброс профиля"
        "\n\nМожно и без команд — используй кнопки меню ниже.",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("weight"))
async def cmd_weight(message: Message) -> None:
    if not message.from_user:
        return
    text = (message.text or "").strip()
    payload = text[len("/weight") :].strip()
    w = _parse_float(payload) if payload else None
    if w is None:
        await message.answer("Формат: /weight 82.5")
        return

    async with SessionLocal() as db:
        repo = UserRepo(db)
        pref_repo = PreferenceRepo(db)
        user = await repo.get_or_create(message.from_user.id, message.from_user.username)
        if not user.profile_complete:
            await message.answer("Сначала заполним профиль: /start")
            return

        user.weight_kg = float(w)
        prefs = await pref_repo.get_json(user.id)
        deficit_pct = prefs.get("deficit_pct")
        t, meta = compute_targets_with_meta(
            sex=user.sex,  # type: ignore[arg-type]
            age=user.age,
            height_cm=user.height_cm,
            weight_kg=user.weight_kg,
            activity=user.activity_level,  # type: ignore[arg-type]
            goal=user.goal,  # type: ignore[arg-type]
            deficit_pct=float(deficit_pct) if deficit_pct is not None else None,
        )
        # Update meta always, but do NOT overwrite custom targets
        targets_source = str(prefs.get("targets_source") or "coach").strip().lower()
        if targets_source != "custom":
            user.calories_target = t.calories
            user.protein_g_target = t.protein_g
            user.fat_g_target = t.fat_g
            user.carbs_g_target = t.carbs_g
            await pref_repo.merge(
                user.id,
                {"targets_source": "coach", "targets": {"calories": t.calories, "protein_g": t.protein_g, "fat_g": t.fat_g, "carbs_g": t.carbs_g}},
            )
        else:
            # keep active custom targets mirrored into user table for /profile consistency
            tz = _tz_from_prefs(prefs)
            today_local = dt.datetime.now(dt.timezone.utc).astimezone(tz).date()
            active = _active_targets(prefs=prefs, user=user, date_local=today_local)
            if active.get("kcal") is not None:
                user.calories_target = int(active["kcal"])
            if active.get("protein_g") is not None:
                user.protein_g_target = int(active["protein_g"])
            if active.get("fat_g") is not None:
                user.fat_g_target = int(active["fat_g"])
            if active.get("carbs_g") is not None:
                user.carbs_g_target = int(active["carbs_g"])
        await pref_repo.merge(
            user.id,
            {"bmr_kcal": meta.bmr_kcal, "tdee_kcal": meta.tdee_kcal, "deficit_pct": meta.deficit_pct},
        )
        await db.commit()

    await message.answer(
        f"⚖️ Вес обновил: <b>{w} кг</b> ✅\n\n"
        f"⚡ TDEE: <b>{meta.tdee_kcal} ккал</b>\n"
        f"📉 Дефицит: <b>{meta.deficit_kcal} ккал/день</b> ({_fmt_pct(meta.deficit_pct)})\n"
        f"🎯 Текущая цель: <b>{user.calories_target} ккал</b>\n"
        f"🥩🧈🍚 БЖУ: <b>{user.protein_g_target}/{user.fat_g_target}/{user.carbs_g_target} г</b>"
        ,
        reply_markup=main_menu_kb(),
    )

@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    async with SessionLocal() as db:
        repo = UserRepo(db)
        user = await repo.get_or_create(message.from_user.id, message.from_user.username if message.from_user else None)
        # wipe durable history (meals/plans/stats/notes/goals/weights/checkins) + preferences json
        try:
            await db.execute(delete(Meal).where(Meal.user_id == user.id))
            await db.execute(delete(Plan).where(Plan.user_id == user.id))
            await db.execute(delete(Stat).where(Stat.user_id == user.id))
            await db.execute(delete(CoachNote).where(CoachNote.user_id == user.id))
            await db.execute(delete(Goal).where(Goal.user_id == user.id))
            await db.execute(delete(WeightLog).where(WeightLog.user_id == user.id))
            await db.execute(delete(DailyCheckin).where(DailyCheckin.user_id == user.id))
            # clear preferences json safely (no delete/create)
            pref_repo = PreferenceRepo(db)
            await pref_repo.set_json(user.id, {})
        except Exception:
            pass
        user.profile_complete = False
        user.age = None
        user.sex = None
        user.height_cm = None
        user.weight_kg = None
        user.activity_level = None
        user.goal = None
        user.allergies = None
        user.restrictions = None
        user.favorite_products = None
        user.disliked_products = None
        user.calories_target = None
        user.protein_g_target = None
        user.fat_g_target = None
        user.carbs_g_target = None
        await repo.set_dialog(user, state=None, step=None, data=None)
        await db.commit()
    await message.answer("🧹 Память и профиль сброшены полностью ✅\n\n🚀 Напиши /start — пройдём анкету заново.", reply_markup=main_menu_kb())


async def _handle_onboarding_step(message: Message, user_repo: UserRepo, user: Any) -> bool:
    if user.dialog_state != "onboarding" or not user.dialog_step:
        return False

    step = int(user.dialog_step)
    text = message.text or ""
    data = await user_repo.get_dialog_data(user) or {"answers": {}}
    answers: dict[str, Any] = data.get("answers", {})

    if step == 1:
        age = _parse_int(text)
        if age is None or not (10 <= age <= 100):
            await message.answer("Возраст числом (пример: 29).")
            return True
        answers["age"] = age

    elif step == 2:
        sex = _map_sex(text)
        if sex is None:
            await message.answer("Пол: напиши «м» или «ж».")
            return True
        answers["sex"] = sex

    elif step == 3:
        h = _parse_float(text)
        if h is None or not (120 <= h <= 230):
            await message.answer("Рост в см (пример: 178).")
            return True
        answers["height_cm"] = h

    elif step == 4:
        w = _parse_float(text)
        if w is None or not (30 <= w <= 300):
            await message.answer("Вес в кг (пример: 82.5).")
            return True
        answers["weight_kg"] = w

    elif step == 5:
        a = _map_activity(text)
        if a is None:
            await message.answer("Активность: низкий / средний / высокий.")
            return True
        answers["activity_level"] = a

    elif step == 6:
        if data.get("awaiting_goal_tempo"):
            tempo = _parse_tempo_choice(text)
            if tempo is None:
                await message.answer("Выбери темп кнопкой ниже.", reply_markup=goal_tempo_kb())
                return True
            tempo_key, deficit_pct = tempo
            # keep goal, but allow explicit override by tempo choice
            if tempo_key == "maintain":
                answers["goal"] = "maintain"
            elif tempo_key == "gain":
                answers["goal"] = "gain"
            elif tempo_key == "recomp":
                answers["goal"] = "recomp"
            else:
                # for soft/standard/hard we assume fat loss mode
                answers["goal"] = answers.get("goal") or "loss"
                if answers["goal"] not in {"loss", "recomp"}:
                    answers["goal"] = "loss"

            answers["deficit_pct"] = float(deficit_pct)
            answers["tempo_key"] = tempo_key

            # advance to next question
            next_step = step + 1
            await user_repo.set_dialog(user, state="onboarding", step=next_step, data={"answers": answers})
            await message.answer(f"{next_step}/10 — {ONBOARDING_QUESTIONS[next_step]}", reply_markup=ReplyKeyboardRemove())
            return True

        g = _map_goal(text)
        if g is None:
            await message.answer("Напиши цель (например: «рекомпозиция», «похудение до 105 кг», «набор»).")
            return True

        answers["goal"] = g
        answers["goal_raw"] = (message.text or "").strip()

        # show tempo previews (kcal) so user can choose correctly
        preview: dict[str, int] | None
        preview_text: str
        try:
            tdee_only = compute_targets_with_meta(
                sex=answers["sex"],
                age=int(answers["age"]),
                height_cm=float(answers["height_cm"]),
                weight_kg=float(answers["weight_kg"]),
                activity=answers["activity_level"],
                goal=str(answers["goal"]),
                deficit_pct=0.0,
            )[1].tdee_kcal
            preview = {
                "soft": int(round(tdee_only * (1 - 0.10))),
                "standard": int(round(tdee_only * (1 - 0.15))),
                "hard": int(round(tdee_only * (1 - 0.25))),
                "recomp": int(round(tdee_only * (1 - 0.10))),
                "maintain": int(round(tdee_only)),
                "gain": int(round(tdee_only * (1 + 0.10))),
            }
            preview_text = (
                f"При твоих данных поддержание (TDEE) ≈ <b>{tdee_only} ккал</b>.\n"
                f"Если выбрать темп:\n"
                f"- 🟢 Мягко (~10%): ~{preview['soft']} ккал\n"
                f"- ✅ Стандарт (~15%): ~{preview['standard']} ккал\n"
                f"- 🔥 Жёстко (~25%): ~{preview['hard']} ккал\n"
            )
        except Exception:
            preview = None
            preview_text = ""
        await user_repo.set_dialog(
            user,
            state="onboarding",
            step=step,
            data={"answers": answers, "awaiting_goal_tempo": True},
        )
        await message.answer(
            f"Ок, цель: <b>{_fmt_goal(g)}</b>.\n"
            + (preview_text + "\n" if preview_text else "")
            + "Теперь выбери темп (он влияет на дефицит/профицит):",
            reply_markup=goal_tempo_kb(preview),
        )
        return True

    elif step == 7:
        answers["allergies"] = text.strip()

    elif step == 8:
        answers["restrictions"] = text.strip()

    elif step == 9:
        answers["favorite_products"] = text.strip()

    elif step == 10:
        answers["disliked_products"] = text.strip()

        # finalize
        user.age = int(answers["age"])
        user.sex = str(answers["sex"])
        user.height_cm = float(answers["height_cm"])
        user.weight_kg = float(answers["weight_kg"])
        user.activity_level = str(answers["activity_level"])
        user.goal = str(answers["goal"])
        user.allergies = str(answers.get("allergies") or "")
        user.restrictions = str(answers.get("restrictions") or "")
        user.favorite_products = str(answers.get("favorite_products") or "")
        user.disliked_products = str(answers.get("disliked_products") or "")

        # defaults
        if not user.country:
            user.country = settings.default_country
        if not user.stores_csv:
            user.stores_csv = settings.default_stores

        pref_repo = PreferenceRepo(user_repo.db)
        deficit_pct = answers.get("deficit_pct")
        goal_raw = answers.get("goal_raw")
        tempo_key = answers.get("tempo_key")

        t, meta = compute_targets_with_meta(
            sex=user.sex,  # type: ignore[arg-type]
            age=user.age,
            height_cm=user.height_cm,
            weight_kg=user.weight_kg,
            activity=user.activity_level,  # type: ignore[arg-type]
            goal=user.goal,  # type: ignore[arg-type]
            deficit_pct=float(deficit_pct) if deficit_pct is not None else None,
        )
        user.calories_target = t.calories
        user.protein_g_target = t.protein_g
        user.fat_g_target = t.fat_g
        user.carbs_g_target = t.carbs_g
        user.profile_complete = True

        # store “truth” of calculation in preferences (no schema changes)
        await pref_repo.merge(
            user.id,
            {
                "goal_raw": goal_raw,
                "tempo_key": tempo_key,
                "deficit_pct": meta.deficit_pct,
                "bmr_kcal": meta.bmr_kcal,
                "tdee_kcal": meta.tdee_kcal,
            },
        )
        # durable coach memory
        try:
            note_repo = CoachNoteRepo(user_repo.db)
            await note_repo.add_note(
                user_id=user.id,
                kind="profile_set",
                title="Профиль создан",
                note_json={
                    "goal": user.goal,
                    "tempo_key": tempo_key,
                    "goal_raw": goal_raw,
                    "tdee_kcal": meta.tdee_kcal,
                    "calories_target": t.calories,
                    "macros": {"p": t.protein_g, "f": t.fat_g, "c": t.carbs_g},
                },
            )
        except Exception:
            pass

        await user_repo.set_dialog(user, state=None, step=None, data=None)

        await message.answer(
            "Готово! Рассчитал твою норму и сохранил профиль.\n\n"
            f"Поддержание (TDEE): <b>{meta.tdee_kcal} ккал</b>\n"
            f"Цель: <b>{_fmt_goal(user.goal)}</b>, темп: <b>{_fmt_pct(meta.deficit_pct)}</b>\n"
            f"Дефицит: <b>{meta.deficit_kcal} ккал/день</b>\n\n"
            f"Целевая норма: <b>{t.calories} ккал</b>\n"
            f"БЖУ: <b>{t.protein_g}/{t.fat_g}/{t.carbs_g} г</b>\n\n"
            "Дальше можешь:\n"
            "- прислать фото еды\n"
            "- написать список продуктов/ингредиентов\n"
            "- попросить «составь рацион на день»\n"
            "Команды: /profile, /reset"
            ,
            reply_markup=main_menu_kb(),
        )
        return True

    # advance
    next_step = step + 1
    await user_repo.set_dialog(user, state="onboarding", step=next_step, data={"answers": answers})
    await message.answer(f"{next_step}/10 — {ONBOARDING_QUESTIONS[next_step]}")
    return True


async def _handle_coach_onboarding(message: Message, user_repo: UserRepo, user: Any) -> bool:
    if user.dialog_state != "coach_onboarding":
        return False
    if not message.text:
        await message.answer("Пришли текстом (одним сообщением).", reply_markup=ReplyKeyboardRemove())
        return True

    pref_repo = PreferenceRepo(user_repo.db)
    prefs = await pref_repo.get_json(user.id)
    data = await user_repo.get_dialog_data(user) or {"profile": {}, "prefs": {}}
    prof = data.get("profile") or {}
    pref_local = data.get("prefs") or {}

    extracted = await text_json(
        system=f"{SYSTEM_COACH}\n\n{COACH_ONBOARD_JSON}",
        user=(
            "Текущий профиль (что уже известно):\n"
            + dumps(
                {
                    "age": user.age,
                    "sex": user.sex,
                    "height_cm": user.height_cm,
                    "weight_kg": user.weight_kg,
                    "activity_level": user.activity_level,
                    "goal": user.goal,
                    "allergies": user.allergies,
                    "restrictions": user.restrictions,
                    "favorite_products": user.favorite_products,
                    "disliked_products": user.disliked_products,
                }
            )
            + "\nТекущие предпочтения:\n"
            + dumps(prefs)
            + "\nЛокально собранные данные (в этой сессии):\n"
            + dumps({"profile": prof, "prefs": pref_local})
            + "\nСообщение пользователя:\n"
            + (message.text or "").strip()
        ),
        max_output_tokens=900,
    )

    profile_patch = extracted.get("profile_patch") or {}
    prefs_patch = extracted.get("preferences_patch") or {}
    qs = extracted.get("clarifying_questions") or []

    def _num(x: Any) -> float | None:
        try:
            if x is None:
                return None
            return float(x)
        except Exception:
            return None

    # validate/normalize patches
    if (a := _num(profile_patch.get("age"))) is not None and 10 <= a <= 100:
        prof["age"] = int(round(a))
    if (s := profile_patch.get("sex")) in {"male", "female"}:
        prof["sex"] = str(s)
    if (h := _num(profile_patch.get("height_cm"))) is not None and 120 <= h <= 230:
        prof["height_cm"] = float(h)
    if (w := _num(profile_patch.get("weight_kg"))) is not None and 30 <= w <= 300:
        prof["weight_kg"] = float(w)
    if (act := profile_patch.get("activity_level")) in {"low", "medium", "high"}:
        prof["activity_level"] = str(act)
    if (g := profile_patch.get("goal")) in {"loss", "maintain", "gain", "recomp"}:
        prof["goal"] = str(g)
    if isinstance(profile_patch.get("allergies"), str):
        prof["allergies"] = str(profile_patch.get("allergies")).strip()
    if isinstance(profile_patch.get("restrictions"), str):
        prof["restrictions"] = str(profile_patch.get("restrictions")).strip()
    if isinstance(profile_patch.get("favorite_products"), str):
        prof["favorite_products"] = str(profile_patch.get("favorite_products")).strip()
    if isinstance(profile_patch.get("disliked_products"), str):
        prof["disliked_products"] = str(profile_patch.get("disliked_products")).strip()

    tempo_key = profile_patch.get("tempo_key")
    if tempo_key in GOAL_TEMPO:
        prof["tempo_key"] = str(tempo_key)
        prof["deficit_pct"] = float(GOAL_TEMPO[str(tempo_key)][1])

    # preferences
    if (mpd := _num(prefs_patch.get("meals_per_day"))) is not None and 1 <= mpd <= 8:
        pref_local["meals_per_day"] = int(round(mpd))
    if isinstance(prefs_patch.get("meal_times"), list):
        times: list[str] = []
        for t in prefs_patch.get("meal_times")[:8]:
            if isinstance(t, str) and re.fullmatch(r"\d{2}:\d{2}", t.strip()):
                times.append(t.strip())
        if times:
            pref_local["meal_times"] = times
    if isinstance(prefs_patch.get("snacks"), bool):
        pref_local["snacks"] = bool(prefs_patch.get("snacks"))
    if isinstance(prefs_patch.get("wake_time"), str) and re.fullmatch(r"\d{2}:\d{2}", prefs_patch["wake_time"].strip()):
        pref_local["wake_time"] = prefs_patch["wake_time"].strip()
    if isinstance(prefs_patch.get("sleep_time"), str) and re.fullmatch(r"\d{2}:\d{2}", prefs_patch["sleep_time"].strip()):
        pref_local["sleep_time"] = prefs_patch["sleep_time"].strip()
    if isinstance(prefs_patch.get("notes"), str) and prefs_patch.get("notes"):
        pref_local["notes"] = str(prefs_patch.get("notes")).strip()

    await user_repo.set_dialog(user, state="coach_onboarding", step=1, data={"profile": prof, "prefs": pref_local})

    required = {"age", "sex", "height_cm", "weight_kg", "activity_level", "goal", "tempo_key", "deficit_pct"}
    if not required.issubset(set(prof.keys())):
        if not qs:
            qs = ["Что не хватает из: возраст, пол, рост, вес, активность, цель и темп (мягко/стандарт/жёстко)?"]
        await message.answer("\n".join([f"- {q}" for q in qs[:3]]))
        return True

    # finalize: persist to user + preferences
    user.age = int(prof["age"])
    user.sex = str(prof["sex"])
    user.height_cm = float(prof["height_cm"])
    user.weight_kg = float(prof["weight_kg"])
    user.activity_level = str(prof["activity_level"])
    user.goal = str(prof["goal"])
    user.allergies = str(prof.get("allergies") or "")
    user.restrictions = str(prof.get("restrictions") or "")
    user.favorite_products = str(prof.get("favorite_products") or "")
    user.disliked_products = str(prof.get("disliked_products") or "")

    if not user.country:
        user.country = settings.default_country
    if not user.stores_csv:
        user.stores_csv = settings.default_stores

    t, meta = compute_targets_with_meta(
        sex=user.sex,  # type: ignore[arg-type]
        age=user.age,
        height_cm=user.height_cm,
        weight_kg=user.weight_kg,
        activity=user.activity_level,  # type: ignore[arg-type]
        goal=user.goal,  # type: ignore[arg-type]
        deficit_pct=float(prof["deficit_pct"]),
    )
    user.calories_target = t.calories
    user.protein_g_target = t.protein_g
    user.fat_g_target = t.fat_g
    user.carbs_g_target = t.carbs_g
    user.profile_complete = True

    await pref_repo.merge(
        user.id,
        {
            **pref_local,
            "tempo_key": prof.get("tempo_key"),
            "deficit_pct": meta.deficit_pct,
            "bmr_kcal": meta.bmr_kcal,
            "tdee_kcal": meta.tdee_kcal,
            # default targets from coach calculation (can be overridden by custom targets later)
            "targets": {"calories": t.calories, "protein_g": t.protein_g, "fat_g": t.fat_g, "carbs_g": t.carbs_g},
            "targets_source": "coach",
        },
    )
    # durable coach memory
    try:
        note_repo = CoachNoteRepo(user_repo.db)
        await note_repo.add_note(
            user_id=user.id,
            kind="profile_set",
            title="Профиль создан",
            note_json={
                "goal": user.goal,
                "tempo_key": prof.get("tempo_key"),
                "tdee_kcal": meta.tdee_kcal,
                "calories_target": t.calories,
                "macros": {"p": t.protein_g, "f": t.fat_g, "c": t.carbs_g},
                "prefs_patch": pref_local,
            },
        )
    except Exception:
        pass

    # Next: choose targets mode (coach calculation vs custom calories)
    await user_repo.set_dialog(user, state="targets_mode", step=0, data={"coach_targets": {"calories": t.calories, "p": t.protein_g, "f": t.fat_g, "c": t.carbs_g}})
    await message.answer(
        "Профиль готов. Дальше выбираем, как задаём калории:\n\n"
        f"Расчёт тренера: <b>{t.calories} ккал</b>, БЖУ <b>{t.protein_g}/{t.fat_g}/{t.carbs_g}</b>\n\n"
        "1) Оставляем расчёт (по цели/темпу)\n"
        "2) Ты задаёшь калораж/КБЖУ сам (например: 2800 будни / 2700 выходные)\n",
        reply_markup=targets_mode_kb(),
    )
    return True


def _profile_context(user: Any) -> str:
    return (
        "Профиль пользователя:\n"
        f"- возраст: {user.age}\n"
        f"- пол: {user.sex}\n"
        f"- рост см: {user.height_cm}\n"
        f"- вес кг: {user.weight_kg}\n"
        f"- активность: {user.activity_level}\n"
        f"- цель: {user.goal}\n"
        f"- аллергии: {user.allergies}\n"
        f"- ограничения: {user.restrictions}\n"
        f"- любимые продукты: {user.favorite_products}\n"
        f"- нелюбимые продукты: {user.disliked_products}\n"
        f"- страна: {user.country}\n"
        f"- магазины: {user.stores_csv}\n"
        f"- норма ккал: {user.calories_target}\n"
        f"- БЖУ: {user.protein_g_target}/{user.fat_g_target}/{user.carbs_g_target}\n"
    )


async def _start_meal_confirm(
    message: Message,
    user_repo: UserRepo,
    user: Any,
    draft: dict[str, Any],
    source: str,
    photo_file_id: str | None = None,
) -> None:
    # Guard: don't enter confirm state on empty/failed parse
    items0 = draft.get("items") or []
    totals0 = draft.get("totals") or {}
    try:
        tw0 = float(totals0.get("total_weight_g") or 0)
    except Exception:
        tw0 = 0.0
    if not items0 or tw0 <= 0:
        await user_repo.set_dialog(user, state=None, step=None, data=None)
        await message.answer(
            "Не смог распознать еду/рецепт в этом сообщении.\n"
            "Сформулируй иначе (например: «куриные крылья 4 шт (~300г) + масло 10г») или пришли штрихкод/фото.",
            reply_markup=main_menu_kb(),
        )
        return

    await user_repo.set_dialog(
        user,
        state="meal_confirm",
        step=1,
        data={"draft": draft, "source": source, "photo_file_id": photo_file_id},
    )
    items = items0
    totals = totals0
    tbl = recipe_table(items)
    per100 = ""
    try:
        tw = float(totals.get("total_weight_g") or 0)
        if source == "recipe" and tw > 0:
            per100 = (
                f"\nНа 100г: "
                f"{(float(totals.get('calories') or 0) / tw * 100):.0f} ккал, "
                f"Б {(float(totals.get('protein_g') or 0) / tw * 100):.1f} / "
                f"Ж {(float(totals.get('fat_g') or 0) / tw * 100):.1f} / "
                f"У {(float(totals.get('carbs_g') or 0) / tw * 100):.1f}"
            )
    except Exception:
        per100 = ""
    text = (
        ("Я распознал рецепт так (оценка):\n" if source == "recipe" else "Я распознал так (оценка):\n")
        + f"<pre>{tbl}</pre>\n"
        + f"Итого: {totals.get('total_weight_g')} г, {totals.get('calories')} ккал, "
        + f"Б {totals.get('protein_g')} / Ж {totals.get('fat_g')} / У {totals.get('carbs_g')}"
        + f"{per100}\n\n"
        + "Подтвердить и внести в дневник? (да/нет)"
    )
    await message.answer(text)


def _maybe_barcode(s: str) -> str | None:
    t = _norm_text(s).replace(" ", "")
    m = re.search(r"\b(\d{8,14})\b", t)
    return m.group(1) if m else None


def _format_food_pick_question(ctx: dict[str, Any], idx: int) -> str:
    unresolved: list[dict[str, Any]] = ctx.get("unresolved") or []
    if idx >= len(unresolved):
        return "Ок."
    it = unresolved[idx]
    q = it.get("query")
    grams = it.get("grams")
    cands: list[dict[str, Any]] = it.get("candidates") or []
    if not cands:
        return (
            f"Не нашел точный продукт для: <b>{q}</b> ({grams} г).\n"
            "Пришли штрихкод (8-14 цифр) или уточни бренд/название."
        )

    lines = [f"Выбери продукт для: <b>{q}</b> ({grams} г)\n"]
    for i, c in enumerate(cands, start=1):
        lines.append(
            f"{i}) {c.get('name')} — {c.get('brand') or '—'} "
            f"({c.get('kcal_100g')} ккал/100г) [barcode: {c.get('barcode')}]"
        )
    lines.append("\nОтветь цифрой (1-5) или пришли штрихкод.")
    lines.append("Чтобы отменить — напиши: ❌ Отмена")
    return "\n".join(lines)


async def _build_meal_from_items(
    *,
    items: list[dict[str, Any]],
    food_service: FoodService,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    for it in items:
        query = str(it.get("query") or "").strip()
        grams = float(it.get("grams") or 0)
        barcode = it.get("barcode") or None
        barcode = str(barcode).strip() if barcode else None
        if not query or grams <= 0:
            continue

        cand = None
        if barcode:
            cand = await food_service.resolve_by_barcode(barcode)
        if not cand:
            cands = await food_service.search(query)
            usable = [
                c
                for c in cands
                if c.kcal_100g is not None
                and c.protein_100g is not None
                and c.fat_100g is not None
                and c.carbs_100g is not None
            ]
            if len(usable) == 1:
                cand = usable[0]
            else:
                unresolved.append(
                    {
                        "query": query,
                        "grams": grams,
                        "candidates": [
                            {
                                "barcode": c.barcode,
                                "name": c.name,
                                "brand": c.brand,
                                "kcal_100g": c.kcal_100g,
                                "protein_100g": c.protein_100g,
                                "fat_100g": c.fat_100g,
                                "carbs_100g": c.carbs_100g,
                            }
                            for c in usable[:5]
                        ],
                    }
                )
                continue

        if not cand:
            unresolved.append({"query": query, "grams": grams, "candidates": []})
            continue

        macros = compute_item_macros(grams=grams, cand=cand)
        if not macros:
            unresolved.append({"query": query, "grams": grams, "candidates": []})
            continue
        resolved.append(macros)

    if unresolved:
        return None, {"unresolved": unresolved, "resolved": resolved}

    totals = {
        "total_weight_g": int(round(sum(float(r["grams"]) for r in resolved))),
        "calories": int(round(sum(float(r["calories"]) for r in resolved))),
        "protein_g": int(round(sum(float(r["protein_g"]) for r in resolved))),
        "fat_g": int(round(sum(float(r["fat_g"]) for r in resolved))),
        "carbs_g": int(round(sum(float(r["carbs_g"]) for r in resolved))),
    }
    draft = {
        "items": [
            {
                "name": r["name"],
                "grams": r["grams"],
                "calories": int(round(float(r["calories"]))),
                "protein_g": float(r["protein_g"]),
                "fat_g": float(r["fat_g"]),
                "carbs_g": float(r["carbs_g"]),
                "barcode": r.get("barcode"),
                "brand": r.get("brand"),
                "per_100g": r.get("per_100g"),
            }
            for r in resolved
        ],
        "totals": totals,
        "data_source": "openfoodfacts",
    }
    return draft, None


async def _handle_food_pick(message: Message, user_repo: UserRepo, food_service: FoodService, user: Any) -> dict[str, Any] | None:
    if user.dialog_state != "food_pick":
        return None

    # allow cancel / menu escape to prevent loops
    t = (message.text or "").strip()
    if t in {
        "❌ Отмена",
        BTN_MENU,
        BTN_HELP,
        BTN_PROFILE,
        BTN_WEIGHT,
        BTN_LOG_MEAL,
        BTN_PHOTO_HELP,
        BTN_PLAN,
        BTN_WEEK,
        BTN_REMINDERS,
        BTN_PROGRESS,
    }:
        await user_repo.set_dialog(user, state=None, step=None, data=None)
        await message.answer("Ок, отменил выбор продукта.", reply_markup=main_menu_kb())
        return {"handled": True}

    data = loads(user.dialog_data_json) or {}
    ctx = data.get("ctx") or {}
    source = data.get("source") or "text"
    photo_file_id = data.get("photo_file_id")
    unresolved: list[dict[str, Any]] = ctx.get("unresolved") or []
    resolved: list[dict[str, Any]] = ctx.get("resolved") or []
    idx = int(user.dialog_step or 0)

    if idx >= len(unresolved):
        await user_repo.set_dialog(user, state=None, step=None, data=None)
        return None

    reply = (message.text or "").strip()
    bc = _maybe_barcode(reply)
    chosen = None

    if bc:
        cand = await food_service.resolve_by_barcode(bc)
        if not cand:
            await message.answer("Не нашел продукт по этому штрихкоду. Проверь цифры и пришли еще раз.")
            return {"handled": True}
        chosen = {"barcode": cand.barcode, "name": cand.name, "brand": cand.brand, "kcal_100g": cand.kcal_100g, "protein_100g": cand.protein_100g, "fat_100g": cand.fat_100g, "carbs_100g": cand.carbs_100g}
    else:
        if reply.isdigit():
            n = int(reply)
            cands: list[dict[str, Any]] = unresolved[idx].get("candidates") or []
            if 1 <= n <= len(cands):
                chosen = cands[n - 1]

    if not chosen:
        await message.answer("Ответь цифрой из списка или пришли штрихкод (8-14 цифр).")
        return {"handled": True}

    grams = float(unresolved[idx].get("grams") or 0)
    if chosen.get("barcode"):
        cand = await food_service.resolve_by_barcode(str(chosen["barcode"]))
    else:
        cand = None
    if not cand:
        await message.answer("Не смог зафиксировать выбранный продукт. Пришли штрихкод.")
        return {"handled": True}

    macros = compute_item_macros(grams=grams, cand=cand)
    if not macros:
        await message.answer("У выбранного продукта нет полных нутриентов (БЖУ/ккал). Выбери другой или пришли штрихкод.")
        return {"handled": True}

    resolved.append(macros)
    idx += 1

    if idx < len(unresolved):
        await user_repo.set_dialog(
            user,
            state="food_pick",
            step=idx,
            data={"ctx": {"unresolved": unresolved, "resolved": resolved}, "source": source, "photo_file_id": photo_file_id},
        )
        await message.answer(_format_food_pick_question({"unresolved": unresolved, "resolved": resolved}, idx))
        return {"handled": True}

    # All resolved: build draft
    totals = {
        "total_weight_g": int(round(sum(float(r["grams"]) for r in resolved))),
        "calories": int(round(sum(float(r["calories"]) for r in resolved))),
        "protein_g": int(round(sum(float(r["protein_g"]) for r in resolved))),
        "fat_g": int(round(sum(float(r["fat_g"]) for r in resolved))),
        "carbs_g": int(round(sum(float(r["carbs_g"]) for r in resolved))),
    }
    draft = {
        "items": [
            {
                "name": r["name"],
                "grams": r["grams"],
                "calories": int(round(float(r["calories"]))),
                "protein_g": float(r["protein_g"]),
                "fat_g": float(r["fat_g"]),
                "carbs_g": float(r["carbs_g"]),
                "barcode": r.get("barcode"),
                "brand": r.get("brand"),
                "per_100g": r.get("per_100g"),
            }
            for r in resolved
        ],
        "totals": totals,
        "data_source": "openfoodfacts",
    }
    await user_repo.set_dialog(user, state=None, step=None, data=None)
    return {"handled": True, "draft": draft, "source": source, "photo_file_id": photo_file_id}


@router.message(F.photo)
async def photo_message(message: Message, bot: Bot) -> None:
    if not message.from_user or not message.photo:
        return

    async with SessionLocal() as db:
        user_repo = UserRepo(db)
        note_repo = CoachNoteRepo(db)
        user = await user_repo.get_or_create(message.from_user.id, message.from_user.username)
        if not user.profile_complete:
            await message.answer("Сначала заполним профиль: /start")
            return

        photo = message.photo[-1]
        caption = (message.caption or "").strip()
        if user.dialog_state == "progress_mode" or "прогресс" in _norm_text(caption):
            # progress photo (not food)
            try:
                image_bytes = await download_telegram_file(bot, photo.file_id)
                analysis = await vision_json(
                    system=f"{SYSTEM_COACH}\n\n{PROGRESS_PHOTO_JSON}",
                    user_text="Это фото прогресса тела. Дай краткий разбор для сравнения.",
                    image_bytes=image_bytes,
                    image_mime="image/jpeg",
                    max_output_tokens=700,
                )
            except Exception:
                analysis = {"summary": "Сохранил фото прогресса (без анализа).", "visible_changes": [], "next_actions": [], "confidence": "low"}

            try:
                await note_repo.add_note(
                    user_id=user.id,
                    kind="progress_photo",
                    title="Фото прогресса",
                    note_json={"photo_file_id": photo.file_id, "analysis": analysis, "caption": caption},
                )
                await db.commit()
            except Exception:
                pass

            msg = str((analysis or {}).get("summary") or "Сохранил фото прогресса.")
            await message.answer(msg + "\n\nНапиши «сравни», чтобы я сопоставил последние фото/замеры.", reply_markup=main_menu_kb())
            return

        try:
            image_bytes = await download_telegram_file(bot, photo.file_id)
            analysis = await vision_json(
                system=f"{SYSTEM_NUTRITIONIST}\n\n{PHOTO_ANALYSIS_JSON}",
                user_text=_profile_context(user) + "\nПроанализируй фото еды.",
                image_bytes=image_bytes,
                image_mime="image/jpeg",
            )
        except Exception as e:
            await message.answer(f"Не смог проанализировать фото (ошибка): {e}")
            return

        questions = analysis.get("clarifying_questions") or []
        await user_repo.set_dialog(
            user,
            state="photo_clarify",
            step=0,
            data={
                "photo_file_id": photo.file_id,
                "analysis": analysis,
                "questions": questions,
                "answers": [],
            },
        )
        await db.commit()

        dish = analysis.get("dish_type")
        w = analysis.get("estimated_weight_g")
        method = analysis.get("cooking_method")
        hidden = analysis.get("hidden_calories") or []
        intro = (
            f"По фото похоже на: <b>{dish}</b>\n"
            f"Оценка веса: <b>{w} г</b>\n"
            f"Способ: {method}\n"
            f"Скрытые калории: {', '.join(hidden) if hidden else '—'}\n\n"
        )
        if questions:
            await message.answer(intro + "Уточню пару деталей.\n\n" + questions[0], reply_markup=main_menu_kb())
        else:
            await message.answer(intro + "Не вижу что уточнять. Напиши примерно масло/соус/порцию — и посчитаю КБЖУ.", reply_markup=main_menu_kb())


@router.message(F.voice)
async def voice_message(message: Message, bot: Bot) -> None:
    if not message.from_user or not message.voice:
        return

    async with SessionLocal() as db:
        user_repo = UserRepo(db)
        meal_repo = MealRepo(db)
        stat_repo = StatRepo(db)
        food_repo = FoodRepo(db)
        food_service = FoodService(food_repo)
        user = await user_repo.get_or_create(message.from_user.id, message.from_user.username)
        if not user.profile_complete:
            await message.answer("Сначала заполним профиль: /start")
            return

        try:
            ogg = await download_telegram_file(bot, message.voice.file_id)
        except Exception as e:
            await message.answer(f"Не смог скачать голосовое: {e}")
            return

        wav = ogg_opus_to_wav_bytes(ogg)
        if wav is None:
            await message.answer("Голосовые пока не могу распознавать без ffmpeg. Установи ffmpeg или напиши текстом.")
            return

        try:
            text = (await transcribe_audio(audio_bytes=wav, filename="audio.wav")).strip()
        except Exception as e:
            await message.answer(f"Не смог распознать речь (ошибка): {e}\nНапиши текстом.")
            return

        if not text:
            await message.answer("Не смог понять речь. Попробуй еще раз или напиши текстом.")
            return

        await message.answer(f"Распознал так:\n<pre>{text}</pre>")

        try:
            parsed = await text_json(
                system=f"{SYSTEM_NUTRITIONIST}\n\n{MEAL_ITEMS_JSON}",
                user=_profile_context(user) + "\nВыдели продукты и граммовки:\n" + text,
                max_output_tokens=650,
            )
        except Exception as e:
            await message.answer(f"Не смог разобрать распознанный текст (ошибка): {e}")
            return

        if parsed.get("needs_clarification"):
            qs = parsed.get("clarifying_questions") or []
            if qs:
                await user_repo.set_dialog(
                    user,
                    state="meal_clarify",
                    step=0,
                    data={"draft": parsed, "questions": qs, "answers": [], "source": "voice"},
                )
                await db.commit()
                await message.answer(qs[0])
                return

        draft, unresolved_ctx = await _build_meal_from_items(items=parsed.get("items") or [], food_service=food_service)
        if unresolved_ctx:
            await user_repo.set_dialog(user, state="food_pick", step=0, data={"ctx": unresolved_ctx, "source": "voice"})
            await db.commit()
            await message.answer(_format_food_pick_question(unresolved_ctx, 0))
            return

        await _start_meal_confirm(message, user_repo, user, draft or {}, source="voice")
        await db.commit()


async def _handle_photo_clarify(
    message: Message,
    bot: Bot,
    user_repo: UserRepo,
    meal_repo: MealRepo,
    food_service: FoodService,
    user: Any,
) -> bool:
    if user.dialog_state != "photo_clarify":
        return False

    t0 = (message.text or "").strip()
    if t0 in {
        "❌ Отмена",
        BTN_MENU,
        BTN_HELP,
        BTN_PROFILE,
        BTN_WEIGHT,
        BTN_LOG_MEAL,
        BTN_PHOTO_HELP,
        BTN_PLAN,
        BTN_WEEK,
        BTN_REMINDERS,
        BTN_PROGRESS,
    }:
        await user_repo.set_dialog(user, state=None, step=None, data=None)
        await message.answer("Ок, отменил разбор фото.", reply_markup=main_menu_kb())
        return True

    data = loads(user.dialog_data_json) or {}
    questions: list[str] = data.get("questions") or []
    answers: list[str] = data.get("answers") or []
    idx = int(user.dialog_step or 0)
    text = t0
    answers.append(text)
    idx += 1

    # still questions left
    if idx < len(questions):
        await user_repo.set_dialog(user, state="photo_clarify", step=idx, data={**data, "answers": answers})
        await message.answer(questions[idx])
        return True

    # finalize: photo -> items (GPT) -> macros (OpenFoodFacts)
    try:
        image_bytes = await download_telegram_file(bot, data["photo_file_id"])
        payload = {
            "photo_analysis": data.get("analysis"),
            "qa": [{"q": q, "a": a} for q, a in zip(questions, answers)],
        }
        parsed = await vision_json(
            system=f"{SYSTEM_NUTRITIONIST}\n\n{PHOTO_TO_ITEMS_JSON}",
            user_text=_profile_context(user) + "\nДанные:\n" + dumps(payload),
            image_bytes=image_bytes,
            image_mime="image/jpeg",
        )
    except Exception as e:
        await message.answer(f"Не смог посчитать КБЖУ по фото (ошибка): {e}")
        await user_repo.set_dialog(user, state=None, step=None, data=None)
        return True

    draft, unresolved_ctx = await _build_meal_from_items(items=parsed.get("items") or [], food_service=food_service)
    if unresolved_ctx:
        await user_repo.set_dialog(
            user,
            state="food_pick",
            step=0,
            data={"ctx": unresolved_ctx, "source": "photo", "photo_file_id": data.get("photo_file_id")},
        )
        await message.answer(_format_food_pick_question(unresolved_ctx, 0))
        return True

    await _start_meal_confirm(
        message,
        user_repo,
        user,
        draft or {},
        source="photo",
        photo_file_id=data.get("photo_file_id"),
    )
    return True


async def _handle_meal_confirm(message: Message, user_repo: UserRepo, meal_repo: MealRepo, user: Any) -> bool:
    if user.dialog_state != "meal_confirm":
        return False
    data = loads(user.dialog_data_json) or {}
    draft = data.get("draft") or {}
    source = data.get("source") or "text"
    photo_file_id = data.get("photo_file_id")

    raw = (message.text or "").strip()
    if raw in {
        "❌ Отмена",
        BTN_MENU,
        BTN_HELP,
        BTN_PROFILE,
        BTN_WEIGHT,
        BTN_LOG_MEAL,
        BTN_PHOTO_HELP,
        BTN_PLAN,
        BTN_WEEK,
        BTN_REMINDERS,
        BTN_PROGRESS,
    }:
        await user_repo.set_dialog(user, state=None, step=None, data=None)
        await message.answer("Ок, отменил подтверждение.", reply_markup=main_menu_kb())
        return True

    text = _norm_text(raw)
    if text in {"да", "yes", "y", "ок", "ага"}:
        totals = draft.get("totals") or {}
        await meal_repo.add_meal(
            user_id=user.id,
            source=source,
            description_raw=None,
            meal_json=draft,
            totals=totals,
            photo_file_id=photo_file_id if isinstance(photo_file_id, str) else None,
        )
        await user_repo.set_dialog(user, state=None, step=None, data=None)
        await message.answer("Готово — внес в дневник.")
        return True
    if text in {"нет", "no", "n"}:
        await user_repo.set_dialog(user, state=None, step=None, data=None)
        await message.answer("Ок, не вношу. Можешь прислать уточнение или заново описать прием пищи.")
        return True

    # Any other message: exit confirm state and let the bot continue normally (coach_chat/router/etc.)
    await user_repo.set_dialog(user, state=None, step=None, data=None)
    return False


async def _handle_meal_clarify(message: Message, user_repo: UserRepo, food_service: FoodService, user: Any) -> bool:
    if user.dialog_state != "meal_clarify":
        return False

    t0 = (message.text or "").strip()
    if t0 in {
        "❌ Отмена",
        BTN_MENU,
        BTN_HELP,
        BTN_PROFILE,
        BTN_WEIGHT,
        BTN_LOG_MEAL,
        BTN_PHOTO_HELP,
        BTN_PLAN,
        BTN_WEEK,
        BTN_REMINDERS,
        BTN_PROGRESS,
    }:
        await user_repo.set_dialog(user, state=None, step=None, data=None)
        await message.answer("Ок, отменил уточнения по приёму пищи.", reply_markup=main_menu_kb())
        return True

    data = loads(user.dialog_data_json) or {}
    source = data.get("source") or "text"
    qs: list[str] = data.get("questions") or []
    answers: list[str] = data.get("answers") or []
    idx = int(user.dialog_step or 0)
    answers.append(t0)
    idx += 1

    if idx < len(qs):
        await user_repo.set_dialog(user, state="meal_clarify", step=idx, data={**data, "answers": answers})
        await message.answer(qs[idx])
        return True

    # Finalize with clarifications
    draft = data.get("draft") or {}
    payload = {"initial_draft": draft, "qa": [{"q": q, "a": a} for q, a in zip(qs, answers)]}
    try:
        parsed = await text_json(
            system=f"{SYSTEM_NUTRITIONIST}\n\n{MEAL_ITEMS_JSON}",
            user=_profile_context(user)
            + "\nУточнения по приему пищи:\n"
            + dumps(payload)
            + "\nВерни финальные items без дополнительных вопросов.",
            max_output_tokens=650,
        )
    except Exception as e:
        await message.answer(f"Не смог собрать финальный расчет (ошибка): {e}")
        await user_repo.set_dialog(user, state=None, step=None, data=None)
        return True

    draft2, unresolved_ctx = await _build_meal_from_items(items=parsed.get("items") or [], food_service=food_service)
    if unresolved_ctx:
        await user_repo.set_dialog(user, state="food_pick", step=0, data={"ctx": unresolved_ctx, "source": source})
        await message.answer(_format_food_pick_question(unresolved_ctx, 0))
        return True

    await _start_meal_confirm(message, user_repo, user, draft2 or {}, source=source)
    return True


async def _handle_apply_calories(message: Message, user_repo: UserRepo, user: Any) -> bool:
    if user.dialog_state != "apply_calories":
        return False
    data = loads(user.dialog_data_json) or {}
    new_cal = data.get("new_calories")
    raw = (message.text or "").strip()
    if raw in {
        "❌ Отмена",
        BTN_MENU,
        BTN_HELP,
        BTN_PROFILE,
        BTN_WEIGHT,
        BTN_LOG_MEAL,
        BTN_PHOTO_HELP,
        BTN_PLAN,
        BTN_WEEK,
        BTN_REMINDERS,
        BTN_PROGRESS,
    }:
        await user_repo.set_dialog(user, state=None, step=None, data=None)
        await message.answer("Ок, не применяю изменения.", reply_markup=main_menu_kb())
        return True

    text = _norm_text(raw)
    if text in {"да", "yes", "y", "ок", "ага"} and isinstance(new_cal, (int, float)):
        user.calories_target = int(new_cal)
        # пересчитаем макросы от новой калорийности с тем же весом/целью (приближение)
        t = compute_targets(
            sex=user.sex,  # type: ignore[arg-type]
            age=user.age,
            height_cm=user.height_cm,
            weight_kg=user.weight_kg,
            activity=user.activity_level,  # type: ignore[arg-type]
            goal=user.goal,  # type: ignore[arg-type]
        )
        # подменяем только калории, макросы пересчитаем пропорционально по углеводам (быстро и безопасно)
        # (детальнее будет делать в weekly логике позже)
        user.calories_target = int(new_cal)
        user.protein_g_target = t.protein_g
        user.fat_g_target = t.fat_g
        # пересчёт углеводов под новую калорийность
        kcal_pf = user.protein_g_target * 4 + user.fat_g_target * 9
        user.carbs_g_target = max(int(round((user.calories_target - kcal_pf) / 4)), 0)

        await user_repo.set_dialog(user, state=None, step=None, data=None)
        await message.answer(
            f"Применил. Новая норма: <b>{user.calories_target} ккал</b>, БЖУ: "
            f"<b>{user.protein_g_target}/{user.fat_g_target}/{user.carbs_g_target} г</b>"
        )
        return True

    if text in {"нет", "no", "n"}:
        await user_repo.set_dialog(user, state=None, step=None, data=None)
        await message.answer("Ок, не меняю норму.")
        return True

    await message.answer("Ответь «да» чтобы применить новую норму или «нет» чтобы оставить как есть.")
    return True


async def _agent_route(text: str, user: Any) -> dict[str, Any] | None:
    try:
        return await text_json(
            system=f"{SYSTEM_COACH}\n\n{ROUTER_JSON}",
            user=_profile_context(user) + "\nСообщение пользователя:\n" + text,
            max_output_tokens=300,
        )
    except Exception:
        return None


def _looks_like_meal(text: str) -> bool:
    t = _norm_text(text)
    if not t:
        return False
    # grams / quantities / typical food markers
    if re.search(r"\b\d+\s?(г|гр|kg|кг|ml|мл|шт)\b", t):
        return True
    if any(k in t for k in ["съел", "поел", "ел ", "завтрак", "обед", "ужин", "перекус", "греч", "куриц", "рис", "паста", "йогур", "творог", "омлет"]):
        return True
    # list-like: commas with numbers
    if "," in t and re.search(r"\d", t):
        return True
    return False


def _needs_hidden_calorie_clarification(user_text: str) -> list[str]:
    t = _norm_text(user_text)
    if not t:
        return []
    risky = any(k in t for k in ["жар", "гриль", "салат", "соус", "сыр", "орех", "майон", "шаур", "бургер", "пицц", "паста"])
    if not risky:
        return []
    # if user already mentioned oil/sauce amounts, skip
    if any(k in t for k in ["масло", "олив", "соус", "майон", "кетч", "алког", "пиво", "вино", "сыр "]):
        return []
    return [
        "Сколько масла/соуса было в приготовлении? (пример: масло 10г / 1 ст.л.)",
        "Был ли сыр/орехи/алкоголь вместе с этим? Если да — сколько примерно?",
    ]


def _parse_dt(s: str | None) -> dt.datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _tz_from_prefs(prefs: dict[str, Any]) -> ZoneInfo:
    tz_name = prefs.get("timezone") if isinstance(prefs.get("timezone"), str) else "Europe/Prague"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Europe/Prague")


def _mean(xs: list[float]) -> float | None:
    if not xs:
        return None
    return sum(xs) / len(xs)


def compute_calibration_from_weights(
    *,
    weights: list[dict[str, Any]],
    current_target_kcal: int,
) -> dict[str, Any] | None:
    """
    weights: list of {date: 'YYYY-MM-DD', weight_kg: float} ascending.
    Uses 7-day vs previous 7-day averages when possible.
    """
    ws = []
    for w in weights:
        try:
            ws.append((dt.date.fromisoformat(w["date"]), float(w["weight_kg"])))
        except Exception:
            continue
    if len(ws) < 10:
        return None

    # take last 14 days window
    last_date = ws[-1][0]
    start = last_date - dt.timedelta(days=13)
    window = [(d, v) for d, v in ws if d >= start]
    if len(window) < 10:
        return None

    # split by date into first 7 and last 7 (by actual dates)
    first = [v for d, v in window if d <= start + dt.timedelta(days=6)]
    second = [v for d, v in window if d > start + dt.timedelta(days=6)]
    a1 = _mean(first)
    a2 = _mean(second)
    if a1 is None or a2 is None:
        return None

    actual_loss_kg = a1 - a2  # positive means losing
    days = 7
    # implied deficit (kcal/day) from weight change
    implied_def = (actual_loss_kg * 7700.0) / float(days)
    # calibrated tdee approx = intake + deficit; we only know intake target
    calibrated_tdee = int(round(float(current_target_kcal) + implied_def))

    return {
        "window_start": start.isoformat(),
        "window_end": last_date.isoformat(),
        "avg_weight_prev7": round(a1, 2),
        "avg_weight_last7": round(a2, 2),
        "actual_loss_kg_per_week": round(actual_loss_kg, 2),
        "implied_deficit_kcal_per_day": int(round(implied_def)),
        "calibrated_tdee_kcal": calibrated_tdee,
    }


def _detect_weight_stall(weights: list[dict[str, Any]], days: int = 14) -> bool:
    ws = []
    for w in weights:
        try:
            ws.append((dt.date.fromisoformat(w["date"]), float(w["weight_kg"])))
        except Exception:
            continue
    if len(ws) < 10:
        return False
    last_date = ws[-1][0]
    start = last_date - dt.timedelta(days=days - 1)
    window = [v for d, v in ws if d >= start]
    if len(window) < 10:
        return False
    # stall if change < 0.2kg over window
    return abs(window[-1] - window[0]) < 0.2


async def _apply_coach_memory_if_needed(message: Message, *, pref_repo: PreferenceRepo, user: Any) -> bool:
    """
    Parse free-form "remember this" / routines / supplements and persist to preferences.
    Returns True if handled (i.e., saved and user was replied to).
    """
    txt = (message.text or "").strip()
    if not txt:
        return False

    prefs = await pref_repo.get_json(user.id)
    extracted = await text_json(
        system=f"{SYSTEM_COACH}\n\n{COACH_MEMORY_JSON}",
        user="Текущие предпочтения из БД:\n" + dumps(prefs) + "\nСообщение пользователя:\n" + txt,
        max_output_tokens=450,
    )
    if not isinstance(extracted, dict):
        return False
    if not extracted.get("should_apply"):
        return False

    patch = extracted.get("preferences_patch") or {}
    if not isinstance(patch, dict) or not patch:
        return False

    # merge snack_rules/supplements carefully
    merged_patch: dict[str, Any] = {}

    if isinstance(patch.get("snack_rules"), list):
        merged_patch["snack_rules"] = patch["snack_rules"]
    if isinstance(patch.get("supplements"), list):
        merged_patch["supplements"] = patch["supplements"]
    if isinstance(patch.get("checkin_every_days"), (int, float)):
        merged_patch["checkin_every_days"] = int(patch["checkin_every_days"])
    if isinstance(patch.get("checkin_ask"), dict):
        merged_patch["checkin_ask"] = patch["checkin_ask"]
    if isinstance(patch.get("weight_prompt_enabled"), bool):
        merged_patch["weight_prompt_enabled"] = bool(patch["weight_prompt_enabled"])
    if isinstance(patch.get("weight_prompt_time"), str) and re.fullmatch(r"\d{2}:\d{2}", patch["weight_prompt_time"].strip()):
        merged_patch["weight_prompt_time"] = patch["weight_prompt_time"].strip()
    if patch.get("weight_prompt_days") in {"weekdays", "weekends", "all"}:
        merged_patch["weight_prompt_days"] = patch["weight_prompt_days"]
    if isinstance(patch.get("reminders"), list):
        rems: list[dict[str, Any]] = []
        for r in patch.get("reminders")[:20]:
            if not isinstance(r, dict):
                continue
            t = r.get("time")
            d = r.get("days")
            txt = r.get("text")
            if isinstance(t, str) and re.fullmatch(r"\d{2}:\d{2}", t.strip()) and d in {"weekdays", "weekends", "all"} and isinstance(txt, str) and txt.strip():
                rems.append({"time": t.strip(), "days": d, "text": txt.strip()})
        merged_patch["reminders"] = rems
    # targets override (store in prefs + user snapshot)
    if isinstance(patch.get("targets"), dict):
        t = patch.get("targets") or {}
        targ: dict[str, Any] = {}
        for k in ["calories", "calories_weekdays", "calories_weekends", "protein_g", "fat_g", "carbs_g"]:
            v = t.get(k)
            if isinstance(v, (int, float)):
                targ[k] = int(round(float(v)))
        if targ:
            merged_patch["targets"] = targ
            # apply to user snapshot (single-day defaults)
            if "calories" in targ:
                user.calories_target = int(targ["calories"])
            if "protein_g" in targ:
                user.protein_g_target = int(targ["protein_g"])
            if "fat_g" in targ:
                user.fat_g_target = int(targ["fat_g"])
            if "carbs_g" in targ:
                user.carbs_g_target = int(targ["carbs_g"])
    if isinstance(patch.get("notes"), str) and patch.get("notes"):
        merged_patch["notes"] = str(patch["notes"]).strip()

    if not merged_patch:
        return False

    await pref_repo.merge(user.id, merged_patch)
    # store durable memory item
    try:
        note_repo = CoachNoteRepo(pref_repo.db)
        await note_repo.add_note(
            user_id=user.id,
            kind="prefs_update",
            title="Обновление правил/настроек",
            note_json={"patch": merged_patch, "text": (message.text or "").strip()},
        )
    except Exception:
        pass
    ack = extracted.get("ack")
    await message.answer(str(ack or "Ок, сохранил это как правило/настройку."), reply_markup=main_menu_kb())
    return True


def _pick_meal_from_plan(plan: dict[str, Any], slot: str | None) -> dict[str, Any] | None:
    meals = plan.get("meals") or []
    if not isinstance(meals, list) or not meals:
        return None

    slot = (slot or "").lower().strip()
    # try by title keywords
    kw = {
        "breakfast": ["завтрак"],
        "lunch": ["обед"],
        "dinner": ["ужин"],
        "snack": ["перекус"],
    }.get(slot, [])
    if kw:
        for m in meals:
            title = str((m or {}).get("title") or "").lower()
            if any(k in title for k in kw):
                return m

    # try by time ranges (if present)
    def _tval(m: dict[str, Any]) -> str:
        return str(m.get("time") or "").strip()

    def _in_range(t: str, start_h: int, end_h: int) -> bool:
        if not re.fullmatch(r"\d{2}:\d{2}", t):
            return False
        h = int(t[:2])
        return start_h <= h < end_h

    if slot == "breakfast":
        for m in meals:
            if _in_range(_tval(m), 5, 11):
                return m
    if slot == "lunch":
        for m in meals:
            if _in_range(_tval(m), 11, 16):
                return m
    if slot == "dinner":
        for m in meals:
            if _in_range(_tval(m), 16, 22):
                return m

    # fallback: middle meal looks like lunch
    return meals[min(1, len(meals) - 1)]


async def _handle_recall_plan(message: Message, *, plan_repo: PlanRepo, user: Any, slot_hint: str | None) -> bool:
    today = dt.date.today()
    plan = await plan_repo.get_day_plan_json(user.id, today)
    if not plan:
        await message.answer("Плана на сегодня ещё нет. Сначала сделай 🗓️ Рацион на день.", reply_markup=main_menu_kb())
        return True

    m = _pick_meal_from_plan(plan, slot_hint)
    if not m:
        await message.answer("Не смог найти прием пищи в плане. Можешь сгенерировать план заново: 🗓️ Рацион на день.")
        return True

    title = str(m.get("title") or "Прием пищи")
    tm = str(m.get("time") or "").strip()
    products = m.get("products") or []
    recipe = m.get("recipe") or []

    text = (
        f"<b>{'Сегодня' if today else ''} {tm + ' — ' if tm else ''}{title}</b>\n\n"
        + ("<b>Продукты</b>:\n" + "\n".join([f"- {p.get('name')} — {p.get('grams')} г ({p.get('store')})" for p in products]) + "\n\n" if products else "")
        + ("<b>Рецепт</b>:\n" + "\n".join([f"- {s}" for s in recipe]) if recipe else "")
    )
    await message.answer(text[:3900], reply_markup=main_menu_kb())
    return True


async def _handle_coach_chat(
    message: Message,
    *,
    pref_repo: PreferenceRepo,
    meal_repo: MealRepo,
    plan_repo: PlanRepo,
    note_repo: CoachNoteRepo,
    user: Any,
) -> bool:
    q = (message.text or "").strip()
    if not q:
        return False

    prefs = await pref_repo.get_json(user.id)
    today_plan = await plan_repo.get_day_plan_json(user.id, dt.date.today())
    recent_notes = await note_repo.last_notes(user.id, limit=20)
    last_meals = await meal_repo.last_meals(user.id, limit=12)
    meals_json = [
        {
            "created_at": m.created_at.isoformat(),
            "source": m.source,
            "calories": m.calories,
            "protein_g": m.protein_g,
            "fat_g": m.fat_g,
            "carbs_g": m.carbs_g,
            "description_raw": m.description_raw,
        }
        for m in last_meals
    ]

    # add computed targets meta if possible (truth / hard numbers)
    try:
        deficit_pct = prefs.get("deficit_pct")
        _, meta = compute_targets_with_meta(
            sex=user.sex,  # type: ignore[arg-type]
            age=user.age,
            height_cm=user.height_cm,
            weight_kg=user.weight_kg,
            activity=user.activity_level,  # type: ignore[arg-type]
            goal=user.goal,  # type: ignore[arg-type]
            deficit_pct=float(deficit_pct) if deficit_pct is not None else None,
        )
        calc_meta = {"bmr_kcal": meta.bmr_kcal, "tdee_kcal": meta.tdee_kcal, "deficit_pct": meta.deficit_pct, "deficit_kcal": meta.deficit_kcal}
    except Exception:
        calc_meta = None

    ctx = {
        "profile": {
            "age": user.age,
            "sex": user.sex,
            "height_cm": user.height_cm,
            "weight_kg": user.weight_kg,
            "activity_level": user.activity_level,
            "goal": user.goal,
            "calories_target": user.calories_target,
            "macros_target": [user.protein_g_target, user.fat_g_target, user.carbs_g_target],
        },
        "calc_meta": calc_meta,
        "preferences": prefs,
        "today_plan": today_plan,
        "recent_meals": meals_json,
        "coach_notes": recent_notes,
    }

    try:
        ans = await text_output(
            system=f"{SYSTEM_COACH}\n\n{COACH_CHAT_GUIDE}",
            user="Контекст (из БД):\n" + dumps(ctx) + "\n\nВопрос пользователя:\n" + q,
            max_output_tokens=900,
        )
    except Exception as e:
        try:
            print("COACH_CHAT_ERROR:", type(e).__name__, _scrub_secrets(str(e))[:500])
            print(traceback.format_exc())
        except Exception:
            pass
        err_snip = _scrub_secrets(str(e)).strip()
        err_snip = _escape_html(err_snip[:180]) if err_snip else ""
        await message.answer(
            "⚠️ Сейчас не могу ответить как тренер (ошибка AI).\n"
            "Попробуй ещё раз через минуту или проверь настройки OpenAI (ключ/модель/лимиты).\n"
            f"Тех.деталь: <code>{type(e).__name__}</code>" + (f"\n<code>{err_snip}</code>" if err_snip else ""),
            reply_markup=main_menu_kb(),
        )
        return True

    out = _safe_nonempty_text(_sanitize_ai_text(ans), fallback="⚠️ Похоже, ответ получился пустым. Попробуй ещё раз (или нажми 🏠 Меню).")
    await message.answer(out[:3900], reply_markup=main_menu_kb())
    return True


async def _handle_recipe_ai(message: Message, *, user_repo: UserRepo, food_service: FoodService, user: Any, text: str) -> bool:
    """
    Free-form recipe -> items(grams) -> macros via OpenFoodFacts.
    Saved via existing meal_confirm flow, source="recipe".
    """
    try:
        parsed = await text_json(
            system=f"{SYSTEM_COACH}\n\n{MEAL_ITEMS_JSON}",
            user=_profile_context(user) + "\nЭто рецепт. Выдели ингредиенты и граммовки:\n" + text,
            max_output_tokens=750,
        )
    except Exception as e:
        await message.answer(f"Не смог разобрать рецепт (ошибка): {e}")
        return True

    if parsed.get("needs_clarification"):
        qs = parsed.get("clarifying_questions") or []
        if qs:
            await user_repo.set_dialog(
                user,
                state="meal_clarify",
                step=0,
                data={"draft": parsed, "questions": qs, "answers": [], "source": "recipe"},
            )
            await message.answer(qs[0])
            return True

    draft2, unresolved_ctx = await _build_meal_from_items(items=parsed.get("items") or [], food_service=food_service)
    if unresolved_ctx:
        await user_repo.set_dialog(user, state="food_pick", step=0, data={"ctx": unresolved_ctx, "source": "recipe"})
        await message.answer(_format_food_pick_question(unresolved_ctx, 0))
        return True

    await _start_meal_confirm(message, user_repo, user, draft2 or {}, source="recipe")
    return True


async def _handle_daily_checkin(message: Message, *, user_repo: UserRepo, user: Any, db: Any) -> bool:
    if user.dialog_state != "daily_checkin":
        return False
    t0 = (message.text or "").strip()
    if t0 in {"❌ Отмена", BTN_MENU, BTN_HELP}:
        await user_repo.set_dialog(user, state=None, step=None, data=None)
        await message.answer("Ок, отменил чек‑лист.", reply_markup=main_menu_kb())
        return True

    pref_repo = PreferenceRepo(db)
    note_repo = CoachNoteRepo(db)
    repo = DailyCheckinRepo(db)
    prefs = await pref_repo.get_json(user.id)
    tz = _tz_from_prefs(prefs)
    today_local = dt.datetime.now(dt.timezone.utc).astimezone(tz).date()

    try:
        parsed = await text_json(
            system=f"{SYSTEM_COACH}\n\n{DAILY_CHECKIN_JSON}",
            user="Текст отчёта:\n" + t0,
            max_output_tokens=350,
        )
    except Exception:
        parsed = {}

    def _b(x: Any) -> bool | None:
        if isinstance(x, bool):
            return x
        return None

    def _i(x: Any) -> int | None:
        try:
            if x is None:
                return None
            return int(float(x))
        except Exception:
            return None

    def _f(x: Any) -> float | None:
        try:
            if x is None:
                return None
            return float(x)
        except Exception:
            return None

    rec = await repo.upsert(
        user_id=user.id,
        date=today_local,
        calories_ok=_b(parsed.get("calories_ok")),
        protein_ok=_b(parsed.get("protein_ok")),
        steps=_i(parsed.get("steps")),
        sleep_hours=_f(parsed.get("sleep_hours")),
        training_done=_b(parsed.get("training_done")),
        alcohol=_b(parsed.get("alcohol")),
        note_text=str(parsed.get("note") or "").strip() or None,
        raw_json=parsed if isinstance(parsed, dict) else None,
    )
    try:
        await note_repo.add_note(user_id=user.id, kind="daily_checkin", title="Дневной чек‑лист", note_json={"date": today_local.isoformat(), **(parsed if isinstance(parsed, dict) else {})})
    except Exception:
        pass

    await user_repo.set_dialog(user, state=None, step=None, data=None)

    # quick feedback (coach style)
    lines = ["Принял чек‑лист."]
    if rec.calories_ok is False:
        lines.append("Калории не соблюдены — завтра держим структуру и убираем “лишнее” без наказаний.")
    if rec.protein_ok is False:
        lines.append("Белок не добран — завтра добьём (минимум +40–60г белка).")
    if rec.alcohol is True:
        lines.append("Алкоголь был — учти задержку воды/аппетит. Завтра без компенсаций, просто в норму.")
    if rec.sleep_hours is not None and rec.sleep_hours < 7:
        lines.append("Сон ниже 7ч — риск голода/срыва выше. Сегодня приоритет лечь раньше.")

    await message.answer("\n".join(lines), reply_markup=main_menu_kb())

    # if-then rules (simple, high-signal)
    try:
        # protein miss 2 days in a row -> actionable suggestion
        last = await repo.last_days(user.id, days=3)
        if len(last) >= 2 and last[-1].get("protein_ok") is False and last[-2].get("protein_ok") is False:
            await note_repo.add_note(
                user_id=user.id,
                kind="rule_trigger",
                title="Недобор белка 2 дня",
                note_json={"rule": "protein_2_days", "last": last[-2:]},
            )
            await message.answer(
                "Правило сработало: белок 2 дня подряд ниже цели.\n"
                "Завтра сделай минимум одно:\n"
                "- +300–400г skyr/йогурта\n"
                "- или +250г творога\n"
                "- или +200–250г курицы/индейки\n"
                "Это проще, чем резать углеводы.",
                reply_markup=main_menu_kb(),
            )
    except Exception:
        pass
    return True


async def _handle_targets_mode(message: Message, *, user_repo: UserRepo, user: Any, db: Any) -> bool:
    if user.dialog_state not in {"targets_mode", "targets_custom"}:
        return False
    t0 = (message.text or "").strip()

    pref_repo = PreferenceRepo(db)
    prefs = await pref_repo.get_json(user.id)
    tz = _tz_from_prefs(prefs)
    today_local = dt.datetime.now(dt.timezone.utc).astimezone(tz).date()

    if user.dialog_state == "targets_mode":
        if t0 in {"❌ Отмена", BTN_MENU}:
            await user_repo.set_dialog(user, state=None, step=None, data=None)
            await message.answer("Ок.", reply_markup=main_menu_kb())
            return True
        if t0 == BTN_TARGETS_AUTO:
            try:
                # mark as coach-driven targets
                await pref_repo.merge(user.id, {"targets_source": "coach"})
            except Exception:
                pass
            await user_repo.set_dialog(user, state=None, step=None, data=None)
            await message.answer("✅ Ок! Работаем по расчёту тренера 💪📊\n\nЖми 🗓️ Рацион на день 🍽️", reply_markup=main_menu_kb())
            return True
        if t0 == BTN_TARGETS_CUSTOM:
            await user_repo.set_dialog(user, state="targets_custom", step=0, data=None)
            await message.answer(
                "Ок. Напиши целевые калории (и при желании БЖУ).\n"
                "Примеры:\n"
                "- «2800 будни и 2700 выходные»\n"
                "- «2800 ккал, Б 210 Ж 80 У 300»\n"
                "Я зафиксирую и буду строить рацион строго под это.",
                reply_markup=main_menu_kb(),
            )
            return True

        await message.answer("Выбери вариант кнопкой ниже.", reply_markup=targets_mode_kb())
        return True

    # targets_custom: parse via coach memory extractor (targets field)
    # We reuse AI extractor to keep it flexible, then ensure macros exist deterministically.
    handled = await _apply_coach_memory_if_needed(message, pref_repo=pref_repo, user=user)
    if not handled:
        await message.answer("Не понял. Напиши числами (ккал и/или БЖУ), например: «2800 будни 2700 выходные».")
        return True

    # mark as custom targets
    try:
        await pref_repo.merge(user.id, {"targets_source": "custom"})
    except Exception:
        pass

    # Ensure targets exist and compute macros if only calories were provided
    prefs2 = await pref_repo.get_json(user.id)
    targ = prefs2.get("targets") if isinstance(prefs2.get("targets"), dict) else {}
    kcal_today = None
    if isinstance(targ, dict):
        wd = today_local.weekday()
        is_weekday = wd < 5
        if is_weekday and isinstance(targ.get("calories_weekdays"), (int, float)):
            kcal_today = int(targ.get("calories_weekdays"))
        elif (not is_weekday) and isinstance(targ.get("calories_weekends"), (int, float)):
            kcal_today = int(targ.get("calories_weekends"))
        elif isinstance(targ.get("calories"), (int, float)):
            kcal_today = int(targ.get("calories"))
    if kcal_today is None and user.calories_target is not None:
        kcal_today = int(user.calories_target)

    if kcal_today is not None and user.weight_kg and user.goal:
        missing_macros = not (isinstance(targ.get("protein_g"), (int, float)) and isinstance(targ.get("fat_g"), (int, float)) and isinstance(targ.get("carbs_g"), (int, float)))
        if missing_macros:
            mt = macros_for_targets(int(kcal_today), weight_kg=float(user.weight_kg), goal=user.goal)  # type: ignore[arg-type]
            await pref_repo.merge(
                user.id,
                {
                    "targets": {
                        **(targ if isinstance(targ, dict) else {}),
                        "protein_g": mt.protein_g,
                        "fat_g": mt.fat_g,
                        "carbs_g": mt.carbs_g,
                    }
                },
            )
            user.protein_g_target = mt.protein_g
            user.fat_g_target = mt.fat_g
            user.carbs_g_target = mt.carbs_g
            user.calories_target = int(kcal_today)

    await user_repo.set_dialog(user, state=None, step=None, data=None)
    await message.answer("✅ Зафиксировал цели 🔥🎯\n\nТеперь 🗓️ Рацион на день 🍽️ будет <b>строго под них</b> 💪", reply_markup=main_menu_kb())
    return True


async def _checkin_loop(bot: Bot) -> None:
    """
    Background loop that periodically asks users for photo/measurements according to preferences.
    """
    while True:
        try:
            async with SessionLocal() as db:
                pref_repo = PreferenceRepo(db)
                # list users
                res = await db.execute(select(User).where(User.profile_complete == True))  # noqa: E712
                users = list(res.scalars().all())
                now_utc = dt.datetime.now(dt.timezone.utc)

                for u in users:
                    prefs = await pref_repo.get_json(u.id)

                    tz_name = prefs.get("timezone") if isinstance(prefs.get("timezone"), str) else "Europe/Prague"
                    try:
                        tz = ZoneInfo(tz_name)
                    except Exception:
                        tz = ZoneInfo("Europe/Prague")
                    now_local = now_utc.astimezone(tz)

                    every = prefs.get("checkin_every_days")
                    if not isinstance(every, (int, float)) or every <= 0:
                        every = None

                    if every is not None:
                        last = _parse_dt(prefs.get("last_checkin_request_utc"))
                        if last:
                            last_utc = last.replace(tzinfo=dt.timezone.utc)
                        else:
                            last_utc = None
                        if last_utc and (now_utc - last_utc) < dt.timedelta(days=float(every)):
                            pass
                        else:
                            ask = prefs.get("checkin_ask") or {}
                            want_photo = bool(ask.get("photo", True))
                            want_meas = bool(ask.get("measurements", True))
                            parts = ["Проверка прогресса:"]
                            if want_photo:
                                parts.append("- пришли фото (фронт/бок/спина) при одинаковом свете")
                            if want_meas:
                                parts.append("- и замеры: талия/бедра/грудь (см)")
                            parts.append("Если хочешь отключить — напиши: «отмени чек-ин».")
                            text = "\n".join(parts)

                            try:
                                await bot.send_message(u.telegram_id, text, reply_markup=main_menu_kb())
                                await pref_repo.merge(u.id, {"last_checkin_request_utc": now_utc.isoformat()})
                                await db.commit()
                            except Exception:
                                pass

                    # daily weight prompt (time-based)
                    if prefs.get("weight_prompt_enabled") is True:
                        tstr = prefs.get("weight_prompt_time") if isinstance(prefs.get("weight_prompt_time"), str) else "06:00"
                        days = prefs.get("weight_prompt_days") if prefs.get("weight_prompt_days") in {"weekdays", "weekends", "all"} else "all"
                        if re.fullmatch(r"\d{2}:\d{2}", tstr):
                            hh = int(tstr[:2])
                            mm = int(tstr[3:5])
                            wd = now_local.weekday()  # 0=Mon
                            is_weekday = wd < 5
                            if (days == "weekdays" and not is_weekday) or (days == "weekends" and is_weekday):
                                pass
                            else:
                                last_date = prefs.get("last_weight_prompt_date")
                                today_str = now_local.date().isoformat()
                                if now_local.hour == hh and mm <= now_local.minute <= mm + 2 and last_date != today_str:
                                    try:
                                        await bot.send_message(
                                            u.telegram_id,
                                            "Доброе утро. Пришли текущий вес (кг).",
                                            reply_markup=main_menu_kb(),
                                        )
                                        await pref_repo.merge(u.id, {"last_weight_prompt_date": today_str})
                                        await db.commit()
                                    except Exception:
                                        pass

                    # generic reminders (time-based)
                    rems = prefs.get("reminders")
                    if isinstance(rems, list) and rems:
                        last_sent = prefs.get("reminders_last_sent")
                        last_sent = last_sent if isinstance(last_sent, dict) else {}
                        updated_last: dict[str, Any] | None = None

                        for idx, r in enumerate(rems[:20]):
                            if not isinstance(r, dict):
                                continue
                            tstr = r.get("time")
                            days = r.get("days")
                            text = r.get("text")
                            if not (isinstance(tstr, str) and re.fullmatch(r"\d{2}:\d{2}", tstr.strip())):
                                continue
                            if days not in {"weekdays", "weekends", "all"}:
                                continue
                            if not isinstance(text, str) or not text.strip():
                                continue

                            hh = int(tstr[:2])
                            mm = int(tstr[3:5])
                            wd = now_local.weekday()
                            is_weekday = wd < 5
                            if (days == "weekdays" and not is_weekday) or (days == "weekends" and is_weekday):
                                continue

                            rid = f"r{idx}"
                            today_str = now_local.date().isoformat()
                            if now_local.hour == hh and mm <= now_local.minute <= mm + 2 and last_sent.get(rid) != today_str:
                                try:
                                    await bot.send_message(u.telegram_id, str(text).strip(), reply_markup=main_menu_kb())
                                    if updated_last is None:
                                        updated_last = dict(last_sent)
                                    updated_last[rid] = today_str
                                except Exception:
                                    pass

                        if updated_last is not None:
                            try:
                                await pref_repo.merge(u.id, {"reminders_last_sent": updated_last})
                                await db.commit()
                            except Exception:
                                pass

                    # daily discipline check-in (structured)
                    if prefs.get("daily_checkin_enabled") is True:
                        tstr = prefs.get("daily_checkin_time") if isinstance(prefs.get("daily_checkin_time"), str) else "21:30"
                        days = prefs.get("daily_checkin_days") if prefs.get("daily_checkin_days") in {"weekdays", "weekends", "all"} else "all"
                        if re.fullmatch(r"\d{2}:\d{2}", tstr):
                            hh = int(tstr[:2])
                            mm = int(tstr[3:5])
                            wd = now_local.weekday()
                            is_weekday = wd < 5
                            if (days == "weekdays" and not is_weekday) or (days == "weekends" and is_weekday):
                                pass
                            else:
                                last_date = prefs.get("last_daily_checkin_date")
                                today_str = now_local.date().isoformat()
                                if now_local.hour == hh and mm <= now_local.minute <= mm + 2 and last_date != today_str:
                                    try:
                                        # set dialog state for next user reply
                                        u.dialog_state = "daily_checkin"
                                        u.dialog_step = 0
                                        u.dialog_data_json = dumps({"date": today_str})
                                        await bot.send_message(
                                            u.telegram_id,
                                            "Дневной чек‑лист (ответь одним сообщением):\n"
                                            "- калории: да/нет\n"
                                            "- белок: да/нет\n"
                                            "- шаги: число\n"
                                            "- сон: часы\n"
                                            "- тренировка: да/нет\n"
                                            "- алкоголь: да/нет\n"
                                            "Можно коротко: «ккал да, белок нет, шаги 9000, сон 7.5, трен да, алко нет».",
                                            reply_markup=main_menu_kb(),
                                        )
                                        await pref_repo.merge(u.id, {"last_daily_checkin_date": today_str})
                                        await db.commit()
                                    except Exception:
                                        pass
        except Exception:
            pass

        await asyncio.sleep(60)


@router.message(Command("plan"))
async def cmd_plan(message: Message) -> None:
    if not message.from_user:
        return

    async with SessionLocal() as db:
        user_repo = UserRepo(db)
        user = await user_repo.get_or_create(message.from_user.id, message.from_user.username)
        if not user.profile_complete:
            await message.answer("Сначала заполним профиль: /start")
            return

        # Keep /plan interactive (date -> store -> days), but allow /plan N to prefill days.
        days_prefill: int | None = None
        if message.text:
            parts = message.text.strip().split()
            if len(parts) >= 2 and parts[1].isdigit():
                days_prefill = max(1, min(int(parts[1]), 7))

        await user_repo.set_dialog(user, state="plan_when", step=0, data={"days_prefill": days_prefill} if days_prefill else None)
        await db.commit()
        await message.answer("📅 На какой день сделать рацион?", reply_markup=plan_when_kb())
        return


async def _generate_plan_for_days(message: Message, *, db: Any, user: Any, days: int, start_date: dt.date, store_only: str | None) -> None:
    plan_repo = PlanRepo(db)
    pref_repo = PreferenceRepo(db)
    prefs = await pref_repo.get_json(user.id)
    # store constraint is per-generation (do NOT persist as user preference)
    if store_only:
        store_only = str(store_only).strip() or None
        if store_only and store_only.lower() == "any":
            store_only = None

    # choose target kcal/macros: prefer explicit targets from prefs (incl weekday/weekend)
    targ = prefs.get("targets") if isinstance(prefs.get("targets"), dict) else {}
    def _get_day_kcal(d: dt.date) -> int | None:
        if isinstance(targ, dict):
            wd = d.weekday()
            is_weekday = wd < 5
            if is_weekday and isinstance(targ.get("calories_weekdays"), (int, float)):
                return int(targ.get("calories_weekdays"))
            if (not is_weekday) and isinstance(targ.get("calories_weekends"), (int, float)):
                return int(targ.get("calories_weekends"))
            if isinstance(targ.get("calories"), (int, float)):
                return int(targ.get("calories"))
        return int(user.calories_target) if user.calories_target is not None else None
    macros_override = (
        isinstance(targ, dict)
        and isinstance(targ.get("protein_g"), (int, float))
        and isinstance(targ.get("fat_g"), (int, float))
        and isinstance(targ.get("carbs_g"), (int, float))
    )
    base_macros = {
        "protein_g": int(targ.get("protein_g")) if macros_override else user.protein_g_target,
        "fat_g": int(targ.get("fat_g")) if macros_override else user.fat_g_target,
        "carbs_g": int(targ.get("carbs_g")) if macros_override else user.carbs_g_target,
    }

    try:
        day_plans: list[dict[str, Any]] = []
        for i in range(days):
            d = start_date + dt.timedelta(days=i)
            kcal_target = _get_day_kcal(d)
            if kcal_target is None:
                raise RuntimeError("Нет целевой нормы калорий в профиле.")
            if macros_override:
                macro_line = f"Целевые БЖУ: Б {base_macros.get('protein_g')} / Ж {base_macros.get('fat_g')} / У {base_macros.get('carbs_g')} г.\n"
            else:
                try:
                    mt = macros_for_targets(int(kcal_target), weight_kg=float(user.weight_kg or 0), goal=user.goal or "maintain")  # type: ignore[arg-type]
                    macro_line = f"Целевые БЖУ: Б {mt.protein_g} / Ж {mt.fat_g} / У {mt.carbs_g} г.\n"
                except Exception:
                    macro_line = ""
            # retry if model doesn't match targets or returns invalid JSON
            last_plan: dict[str, Any] | None = None
            last_err: Exception | None = None
            store_line = (
                f"\nВАЖНО: покупка только в магазине: <b>{store_only}</b>. Все products[*].store и shopping_list[*].store должны быть строго '{store_only}'."
                if store_only
                else ""
            )
            user_prompt = (
                _profile_context(user)
                + "\nПредпочтения/режим дня (из БД):\n"
                + dumps(prefs)
                + f"\nСоставь рацион на {d.isoformat()} на <b>{kcal_target} ккал</b>.\n"
                + macro_line
                + store_line
                + "Требования:\n"
                + "- Сумма за день должна попасть в цель (допуск ±5%).\n"
                + "- Продукты реальные и типовые для Чехии (Lidl/Kaufland/Albert/PENNY).\n"
                + "- В каждом приёме пищи обязателен список продуктов с граммами.\n"
                + "- shopping_list обязателен.\n"
                + "- Никаких спорт-добавок (whey/протеин/креатин/гейнер).\n"
            )

            # Speed + cost: try fast model first, then fallback to high-quality model.
            models_to_try: list[str] = []
            m_fast = str(getattr(settings, "openai_plan_model_fast", "") or "").strip()
            if m_fast:
                models_to_try.append(m_fast)
            models_to_try.append(settings.openai_plan_model)
            models_seen: set[str] = set()
            for m in models_to_try:
                if not m or m in models_seen:
                    continue
                models_seen.add(m)
                try:
                    plan_raw = await text_json(
                        system=f"{SYSTEM_COACH}\n\n{DAY_PLAN_JSON}",
                        user=user_prompt,
                        model=m,
                        max_output_tokens=1400,
                        timeout_s=getattr(settings, "openai_plan_timeout_s", 30),
                    )
                except Exception as e:
                    last_err = e
                    continue
                if not isinstance(plan_raw, dict):
                    last_err = RuntimeError("Plan JSON is not an object")
                    continue
                plan = _normalize_day_plan(plan_raw, store_only=store_only)
                last_plan = plan
                if _plan_quality_ok(plan, kcal_target):
                    break
            if last_plan is None:
                raise last_err or RuntimeError("Plan generation failed")
            # If we still didn't hit quality after retries, treat as failure (don't send partial plan)
            if not _plan_quality_ok(last_plan, kcal_target):
                raise RuntimeError(f"Plan quality not OK for target {kcal_target}")
            plan = last_plan
            day_plans.append(plan)
    except Exception as e:
        try:
            print("PLAN_GENERATION_ERROR:", type(e).__name__, _scrub_secrets(str(e))[:500])
            print(traceback.format_exc())
        except Exception:
            pass
        # Do NOT send low-quality plain-text plans (they break store constraints and product clarity).
        # Instead, keep user in "plan_edit" mode with a clear retry action.
        try:
            user_repo = UserRepo(db)
            await user_repo.set_dialog(user, state="plan_edit", step=0, data={"start_date": start_date.isoformat(), "days": days, "store_only": store_only or "any"})
            await db.commit()
        except Exception:
            pass
        err_snip = _scrub_secrets(str(e)).strip()
        err_snip = _escape_html(err_snip[:180]) if err_snip else ""
        await message.answer(
            "⚠️ Не смог собрать качественный рацион (ошибка генерации).\n\n"
            "Жми <b>🔁 Пересобрать рацион</b> — я сделаю новый вариант строго под выбранный магазин и твой КБЖУ.\n"
            f"Тех.деталь: <code>{type(e).__name__}</code>" + (f"\n<code>{err_snip}</code>" if err_snip else ""),
            reply_markup=plan_edit_kb(),
        )
        return

    # persist plans
    for i, plan in enumerate(day_plans):
        d = start_date + dt.timedelta(days=i)
        kcal_target = _get_day_kcal(d) or user.calories_target
        await plan_repo.upsert_day_plan(
            user_id=user.id,
            date=start_date + dt.timedelta(days=i),
            calories_target=int(kcal_target) if kcal_target is not None else None,
            plan=plan,
        )
    await db.commit()
    await _send_plans(message, db=db, user=user, start_date=start_date, day_plans=day_plans, store_only=store_only)

    # enter "plan_edit" mode so the user can iteratively tweak the plan
    try:
        user_repo = UserRepo(db)
        await user_repo.set_dialog(
            user,
            state="plan_edit",
            step=0,
            data={"start_date": start_date.isoformat(), "days": days, "store_only": store_only or "any"},
        )
        await db.commit()
        await message.answer(
            "🛠️ <b>Правки рациона</b>:\n"
            "Напиши, что изменить (пример: «замени перекус 09:00 на вариант за рулём»).\n"
            "Или нажми кнопки ниже 👇",
            reply_markup=plan_edit_kb(),
        )
    except Exception:
        pass

    # clear generating state if still set
    try:
        user_repo = UserRepo(db)
        if user.dialog_state == "plan_generating":
            await user_repo.set_dialog(user, state="plan_edit", step=0, data={"start_date": start_date.isoformat(), "days": days, "store_only": store_only or "any"})
            await db.commit()
    except Exception:
        pass


@router.message(Command("recipe"))
async def cmd_recipe(message: Message) -> None:
    text = (message.text or "").strip()
    payload = text[len("/recipe") :].strip()
    if not payload:
        await message.answer(
            "Пришли ингредиенты в формате строк, например:\n"
            "<pre>курица 200г 220ккал Б 40 Ж 5 У 0\nрис 150г 180ккал Б 4 Ж 1 У 38</pre>\n"
            "И я посчитаю итог и на 100г."
            ,
            reply_markup=main_menu_kb(),
        )
        return

    rows = parse_ingredients_block(payload)
    if not rows:
        await message.answer("Не смог распознать строки. Нужны: граммы, ккал, Б/Ж/У на строку.", reply_markup=main_menu_kb())
        return

    totals = compute_totals(rows)
    tbl = recipe_table([r.__dict__ for r in rows])
    per100 = totals.get("per_100g") or {}
    await message.answer(
        "Расчет рецепта:\n"
        f"<pre>{tbl}</pre>\n"
        f"Итого: {totals['total_weight_g']} г, {totals['calories']:.0f} ккал, "
        f"Б {totals['protein_g']:.1f} / Ж {totals['fat_g']:.1f} / У {totals['carbs_g']:.1f}\n"
        f"На 100г: {per100.get('calories', 0):.0f} ккал, "
        f"Б {per100.get('protein_g', 0):.1f} / Ж {per100.get('fat_g', 0):.1f} / У {per100.get('carbs_g', 0):.1f}"
        ,
        reply_markup=main_menu_kb(),
    )


@router.message(Command("week"))
async def cmd_week(message: Message) -> None:
    if not message.from_user:
        return

    async with SessionLocal() as db:
        user_repo = UserRepo(db)
        meal_repo = MealRepo(db)
        stat_repo = StatRepo(db)
        note_repo = CoachNoteRepo(db)
        pref_repo = PreferenceRepo(db)
        wrepo = WeightLogRepo(db)
        user = await user_repo.get_or_create(message.from_user.id, message.from_user.username)
        if not user.profile_complete:
            await message.answer("Сначала заполним профиль: /start")
            return

        end = _utcnow_naive()
        start = end - dt.timedelta(days=7)
        meals = await meal_repo.meals_between(user.id, start, end)
        diary = []
        cals = []
        for m in meals:
            if m.calories is not None:
                cals.append(int(m.calories))
            diary.append(
                {
                    "created_at": m.created_at.isoformat(),
                    "source": m.source,
                    "calories": m.calories,
                    "protein_g": m.protein_g,
                    "fat_g": m.fat_g,
                    "carbs_g": m.carbs_g,
                    "total_weight_g": m.total_weight_g,
                    "description_raw": m.description_raw,
                }
            )

        try:
            analysis = await text_json(
                system=f"{SYSTEM_NUTRITIONIST}\n\n{WEEKLY_ANALYSIS_JSON}",
                user=_profile_context(user) + "\nДневник за 7 дней:\n" + dumps(diary),
                max_output_tokens=1200,
            )
        except Exception:
            txt = await text_output(
                system=SYSTEM_NUTRITIONIST
                + "\nПроанализируй дневник за 7 дней и профиль: ошибки, рекомендации, поддержка. Пиши пунктами.",
                user=_profile_context(user) + "\nДневник за 7 дней:\n" + dumps(diary),
                max_output_tokens=1200,
            )
            out = _safe_nonempty_text(_sanitize_ai_text(txt), fallback="⚠️ Не смог получить текст анализа. Попробуй ещё раз через пару секунд.")
            await message.answer(out[:3900], reply_markup=main_menu_kb())
            return

        parts = [
            f"<b>Итог</b>: {analysis.get('summary')}",
            "\n<b>Ошибки</b>:\n" + "\n".join([f"- {x}" for x in (analysis.get('mistakes') or [])]) if analysis.get("mistakes") else "",
            "\n<b>Рекомендации</b>:\n" + "\n".join([f"- {x}" for x in (analysis.get('recommendations') or [])]) if analysis.get("recommendations") else "",
        ]
        ca = analysis.get("calorie_adjustment")
        if ca:
            parts.append(f"\n<b>Корректировка калорий</b>: {ca.get('new_calories')} ккал — {ca.get('reason')}")
        await message.answer("\n".join([p for p in parts if p.strip()]))

        # durable coach memory
        try:
            await note_repo.add_note(
                user_id=user.id,
                kind="weekly_review",
                title="Недельный разбор",
                note_json={"analysis": analysis},
            )
        except Exception:
            pass

        # TDEE calibration (deterministic) based on weight trend; save to prefs + note
        try:
            prefs = await pref_repo.get_json(user.id)
            weights = await wrepo.last_days(user.id, days=21)
            if user.calories_target:
                calib = compute_calibration_from_weights(weights=weights, current_target_kcal=int(user.calories_target))
            else:
                calib = None
            if calib:
                await pref_repo.merge(user.id, {"tdee_calibrated_kcal": calib["calibrated_tdee_kcal"], "tdee_calibration": calib})
                await note_repo.add_note(user_id=user.id, kind="tdee_calibration", title="Калибровка TDEE по весу", note_json=calib)
                await db.commit()
                await message.answer(
                    "Калибровка по динамике веса (реальные данные):\n"
                    f"- средний вес (пред 7д): <b>{calib['avg_weight_prev7']} кг</b>\n"
                    f"- средний вес (посл 7д): <b>{calib['avg_weight_last7']} кг</b>\n"
                    f"- темп: <b>{calib['actual_loss_kg_per_week']} кг/нед</b>\n"
                    f"- implied дефицит: <b>{calib['implied_deficit_kcal_per_day']} ккал/день</b>\n"
                    f"- оценка TDEE: <b>{calib['calibrated_tdee_kcal']} ккал</b>\n\n"
                    "Это не меняет калории автоматически — но теперь тренер будет опираться на эту оценку.",
                    reply_markup=main_menu_kb(),
                )
        except Exception:
            pass

        # persist weekly snapshot into stats
        avg_cal = int(round(sum(cals) / len(cals))) if cals else None
        await stat_repo.add_week_stat(
            user_id=user.id,
            week_start=start.date(),
            week_end=end.date(),
            avg_calories=avg_cal,
            notes=analysis,
            weight_start_kg=None,
            weight_end_kg=user.weight_kg,
        )
        await db.commit()

        if ca and ca.get("new_calories") is not None:
            await user_repo.set_dialog(user, state="apply_calories", step=1, data={"new_calories": ca.get("new_calories")})
            await db.commit()
            await message.answer("Применить новую норму калорий? (да/нет)")


@router.message()
async def any_text(message: Message) -> None:
    if not message.from_user:
        return

    async with SessionLocal() as db:
        user_repo = UserRepo(db)
        meal_repo = MealRepo(db)
        food_repo = FoodRepo(db)
        food_service = FoodService(food_repo)
        user = await user_repo.get_or_create(message.from_user.id, message.from_user.username)

        # If a long-running plan is being generated, keep UX tight.
        t_now = (message.text or "").strip()
        if user.dialog_state == "plan_generating":
            # auto-timeout: if stuck too long, reset
            try:
                data = loads(user.dialog_data_json) if user.dialog_data_json else {}
                started = data.get("started_at_utc") if isinstance(data, dict) else None
                if isinstance(started, str):
                    st = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
                    if (dt.datetime.now(dt.timezone.utc) - st) > dt.timedelta(seconds=90):
                        await user_repo.set_dialog(user, state=None, step=None, data=None)
                        await db.commit()
                        await message.answer("⚠️ Похоже, генерация зависла. Сбросил режим.\n\nЖми 🗓️ Рацион на день ещё раз.", reply_markup=main_menu_kb())
                        return
            except Exception:
                pass

            if _norm_text(t_now) in {_norm_text(BTN_CANCEL), "отмена"} or t_now in {"❌ Отмена", BTN_MENU}:
                await user_repo.set_dialog(user, state=None, step=None, data=None)
                await db.commit()
                await message.answer("Ок, отменил. 🧠 Если нужно — снова жми 🗓️ Рацион на день.", reply_markup=main_menu_kb())
                return
            await message.answer("⏳ Я собираю рацион прямо сейчас…\n\nПодожди 10–40 сек или нажми ❌ Отмена.", reply_markup=cancel_kb())
            return

        handled = await _handle_targets_mode(message, user_repo=user_repo, user=user, db=db)
        if handled:
            await db.commit()
            return

        handled = await _handle_daily_checkin(message, user_repo=user_repo, user=user, db=db)
        if handled:
            await db.commit()
            return

        handled = await _handle_coach_onboarding(message, user_repo, user)
        if handled:
            await db.commit()
            return

        handled = await _handle_onboarding_step(message, user_repo, user)
        if handled:
            await db.commit()
            return

        picked = await _handle_food_pick(message, user_repo=user_repo, food_service=food_service, user=user)
        if picked and picked.get("handled") and picked.get("draft"):
            await _start_meal_confirm(
                message,
                user_repo,
                user,
                picked["draft"],
                source=picked.get("source") or "text",
                photo_file_id=picked.get("photo_file_id"),
            )
            await db.commit()
            return
        if picked and picked.get("handled"):
            await db.commit()
            return

        handled = await _handle_photo_clarify(
            message,
            bot=message.bot,
            user_repo=user_repo,
            meal_repo=meal_repo,
            food_service=food_service,
            user=user,
        )
        if handled:
            await db.commit()
            return

        handled = await _handle_meal_clarify(message, user_repo=user_repo, food_service=food_service, user=user)
        if handled:
            await db.commit()
            return

        handled = await _handle_meal_confirm(message, user_repo=user_repo, meal_repo=meal_repo, user=user)
        if handled:
            await db.commit()
            return

        handled = await _handle_apply_calories(message, user_repo=user_repo, user=user)
        if handled:
            await db.commit()
            return

        if not user.profile_complete:
            await message.answer("Сначала заполним профиль: напиши /start")
            return

        # Menu buttons
        t = (message.text or "").strip()
        if t in {BTN_MENU}:
            await message.answer("Меню:", reply_markup=main_menu_kb())
            return
        if t in {BTN_HELP}:
            await cmd_help(message)
            return
        if t in {BTN_PROFILE}:
            await cmd_profile(message)
            return
        if t in {BTN_PLAN}:
            await user_repo.set_dialog(user, state="plan_when", step=0, data=None)
            await db.commit()
            await message.answer("📅 На какой день сделать рацион?", reply_markup=plan_when_kb())
            return
        if t in {BTN_WEEK}:
            await cmd_week(message)
            return
        if t in {BTN_REMINDERS}:
            await user_repo.set_dialog(user, state="reminders_setup", step=0, data=None)
            await db.commit()
            await message.answer(
                "Ок. Опиши напоминания одним сообщением.\n"
                "Примеры:\n"
                "- «каждый день в 06:00 спроси вес»\n"
                "- «в 09:00 по будням перекус»\n"
                "- «в 21:30 спроси, как прошёл день и соблюдал ли калории»\n"
                "- «каждые 3 дня попроси фото и замеры»\n\n"
                "Чтобы отменить — напиши: ❌ Отмена",
                reply_markup=main_menu_kb(),
            )
            return
        if t in {BTN_PROGRESS}:
            await user_repo.set_dialog(user, state="progress_mode", step=0, data=None)
            await db.commit()
            await message.answer(
                "Ок, режим прогресса.\n"
                "- Пришли замеры текстом (пример: «талия 102, грудь 112, бедра 108»)\n"
                "- Или пришли фото прогресса (можно написать в подписи «прогресс»)\n"
                "- Напиши «сравни» — сравню последние записи/фото.\n\n"
                "Чтобы отменить — напиши: ❌ Отмена",
                reply_markup=main_menu_kb(),
            )
            return
        if t in {BTN_WEIGHT}:
            await user_repo.set_dialog(user, state="set_weight", step=0, data=None)
            await db.commit()
            await message.answer("Напиши новый вес в кг (например: 82.5).", reply_markup=main_menu_kb())
            return
        if t in {BTN_PHOTO_HELP}:
            await message.answer("Ок. Просто отправь фото блюда сюда — я разберу и посчитаю.", reply_markup=main_menu_kb())
            return
        if t in {BTN_LOG_MEAL}:
            await message.answer("Ок. Напиши прием пищи (например: «гречка 200г, курица 150г, масло 10г»).", reply_markup=main_menu_kb())
            return

        # reminders setup dialog
        if user.dialog_state == "reminders_setup":
            if t in {"❌ Отмена"}:
                await user_repo.set_dialog(user, state=None, step=None, data=None)
                await db.commit()
                await message.answer("Ок, отменил.", reply_markup=main_menu_kb())
                return
            # reuse coach memory extractor; it already stores to preferences + coach_notes
            pref_repo = PreferenceRepo(db)
            handled = await _apply_coach_memory_if_needed(message, pref_repo=pref_repo, user=user)
            await user_repo.set_dialog(user, state=None, step=None, data=None)
            await db.commit()
            if not handled:
                await message.answer("Не понял напоминания. Напиши проще (время + что спрашивать).", reply_markup=main_menu_kb())
            return

        # progress mode dialog (text only; photos handled in photo handler)
        if user.dialog_state == "progress_mode":
            if t in {"❌ Отмена"}:
                await user_repo.set_dialog(user, state=None, step=None, data=None)
                await db.commit()
                await message.answer("Ок, вышел из режима прогресса.", reply_markup=main_menu_kb())
                return
            # "compare" request
            if any(x in _norm_text(t) for x in ["сравни", "анализ", "прогресс"]):
                note_repo = CoachNoteRepo(db)
                pref_repo = PreferenceRepo(db)
                plan_repo = PlanRepo(db)
                handled = await _handle_coach_chat(
                    message,
                    pref_repo=pref_repo,
                    meal_repo=meal_repo,
                    plan_repo=plan_repo,
                    note_repo=note_repo,
                    user=user,
                )
                await db.commit()
                return
            # store measurements as durable note
            try:
                note_repo = CoachNoteRepo(db)
                await note_repo.add_note(user_id=user.id, kind="measurements", title="Замеры", note_text=t)
                await db.commit()
                await message.answer("Сохранил замеры. Напиши «сравни», чтобы я оценил динамику.", reply_markup=main_menu_kb())
            except Exception:
                await message.answer("Не смог сохранить замеры. Попробуй ещё раз.", reply_markup=main_menu_kb())
            return

        # set_weight dialog
        if user.dialog_state == "set_weight":
            w = _parse_float(t)
            if w is None:
                await message.answer("Вес числом (пример: 82.5).", reply_markup=main_menu_kb())
                return
            user.weight_kg = float(w)
            # persist daily weight log (local date by timezone)
            try:
                pref_repo = PreferenceRepo(db)
                prefs = await pref_repo.get_json(user.id)
                tz = _tz_from_prefs(prefs)
                today_local = dt.datetime.now(dt.timezone.utc).astimezone(tz).date()
                wrepo = WeightLogRepo(db)
                await wrepo.upsert(user_id=user.id, date=today_local, weight_kg=float(w))
            except Exception:
                pass
            # recompute meta always, but do not overwrite custom targets
            pref_repo = PreferenceRepo(db)
            prefs = await pref_repo.get_json(user.id)
            deficit_pct = prefs.get("deficit_pct")
            tr, meta = compute_targets_with_meta(
                sex=user.sex,  # type: ignore[arg-type]
                age=user.age,
                height_cm=user.height_cm,
                weight_kg=user.weight_kg,
                activity=user.activity_level,  # type: ignore[arg-type]
                goal=user.goal,  # type: ignore[arg-type]
                deficit_pct=float(deficit_pct) if deficit_pct is not None else None,
            )
            targets_source = str(prefs.get("targets_source") or "coach").strip().lower()
            if targets_source != "custom":
                user.calories_target = tr.calories
                user.protein_g_target = tr.protein_g
                user.fat_g_target = tr.fat_g
                user.carbs_g_target = tr.carbs_g
                await pref_repo.merge(
                    user.id,
                    {"targets_source": "coach", "targets": {"calories": tr.calories, "protein_g": tr.protein_g, "fat_g": tr.fat_g, "carbs_g": tr.carbs_g}},
                )
            else:
                tz = _tz_from_prefs(prefs)
                today_local = dt.datetime.now(dt.timezone.utc).astimezone(tz).date()
                active = _active_targets(prefs=prefs, user=user, date_local=today_local)
                if active.get("kcal") is not None:
                    user.calories_target = int(active["kcal"])
                if active.get("protein_g") is not None:
                    user.protein_g_target = int(active["protein_g"])
                if active.get("fat_g") is not None:
                    user.fat_g_target = int(active["fat_g"])
                if active.get("carbs_g") is not None:
                    user.carbs_g_target = int(active["carbs_g"])

            await pref_repo.merge(user.id, {"bmr_kcal": meta.bmr_kcal, "tdee_kcal": meta.tdee_kcal, "deficit_pct": meta.deficit_pct})
            await user_repo.set_dialog(user, state=None, step=None, data=None)
            try:
                note_repo = CoachNoteRepo(db)
                await note_repo.add_note(
                    user_id=user.id,
                    kind="weight_update",
                    title="Обновление веса",
                    note_json={"weight_kg": float(w), "calories_target": user.calories_target, "macros": {"p": user.protein_g_target, "f": user.fat_g_target, "c": user.carbs_g_target}},
                )
            except Exception:
                pass
            await db.commit()
            await message.answer(
                f"⚖️ Вес обновил: <b>{w} кг</b> ✅\n"
                f"🎯 Текущая цель: <b>{user.calories_target} ккал</b>\n"
                f"🥩🧈🍚 БЖУ: <b>{user.protein_g_target}/{user.fat_g_target}/{user.carbs_g_target} г</b>",
                reply_markup=main_menu_kb(),
            )
            return

        # plan dialogs (date + days)
        if user.dialog_state in {"plan_when", "plan_date", "plan_store", "plan_days"}:
            if t in {BTN_CANCEL, "❌ Отмена", BTN_MENU}:
                await user_repo.set_dialog(user, state=None, step=None, data=None)
                await db.commit()
                await message.answer("Ок, отменил.", reply_markup=main_menu_kb())
                return

            pref_repo = PreferenceRepo(db)
            prefs = await pref_repo.get_json(user.id)
            tz = _tz_from_prefs(prefs)
            today_local = dt.datetime.now(dt.timezone.utc).astimezone(tz).date()

            if user.dialog_state == "plan_when":
                # keep any prefills (e.g. /plan N)
                try:
                    prev_data = loads(user.dialog_data_json) if user.dialog_data_json else {}
                except Exception:
                    prev_data = {}

                t_norm = _norm_text(t)
                # accept free-form text too
                if t == BTN_PLAN_TODAY or "сегодня" in t_norm:
                    start_date = today_local
                elif t == BTN_PLAN_TOMORROW or "завтра" in t_norm:
                    start_date = today_local + dt.timedelta(days=1)
                elif t == BTN_PLAN_AFTER_TOMORROW or "послезавтра" in t_norm:
                    start_date = today_local + dt.timedelta(days=2)
                elif t == BTN_PLAN_OTHER_DATE:
                    keep = prev_data if isinstance(prev_data, dict) and prev_data else None
                    await user_repo.set_dialog(user, state="plan_date", step=0, data=keep)
                    await db.commit()
                    await message.answer("Введи дату (DD.MM или YYYY-MM-DD). Например: 03.02 или 2026-02-03.", reply_markup=main_menu_kb())
                    return
                else:
                    # try parse date directly from free text
                    s0 = t_norm
                    start_date = None
                    m1 = re.search(r"(\d{4})-(\d{2})-(\d{2})", s0)
                    if m1:
                        try:
                            start_date = dt.date(int(m1.group(1)), int(m1.group(2)), int(m1.group(3)))
                        except Exception:
                            start_date = None
                    if start_date is None:
                        m2 = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", s0)
                        if m2:
                            try:
                                dd = int(m2.group(1))
                                mm = int(m2.group(2))
                                yy = int(m2.group(3)) if m2.group(3) else today_local.year
                                start_date = dt.date(yy, mm, dd)
                            except Exception:
                                start_date = None
                    if start_date is None:
                        await message.answer("Выбери день кнопкой 👇 (или напиши: «завтра», «послезавтра», «03.02»).", reply_markup=plan_when_kb())
                        return
                    if start_date < today_local:
                        await message.answer(f"Эта дата в прошлом ({start_date.isoformat()}). Выбери сегодня/будущую.", reply_markup=plan_when_kb())
                        return

                data2: dict[str, Any] = {"start_date": start_date.isoformat()}
                if isinstance(prev_data, dict) and isinstance(prev_data.get("days_prefill"), int):
                    data2["days_prefill"] = prev_data.get("days_prefill")
                await user_repo.set_dialog(user, state="plan_store", step=0, data=data2)
                await db.commit()
                await message.answer(f"🛒 Ок. Старт: <b>{start_date.isoformat()}</b>.\nГде покупаем?", reply_markup=plan_store_kb())
                return

            if user.dialog_state == "plan_date":
                try:
                    prev_data = loads(user.dialog_data_json) if user.dialog_data_json else {}
                except Exception:
                    prev_data = {}
                s0 = _norm_text(t)
                start_date: dt.date | None = None
                # YYYY-MM-DD
                m1 = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s0)
                if m1:
                    try:
                        start_date = dt.date(int(m1.group(1)), int(m1.group(2)), int(m1.group(3)))
                    except Exception:
                        start_date = None
                # DD.MM or DD.MM.YYYY
                if start_date is None:
                    m2 = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?$", s0)
                    if m2:
                        try:
                            dd = int(m2.group(1))
                            mm = int(m2.group(2))
                            yy = int(m2.group(3)) if m2.group(3) else today_local.year
                            start_date = dt.date(yy, mm, dd)
                        except Exception:
                            start_date = None

                if start_date is None:
                    await message.answer("Не понял дату. Пример: 03.02 или 2026-02-03.", reply_markup=main_menu_kb())
                    return
                if start_date < today_local:
                    await message.answer(f"Эта дата в прошлом ({start_date.isoformat()}). Введи будущую/сегодня.", reply_markup=main_menu_kb())
                    return

                data2: dict[str, Any] = {"start_date": start_date.isoformat()}
                if isinstance(prev_data, dict) and isinstance(prev_data.get("days_prefill"), int):
                    data2["days_prefill"] = prev_data.get("days_prefill")
                await user_repo.set_dialog(user, state="plan_store", step=0, data=data2)
                await db.commit()
                await message.answer(f"🛒 Ок. Старт: <b>{start_date.isoformat()}</b>.\nГде покупаем?", reply_markup=plan_store_kb())
                return

            if user.dialog_state == "plan_store":
                # normalize store choice
                store_choice = None
                if t == BTN_STORE_ANY:
                    store_choice = "any"
                elif t == BTN_STORE_KAUFLAND:
                    store_choice = "Kaufland"
                elif t == BTN_STORE_LIDL:
                    store_choice = "Lidl"
                elif t == BTN_STORE_ALBERT:
                    store_choice = "Albert"
                elif t == BTN_STORE_PENNY:
                    store_choice = "PENNY"
                else:
                    await message.answer("Выбери магазин кнопкой 👇", reply_markup=plan_store_kb())
                    return

                start_date = today_local
                days_prefill: int | None = None
                try:
                    data = loads(user.dialog_data_json) if user.dialog_data_json else {}
                    sd = (data or {}).get("start_date")
                    if isinstance(sd, str):
                        start_date = dt.date.fromisoformat(sd)
                    dp = (data or {}).get("days_prefill")
                    if isinstance(dp, int) and 1 <= dp <= 7:
                        days_prefill = dp
                except Exception:
                    pass

                data2: dict[str, Any] = {"start_date": start_date.isoformat(), "store_only": store_choice}
                if days_prefill is not None:
                    data2["days_prefill"] = days_prefill
                await user_repo.set_dialog(user, state="plan_days", step=0, data=data2)
                await db.commit()
                await message.answer(f"📆 Старт: <b>{start_date.isoformat()}</b> | 🛒 Магазин: <b>{store_choice}</b>\nНа сколько дней? (1-7)", reply_markup=plan_days_kb())
                return

            if user.dialog_state == "plan_days":
                n = _parse_int(t)
                if n is None:
                    try:
                        data = loads(user.dialog_data_json) if user.dialog_data_json else {}
                        dp = (data or {}).get("days_prefill")
                        if isinstance(dp, int) and 1 <= dp <= 7:
                            n = dp
                    except Exception:
                        n = None
                if n is None or not (1 <= n <= 7):
                    await message.answer("Напиши число от 1 до 7 (или выбери кнопку).", reply_markup=plan_days_kb())
                    return
                # pull start_date from dialog data (default: today_local)
                start_date = today_local
                store_only: str | None = None
                try:
                    data = loads(user.dialog_data_json) if user.dialog_data_json else {}
                    sd = (data or {}).get("start_date")
                    if isinstance(sd, str):
                        start_date = dt.date.fromisoformat(sd)
                    so = (data or {}).get("store_only")
                    if isinstance(so, str) and so.strip():
                        store_only = so.strip()
                except Exception:
                    pass

                # mark as generating to prevent "Аууу" / random text from being routed elsewhere
                await user_repo.set_dialog(
                    user,
                    state="plan_generating",
                    step=0,
                    data={"start_date": start_date.isoformat(), "days": n, "store_only": store_only or "any", "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat()},
                )
                await db.commit()
                await message.answer("⏳ Готовлю рацион… (обычно 10–40 сек) 🍽️", reply_markup=cancel_kb())
                await _generate_plan_for_days(message, db=db, user=user, days=n, start_date=start_date, store_only=store_only)
                return

        # plan_edit dialog (iterative tweaks)
        if user.dialog_state == "plan_edit":
            t0 = (message.text or "").strip()
            if t0 in {BTN_PLAN_EDIT_CANCEL, BTN_MENU, BTN_CANCEL, "❌ Отмена"}:
                await user_repo.set_dialog(user, state=None, step=None, data=None)
                await db.commit()
                await message.answer("Ок, закрыл правки. 🧠 Если нужно — снова жми 🗓️ Рацион на день.", reply_markup=main_menu_kb())
                return

            data = loads(user.dialog_data_json) if user.dialog_data_json else {}
            try:
                start_date = dt.date.fromisoformat(str((data or {}).get("start_date")))
            except Exception:
                pref_repo = PreferenceRepo(db)
                prefs = await pref_repo.get_json(user.id)
                tz = _tz_from_prefs(prefs)
                start_date = dt.datetime.now(dt.timezone.utc).astimezone(tz).date()
            days = int((data or {}).get("days") or 1)
            store_only = str((data or {}).get("store_only") or "any").strip()
            if store_only.lower() == "any":
                store_only = "any"

            if t0 == BTN_PLAN_REGEN:
                await _generate_plan_for_days(message, db=db, user=user, days=days, start_date=start_date, store_only=store_only)
                return

            if t0 == BTN_PLAN_APPROVE:
                plan_repo = PlanRepo(db)
                for i in range(days):
                    d = start_date + dt.timedelta(days=i)
                    p = await plan_repo.get_day_plan_json(user.id, d)
                    if isinstance(p, dict):
                        p["_meta"] = {"approved": True, "approved_at_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
                        await plan_repo.upsert_day_plan(user_id=user.id, date=d, calories_target=None, plan=p)
                await db.commit()
                await user_repo.set_dialog(user, state=None, step=None, data=None)
                await db.commit()
                await message.answer("✅ Утвердил! План зафиксирован 💪📌\n\nЗавтра можно так же: подкорректируем и утвердим.", reply_markup=main_menu_kb())
                return

            # Apply memory (so future plans follow new constraints)
            pref_repo = PreferenceRepo(db)
            await _apply_coach_memory_if_needed(message, pref_repo=pref_repo, user=user)
            prefs = await pref_repo.get_json(user.id)

            # Edit day 1 by default; allow "день 2" etc
            day_idx = 1
            mday = re.search(r"(?:день|day)\s*(\d+)", _norm_text(t0))
            if mday:
                try:
                    day_idx = max(1, min(int(mday.group(1)), days))
                except Exception:
                    day_idx = 1
            edit_date = start_date + dt.timedelta(days=day_idx - 1)

            plan_repo = PlanRepo(db)
            current = await plan_repo.get_day_plan_json(user.id, edit_date) or {}
            active = _active_targets(prefs=prefs, user=user, date_local=edit_date)
            kcal_target = int(active.get("kcal") or user.calories_target or 0)
            if kcal_target <= 0:
                await message.answer("Не вижу целевую норму калорий. Открой 👤 Профиль и зафиксируй цели.", reply_markup=main_menu_kb())
                return

            store_line = "" if store_only.lower() == "any" else f"Покупка только в магазине: {store_only}."

            # Ask model to patch the plan
            last_plan: dict[str, Any] | None = None
            last_err: Exception | None = None
            edit_prompt = (
                _profile_context(user)
                + "\nПредпочтения/режим дня (из БД):\n"
                + dumps(prefs)
                + f"\nЦель: {kcal_target} ккал. БЖУ: {active.get('protein_g')}/{active.get('fat_g')}/{active.get('carbs_g')}.\n"
                + store_line
                + f"\nТекущий план на {edit_date.isoformat()}:\n"
                + dumps(current)
                + "\n\nПросьба пользователя:\n"
                + t0
                + "\nВАЖНО: минимальные изменения, пересчитать граммы/ккал под цель, без спорт-добавок.\n"
            )
            models_to_try: list[str] = []
            m_fast = str(getattr(settings, "openai_plan_model_fast", "") or "").strip()
            if m_fast:
                models_to_try.append(m_fast)
            models_to_try.append(settings.openai_plan_model)
            models_seen: set[str] = set()
            for m in models_to_try:
                if not m or m in models_seen:
                    continue
                models_seen.add(m)
                try:
                    patched_raw = await text_json(
                        system=f"{SYSTEM_COACH}\n\n{PLAN_EDIT_JSON}",
                        user=edit_prompt,
                        model=m,
                        max_output_tokens=1400,
                        timeout_s=getattr(settings, "openai_plan_timeout_s", 30),
                    )
                except Exception as e:
                    last_err = e
                    continue
                if not isinstance(patched_raw, dict):
                    last_err = RuntimeError("Patched plan JSON is not an object")
                    continue
                patched = _normalize_day_plan(patched_raw, store_only=store_only)
                last_plan = patched
                if _plan_quality_ok(patched, kcal_target):
                        break
            if last_plan is None:
                raise last_err or RuntimeError("Plan edit failed")
            if not _plan_quality_ok(last_plan, kcal_target):
                raise RuntimeError(f"Plan edit quality not OK for target {kcal_target}")
            new_plan = last_plan
            await plan_repo.upsert_day_plan(user_id=user.id, date=edit_date, calories_target=kcal_target, plan=new_plan)
            await db.commit()

            # Reload all days and show updated plan + shopping list
            day_plans = await _load_day_plans(plan_repo=plan_repo, user_id=user.id, start_date=start_date, days=days)
            await _send_plans(message, db=db, user=user, start_date=start_date, day_plans=day_plans, store_only=store_only)
            await message.answer("🛠️ Ок! Поменял. Хочешь ещё правки? Пиши текстом или жми ✅ Утвердить.", reply_markup=plan_edit_kb())
            return

        # Agent router (free-form commands)
        user_text = (message.text or "").strip()
        route = await _agent_route(user_text, user=user)
        action = (route or {}).get("action")

        if action == "help":
            await cmd_help(message)
            return
        if action == "show_profile":
            await cmd_profile(message)
            return
        if action == "update_weight" and (route or {}).get("weight_kg") is not None:
            w = float(route.get("weight_kg"))
            user.weight_kg = float(w)
            try:
                pref_repo = PreferenceRepo(db)
                prefs = await pref_repo.get_json(user.id)
                tz = _tz_from_prefs(prefs)
                today_local = dt.datetime.now(dt.timezone.utc).astimezone(tz).date()
                wrepo = WeightLogRepo(db)
                await wrepo.upsert(user_id=user.id, date=today_local, weight_kg=float(w))
            except Exception:
                pass
            pref_repo = PreferenceRepo(db)
            prefs = await pref_repo.get_json(user.id)
            deficit_pct = prefs.get("deficit_pct")
            t, meta = compute_targets_with_meta(
                sex=user.sex,  # type: ignore[arg-type]
                age=user.age,
                height_cm=user.height_cm,
                weight_kg=user.weight_kg,
                activity=user.activity_level,  # type: ignore[arg-type]
                goal=user.goal,  # type: ignore[arg-type]
                deficit_pct=float(deficit_pct) if deficit_pct is not None else None,
            )
            targets_source = str(prefs.get("targets_source") or "coach").strip().lower()
            if targets_source != "custom":
                user.calories_target = t.calories
                user.protein_g_target = t.protein_g
                user.fat_g_target = t.fat_g
                user.carbs_g_target = t.carbs_g
                await pref_repo.merge(
                    user.id,
                    {"targets_source": "coach", "targets": {"calories": t.calories, "protein_g": t.protein_g, "fat_g": t.fat_g, "carbs_g": t.carbs_g}},
                )
            else:
                tz = _tz_from_prefs(prefs)
                today_local = dt.datetime.now(dt.timezone.utc).astimezone(tz).date()
                active = _active_targets(prefs=prefs, user=user, date_local=today_local)
                if active.get("kcal") is not None:
                    user.calories_target = int(active["kcal"])
                if active.get("protein_g") is not None:
                    user.protein_g_target = int(active["protein_g"])
                if active.get("fat_g") is not None:
                    user.fat_g_target = int(active["fat_g"])
                if active.get("carbs_g") is not None:
                    user.carbs_g_target = int(active["carbs_g"])

            await pref_repo.merge(user.id, {"bmr_kcal": meta.bmr_kcal, "tdee_kcal": meta.tdee_kcal, "deficit_pct": meta.deficit_pct})
            try:
                note_repo = CoachNoteRepo(db)
                await note_repo.add_note(
                    user_id=user.id,
                    kind="weight_update",
                    title="Обновление веса",
                    note_json={"weight_kg": float(w), "calories_target": user.calories_target, "macros": {"p": user.protein_g_target, "f": user.fat_g_target, "c": user.carbs_g_target}},
                )
            except Exception:
                pass
            # stall detection
            try:
                wrepo = WeightLogRepo(db)
                weights = await wrepo.last_days(user.id, days=21)
                if _detect_weight_stall(weights, days=14) and (user.goal in {"loss", "recomp"}):
                    await note_repo.add_note(user_id=user.id, kind="rule_trigger", title="Стоп веса 14 дней", note_json={"rule": "stall_14d", "weights": weights[-14:]})
                    await message.answer(
                        "Правило сработало: вес стоит ~14 дней.\n"
                        "Выбирай один рычаг на 10 дней:\n"
                        "- минус 150–200 ккал от нормы\n"
                        "ИЛИ\n"
                        "- +2500–3500 шагов/день.\n"
                        "Напиши: «минус 200 ккал» или «+3000 шагов» — и я зафиксирую.",
                        reply_markup=main_menu_kb(),
                    )
            except Exception:
                pass
            await db.commit()
            await message.answer(
                f"Обновил вес: <b>{w} кг</b>.\n"
                f"Новая норма: <b>{t.calories} ккал</b>, БЖУ: <b>{t.protein_g}/{t.fat_g}/{t.carbs_g} г</b>"
            )
            return
        if action == "plan_day":
            # Start interactive flow (date/store/days) for better UX
            async with SessionLocal() as db:
                user_repo = UserRepo(db)
                user = await user_repo.get_or_create(message.from_user.id, message.from_user.username if message.from_user else None)
                if not user.profile_complete:
                    await message.answer("Сначала заполним профиль: /start")
                    return
                await user_repo.set_dialog(user, state="plan_when", step=0, data=None)
                await db.commit()
            await message.answer("📅 На какой день сделать рацион? 👇", reply_markup=plan_when_kb())
            return
        if action == "analyze_week":
            await cmd_week(message)
            return
        if action == "update_prefs":
            pref_repo = PreferenceRepo(db)
            handled = await _apply_coach_memory_if_needed(message, pref_repo=pref_repo, user=user)
            if handled:
                await db.commit()
                return
        if action == "recall_plan":
            plan_repo = PlanRepo(db)
            slot = (route or {}).get("note")
            handled = await _handle_recall_plan(message, plan_repo=plan_repo, user=user, slot_hint=str(slot) if slot else None)
            if handled:
                await db.commit()
                return
        if action == "coach_chat":
            pref_repo = PreferenceRepo(db)
            plan_repo = PlanRepo(db)
            note_repo = CoachNoteRepo(db)
            handled = await _handle_coach_chat(message, pref_repo=pref_repo, meal_repo=meal_repo, plan_repo=plan_repo, note_repo=note_repo, user=user)
            if handled:
                await db.commit()
                return
        if action == "recipe_ai":
            handled = await _handle_recipe_ai(message, user_repo=user_repo, food_service=food_service, user=user, text=(route or {}).get("meal_text") or user_text)
            if handled:
                await db.commit()
                return
        if action == "unknown":
            note = (route or {}).get("note") or "Уточни, что именно сделать?"
            await message.answer(str(note))
            return

        # Default fallback: if it doesn't look like a meal, answer as coach
        if not _looks_like_meal(user_text):
            pref_repo = PreferenceRepo(db)
            plan_repo = PlanRepo(db)
            note_repo = CoachNoteRepo(db)
            handled = await _handle_coach_chat(message, pref_repo=pref_repo, meal_repo=meal_repo, plan_repo=plan_repo, note_repo=note_repo, user=user)
            if handled:
                await db.commit()
                return

        # Otherwise: treat as meal
        meal_text = (route or {}).get("meal_text") or user_text

        # Text -> items (GPT) -> macros (OpenFoodFacts)
        try:
            parsed = await text_json(
                system=f"{SYSTEM_NUTRITIONIST}\n\n{MEAL_ITEMS_JSON}",
                user=_profile_context(user) + "\nВыдели продукты и граммовки:\n" + meal_text,
                max_output_tokens=650,
            )
        except Exception as e:
            await message.answer(f"Не смог разобрать сообщение (ошибка): {e}\nПопробуй написать проще (например: «гречка 200г, курица 150г»).")
            return

        if parsed.get("needs_clarification"):
            qs = parsed.get("clarifying_questions") or []
            if qs:
                await user_repo.set_dialog(
                    user,
                    state="meal_clarify",
                    step=0,
                    data={"draft": parsed, "questions": qs, "answers": [], "source": "text"},
                )
                await db.commit()
                await message.answer(qs[0])
                return
        else:
            extra_qs = _needs_hidden_calorie_clarification(meal_text)
            if extra_qs:
                await user_repo.set_dialog(
                    user,
                    state="meal_clarify",
                    step=0,
                    data={"draft": parsed, "questions": extra_qs, "answers": [], "source": "text"},
                )
                await db.commit()
                await message.answer(extra_qs[0])
                return

        draft2, unresolved_ctx = await _build_meal_from_items(items=parsed.get("items") or [], food_service=food_service)
        if unresolved_ctx:
            await user_repo.set_dialog(user, state="food_pick", step=0, data={"ctx": unresolved_ctx, "source": "text"})
            await db.commit()
            await message.answer(_format_food_pick_question(unresolved_ctx, 0))
            return

        await _start_meal_confirm(message, user_repo, user, draft2 or {}, source="text")
        await db.commit()


async def main() -> None:
    await init_db()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(_checkin_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

