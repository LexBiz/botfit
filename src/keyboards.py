from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


BTN_PROFILE = "👤 Профиль"
BTN_WEIGHT = "⚖️ Обновить вес"
BTN_LOG_MEAL = "🍽️ Добавить еду"
BTN_PHOTO_HELP = "📸 Добавить еду (фото)"
BTN_PLAN = "🗓️ Рацион на день"
BTN_WEEK = "📈 Анализ 7 дней"
BTN_REMINDERS = "⏰ Напоминания"
BTN_PROGRESS = "📷📏 Прогресс"
BTN_HELP = "❓ Помощь"
BTN_MENU = "🏠 Меню"


MAIN_BUTTONS: list[list[str]] = [
    [BTN_PROFILE, BTN_WEIGHT],
    [BTN_LOG_MEAL, BTN_PHOTO_HELP],
    [BTN_PLAN, BTN_WEEK],
    [BTN_REMINDERS, BTN_PROGRESS],
    [BTN_HELP],
]


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in MAIN_BUTTONS],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие или просто напиши текстом",
    )


def goal_tempo_kb(preview_kcal: dict[str, int] | None = None) -> ReplyKeyboardMarkup:
    """
    preview_kcal: optional mapping tempo_key -> kcal/day to show in button labels
    (kept parseable by substring keywords in bot.py)
    """
    pk = preview_kcal or {}
    def _p(k: str) -> str:
        v = pk.get(k)
        return f" ~{v} ккал" if isinstance(v, int) else ""

    hard = f"🔥 Жёстко (быстрее{_p('hard')})"
    std = f"✅ Стандарт{_p('standard')}"
    soft = f"🟢 Мягко{_p('soft')}"
    recomp = f"🧱 Рекомпозиция{_p('recomp')}"
    maint = f"⚖️ Поддержание{_p('maintain')}"
    gain = f"📈 Набор{_p('gain')}"

    rows = [
        [hard],
        [std],
        [soft],
        [recomp],
        [maint],
        [gain],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Выбери темп",
    )

