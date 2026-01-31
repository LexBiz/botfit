from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


BTN_PROFILE = "👤 Профиль"
BTN_WEIGHT = "⚖️ Обновить вес"
BTN_LOG_MEAL = "🍽️ Добавить еду (текст)"
BTN_PHOTO_HELP = "📸 Добавить еду (фото)"
BTN_PLAN = "🗓️ Рацион на день"
BTN_WEEK = "📈 Анализ 7 дней"
BTN_RECIPE = "🧮 Рецепт (ингредиенты)"
BTN_HELP = "❓ Помощь"
BTN_MENU = "🏠 Меню"


MAIN_BUTTONS: list[list[str]] = [
    [BTN_PROFILE, BTN_WEIGHT],
    [BTN_LOG_MEAL, BTN_PHOTO_HELP],
    [BTN_PLAN, BTN_WEEK],
    [BTN_RECIPE, BTN_HELP],
]


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in MAIN_BUTTONS],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие или просто напиши текстом",
    )


def goal_tempo_kb() -> ReplyKeyboardMarkup:
    # universal set; assistant will clamp if needed
    rows = [
        ["🔥 Жёстко (быстрее)"],
        ["✅ Стандарт"],
        ["🟢 Мягко"],
        ["🧱 Рекомпозиция"],
        ["⚖️ Поддержание"],
        ["📈 Набор"],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Выбери темп",
    )

