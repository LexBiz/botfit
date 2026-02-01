from __future__ import annotations

import asyncio
import datetime as dt
import math
import re
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message

from src.config import settings
from src.db import SessionLocal
from src.init_db import init_db
from src.jsonutil import dumps, loads
from aiogram.types import ReplyKeyboardRemove

from src.nutrition import compute_targets, compute_targets_with_meta
from src.audio import ogg_opus_to_wav_bytes
from src.openai_client import text_json, text_output, transcribe_audio, vision_json
from src.prompts import (
    COACH_ONBOARD_JSON,
    COACH_MEMORY_JSON,
    COACH_CHAT_GUIDE,
    DAY_PLAN_JSON,
    MEAL_ITEMS_JSON,
    MEAL_FROM_PHOTO_FINAL_JSON,
    MEAL_FROM_TEXT_JSON,
    PHOTO_ANALYSIS_JSON,
    PHOTO_TO_ITEMS_JSON,
    ROUTER_JSON,
    SYSTEM_COACH,
    SYSTEM_NUTRITIONIST,
    WEEKLY_ANALYSIS_JSON,
)
from src.food_service import FoodService, compute_item_macros
from src.keyboards import (
    BTN_HELP,
    BTN_LOG_MEAL,
    BTN_MENU,
    BTN_PHOTO_HELP,
    BTN_PLAN,
    BTN_PROFILE,
    BTN_RECIPE,
    BTN_WEEK,
    BTN_WEIGHT,
    goal_tempo_kb,
    main_menu_kb,
)
from src.render import recipe_table
from src.recipe_calc import compute_totals, parse_ingredients_block
from src.repositories import FoodRepo, MealRepo, PlanRepo, PreferenceRepo, StatRepo, UserRepo
from src.tg_files import download_telegram_file
from src.models import User


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

        await message.answer(
            "Твой профиль:\n"
            f"- Возраст: {user.age}\n"
            f"- Пол: {user.sex}\n"
            f"- Рост: {user.height_cm} см\n"
            f"- Вес: {user.weight_kg} кг\n"
            f"- Активность: {user.activity_level}\n"
            f"- Цель: {_fmt_goal(user.goal)}\n"
            f"- Поддержание (TDEE): {meta.tdee_kcal} ккал\n"
            f"- Дефицит: {meta.deficit_kcal} ккал/день ({_fmt_pct(meta.deficit_pct)})\n"
            f"- Норма: {t.calories} ккал\n"
            f"- БЖУ: {t.protein_g}/{t.fat_g}/{t.carbs_g} г"
            ,
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
        user.calories_target = t.calories
        user.protein_g_target = t.protein_g
        user.fat_g_target = t.fat_g
        user.carbs_g_target = t.carbs_g
        await pref_repo.merge(
            user.id,
            {"bmr_kcal": meta.bmr_kcal, "tdee_kcal": meta.tdee_kcal, "deficit_pct": meta.deficit_pct},
        )
        await db.commit()

    await message.answer(
        f"Обновил вес: <b>{w} кг</b>.\n"
        f"Поддержание (TDEE): <b>{meta.tdee_kcal} ккал</b>\n"
        f"Дефицит: <b>{meta.deficit_kcal} ккал/день</b> ({_fmt_pct(meta.deficit_pct)})\n"
        f"Целевая норма: <b>{t.calories} ккал</b>, БЖУ: <b>{t.protein_g}/{t.fat_g}/{t.carbs_g} г</b>"
        ,
        reply_markup=main_menu_kb(),
    )

@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    async with SessionLocal() as db:
        repo = UserRepo(db)
        user = await repo.get_or_create(message.from_user.id, message.from_user.username if message.from_user else None)
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
    await message.answer("Профиль сброшен. Напиши /start чтобы пройти анкету заново.", reply_markup=main_menu_kb())


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
        },
    )

    await user_repo.set_dialog(user, state=None, step=None, data=None)
    await message.answer(
        "Профиль готов. Работаем по цифрам.\n\n"
        f"Поддержание (TDEE): <b>{meta.tdee_kcal} ккал</b>\n"
        f"Темп: <b>{_fmt_pct(meta.deficit_pct)}</b> (дефицит {meta.deficit_kcal} ккал/день)\n"
        f"Твоя норма: <b>{t.calories} ккал</b>\n"
        f"БЖУ: <b>{t.protein_g}/{t.fat_g}/{t.carbs_g} г</b>",
        reply_markup=main_menu_kb(),
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
    await user_repo.set_dialog(
        user,
        state="meal_confirm",
        step=1,
        data={"draft": draft, "source": source, "photo_file_id": photo_file_id},
    )
    items = draft.get("items") or []
    totals = draft.get("totals") or {}
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
        BTN_RECIPE,
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
        user = await user_repo.get_or_create(message.from_user.id, message.from_user.username)
        if not user.profile_complete:
            await message.answer("Сначала заполним профиль: /start")
            return

        photo = message.photo[-1]
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

    data = loads(user.dialog_data_json) or {}
    questions: list[str] = data.get("questions") or []
    answers: list[str] = data.get("answers") or []
    idx = int(user.dialog_step or 0)
    text = (message.text or "").strip()
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

    text = _norm_text(message.text or "")
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

    await message.answer("Ответь «да» чтобы сохранить или «нет» чтобы отменить.")
    return True


async def _handle_meal_clarify(message: Message, user_repo: UserRepo, food_service: FoodService, user: Any) -> bool:
    if user.dialog_state != "meal_clarify":
        return False

    data = loads(user.dialog_data_json) or {}
    source = data.get("source") or "text"
    qs: list[str] = data.get("questions") or []
    answers: list[str] = data.get("answers") or []
    idx = int(user.dialog_step or 0)
    answers.append((message.text or "").strip())
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
    text = _norm_text(message.text or "")
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


def _parse_dt(s: str | None) -> dt.datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


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
    if isinstance(patch.get("notes"), str) and patch.get("notes"):
        merged_patch["notes"] = str(patch["notes"]).strip()

    if not merged_patch:
        return False

    await pref_repo.merge(user.id, merged_patch)
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
    user: Any,
) -> bool:
    q = (message.text or "").strip()
    if not q:
        return False

    prefs = await pref_repo.get_json(user.id)
    today_plan = await plan_repo.get_day_plan_json(user.id, dt.date.today())
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
    }

    ans = await text_output(
        system=f"{SYSTEM_COACH}\n\n{COACH_CHAT_GUIDE}",
        user="Контекст (из БД):\n" + dumps(ctx) + "\n\nВопрос пользователя:\n" + q,
        max_output_tokens=900,
    )
    await message.answer(ans[:3900], reply_markup=main_menu_kb())
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

        days = 1
        if message.text:
            parts = message.text.strip().split()
            if len(parts) >= 2 and parts[1].isdigit():
                days = max(1, min(int(parts[1]), 7))

        await _generate_plan_for_days(message, db=db, user=user, days=days)
        return


