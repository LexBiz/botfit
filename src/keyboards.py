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
BTN_CANCEL = "❌ Отмена"

BTN_TARGETS_AUTO = "✅ Использовать расчёт тренера"
BTN_TARGETS_CUSTOM = "✍️ Я задам калории/КБЖУ сам"

BTN_PLAN_TODAY = "📅 Сегодня"
BTN_PLAN_TOMORROW = "➡️ Завтра"
BTN_PLAN_AFTER_TOMORROW = "⏭️ Послезавтра"
BTN_PLAN_OTHER_DATE = "🗓️ Другая дата"

BTN_DAYS_1 = "1 день"
BTN_DAYS_3 = "3 дня"
BTN_DAYS_7 = "7 дней"

BTN_STORE_ANY = "🛒 Любой магазин"
BTN_STORE_KAUFLAND = "🟦 Kaufland"
BTN_STORE_LIDL = "🟨 Lidl"
BTN_STORE_ALBERT = "🟥 Albert"
BTN_STORE_PENNY = "🟩 PENNY"

BTN_PLAN_APPROVE = "✅ Утвердить рацион"
BTN_PLAN_REGEN = "🔁 Пересобрать рацион"
BTN_PLAN_EDIT_CANCEL = "↩️ Закрыть правки"


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


def targets_mode_kb() -> ReplyKeyboardMarkup:
    rows = [
        [BTN_TARGETS_AUTO],
        [BTN_TARGETS_CUSTOM],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Как задаём калории?",
    )


def plan_when_kb() -> ReplyKeyboardMarkup:
    rows = [
        [BTN_PLAN_TODAY, BTN_PLAN_TOMORROW],
        [BTN_PLAN_AFTER_TOMORROW, BTN_PLAN_OTHER_DATE],
        [BTN_CANCEL],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        input_field_placeholder="На какой день?",
    )


def plan_days_kb() -> ReplyKeyboardMarkup:
    rows = [
        [BTN_DAYS_1, BTN_DAYS_3, BTN_DAYS_7],
        [BTN_CANCEL],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        input_field_placeholder="На сколько дней?",
    )


def plan_store_kb() -> ReplyKeyboardMarkup:
    rows = [
        [BTN_STORE_ANY],
        [BTN_STORE_KAUFLAND, BTN_STORE_LIDL],
        [BTN_STORE_ALBERT, BTN_STORE_PENNY],
        [BTN_CANCEL],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        input_field_placeholder="Где покупаем?",
    )


def plan_edit_kb() -> ReplyKeyboardMarkup:
    rows = [
        [BTN_PLAN_APPROVE],
        [BTN_PLAN_REGEN],
        [BTN_PLAN_EDIT_CANCEL],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        input_field_placeholder="Напиши, что поменять в рационе",
    )


def cancel_kb() -> ReplyKeyboardMarkup:
    rows = [
        [BTN_CANCEL],
        [BTN_MENU],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        input_field_placeholder="Можно отменить",
    )