async def _generate_plan_for_days(message: Message, *, db: Any, user: Any, days: int) -> None:
    plan_repo = PlanRepo(db)
    food_service = FoodService(FoodRepo(db))
    pref_repo = PreferenceRepo(db)
    prefs = await pref_repo.get_json(user.id)
    start_date = dt.date.today()
    try:
        day_plans: list[dict[str, Any]] = []
        for i in range(days):
            d = start_date + dt.timedelta(days=i)
            plan = await text_json(
                system=f"{SYSTEM_NUTRITIONIST}\n\n{DAY_PLAN_JSON}",
                user=(
                    _profile_context(user)
                    + "\nПредпочтения/режим дня (из БД):\n"
                    + dumps(prefs)
                    + f"\nСоставь рацион на {d.isoformat()} на {user.calories_target} ккал.\n"
                    + "Требования:\n"
                    + "- Сделай разнообразно (не повторяй одинаковые блюда изо дня в день).\n"
                    + "- Если есть meal_times — привяжи приёмы пищи к этим временам.\n"
                    + "- Учитывай ограничения/аллергии/нелюбимое.\n"
                    + "- shopping_list обязательно заполни.\n"
                ),
                max_output_tokens=1400,
            )
            day_plans.append(plan)
    except Exception:
        # Safe fallback: return plain text plan instead of failing.
        plan_text = await text_output(
            system=SYSTEM_NUTRITIONIST
            + "\nСоставь рацион на день для Чехии (Lidl/Kaufland/Albert) с граммовками, рецептами и КБЖУ. Пиши структурировано.",
            user=_profile_context(user) + f"\nНорма: {user.calories_target} ккал. Составь рацион на день.",
            max_output_tokens=1400,
        )
        await message.answer(plan_text[:3900], reply_markup=main_menu_kb())
        return

    # persist plans
    for i, plan in enumerate(day_plans):
        await plan_repo.upsert_day_plan(
            user_id=user.id,
            date=start_date + dt.timedelta(days=i),
            calories_target=user.calories_target,
            plan=plan,
        )
    await db.commit()

    def _norm_name(n: str) -> str:
        return re.sub(r"\s+", " ", n.strip().lower())

    def _suggest_buy(name: str, grams: float) -> str:
        n = _norm_name(name)
        g = max(float(grams), 0.0)
        # very simple heuristics, labeled as estimate
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
    agg: dict[tuple[str, str], float] = {}
    display: dict[tuple[str, str], str] = {}
    for plan in day_plans:
        # some models may forget shopping_list; build fallback from meal products
        sl = plan.get("shopping_list")
        if not sl:
            sl = []
            for m in (plan.get("meals") or []):
                for p in (m.get("products") or []):
                    sl.append(p)

        for it in (sl or []):
            name = str(it.get("name") or "").strip()
            store = str(it.get("store") or "").strip() or "Lidl"
            grams = float(it.get("grams") or 0)
            if not name or grams <= 0:
                continue
            key = (_norm_name(name), store)
            agg[key] = agg.get(key, 0.0) + grams
            display.setdefault(key, name)

    # enrich with images (limit to avoid slow)
    items_sorted = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)
    shopping_lines: list[str] = []
    for (norm, store), grams in items_sorted[:25]:
        display_name = display.get((norm, store), norm)
        img_url = await food_service.best_image_url(display_name)
        buy_hint = _suggest_buy(display_name, grams)
        shopping_lines.append(
            f"- <b>{display_name}</b> — {grams:.0f} г ({store}). {buy_hint}. "
            f"<a href=\"{img_url}\">фото</a>"
        )

    parts: list[str] = [f"<b>Рацион на {days} дн.</b>"]
    for di, plan in enumerate(day_plans):
        d = start_date + dt.timedelta(days=di)
        meals = plan.get("meals") or []
        totals = plan.get("totals") or {}
        parts.append(f"\n<b>День {di+1} — {d.isoformat()}</b>")
        for i, m in enumerate(meals, start=1):
            tm = str(m.get("time") or "").strip()
            tm_txt = f"{tm} — " if tm else ""
            parts.append(
                f"\n<b>{i}. {tm_txt}{m.get('title')}</b>\n"
                f"КБЖУ: {m.get('kcal')} ккал | Б {m.get('protein_g')} | Ж {m.get('fat_g')} | У {m.get('carbs_g')}\n"
                "Продукты:\n"
                + "\n".join([f"- {p.get('name')} — {p.get('grams')} г ({p.get('store')})" for p in (m.get('products') or [])])
                + "\nРецепт:\n"
                + "\n".join([f"- {s}" for s in (m.get('recipe') or [])])
            )
        parts.append(
            f"\n<b>Итого дня</b>: {totals.get('kcal')} ккал | Б {totals.get('protein_g')} | Ж {totals.get('fat_g')} | У {totals.get('carbs_g')}"
        )

    if shopping_lines:
        shopping_text = "<b>Список покупок (суммарно)</b>:\n" + "\n".join(shopping_lines)
    else:
        shopping_text = ""

    # send plan and shopping list separately to avoid Telegram message truncation
    await message.answer("\n".join(parts)[:3900], reply_markup=main_menu_kb())
    if shopping_text:
        await message.answer(shopping_text[:3900], reply_markup=main_menu_kb())


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
            await message.answer(txt[:3900], reply_markup=main_menu_kb())
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
            await user_repo.set_dialog(user, state="plan_days", step=0, data=None)
            await db.commit()
            await message.answer("На сколько дней сделать рацион? (1-7). Можно просто цифрой.", reply_markup=main_menu_kb())
            return
        if t in {BTN_WEEK}:
            await cmd_week(message)
            return
        if t in {BTN_RECIPE}:
            await message.answer(
                "Пришли ингредиенты строками (как в /recipe), и я посчитаю итог и на 100г.",
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

        # set_weight dialog
        if user.dialog_state == "set_weight":
            w = _parse_float(t)
            if w is None:
                await message.answer("Вес числом (пример: 82.5).", reply_markup=main_menu_kb())
                return
            user.weight_kg = float(w)
            tr = compute_targets(
                sex=user.sex,  # type: ignore[arg-type]
                age=user.age,
                height_cm=user.height_cm,
                weight_kg=user.weight_kg,
                activity=user.activity_level,  # type: ignore[arg-type]
                goal=user.goal,  # type: ignore[arg-type]
            )
            user.calories_target = tr.calories
            user.protein_g_target = tr.protein_g
            user.fat_g_target = tr.fat_g
            user.carbs_g_target = tr.carbs_g
            await user_repo.set_dialog(user, state=None, step=None, data=None)
            await db.commit()
            await message.answer(
                f"Обновил вес: <b>{w} кг</b>.\n"
                f"Новая норма: <b>{tr.calories} ккал</b>, БЖУ: <b>{tr.protein_g}/{tr.fat_g}/{tr.carbs_g} г</b>",
                reply_markup=main_menu_kb(),
            )
            return

        # plan_days dialog
        if user.dialog_state == "plan_days":
            n = _parse_int(t)
            if n is None or not (1 <= n <= 7):
                await message.answer("Напиши число от 1 до 7.", reply_markup=main_menu_kb())
                return
            await user_repo.set_dialog(user, state=None, step=None, data=None)
            await db.commit()
            await _generate_plan_for_days(message, db=db, user=user, days=n)
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
            t = compute_targets(
                sex=user.sex,  # type: ignore[arg-type]
                age=user.age,
                height_cm=user.height_cm,
                weight_kg=user.weight_kg,
                activity=user.activity_level,  # type: ignore[arg-type]
                goal=user.goal,  # type: ignore[arg-type]
            )
            user.calories_target = t.calories
            user.protein_g_target = t.protein_g
            user.fat_g_target = t.fat_g
            user.carbs_g_target = t.carbs_g
            await db.commit()
            await message.answer(
                f"Обновил вес: <b>{w} кг</b>.\n"
                f"Новая норма: <b>{t.calories} ккал</b>, БЖУ: <b>{t.protein_g}/{t.fat_g}/{t.carbs_g} г</b>"
            )
            return
        if action == "plan_day":
            await cmd_plan(message)
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
            handled = await _handle_coach_chat(message, pref_repo=pref_repo, meal_repo=meal_repo, plan_repo=plan_repo, user=user)
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
            handled = await _handle_coach_chat(message, pref_repo=pref_repo, meal_repo=meal_repo, plan_repo=plan_repo, user=user)
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

