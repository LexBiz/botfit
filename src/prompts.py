from __future__ import annotations


SYSTEM_NUTRITIONIST = """
Ты — персональный AI-нутриционист. Пиши по-русски, кратко и по делу.
Если данных не хватает — задавай уточняющие вопросы.
Не придумывай факты; если оцениваешь — явно помечай как оценку.
""".strip()


PHOTO_ANALYSIS_JSON = """
Верни строго JSON (без текста вокруг). Поля:
{
  "dish_type": string,
  "estimated_weight_g": number,
  "cooking_method": string,
  "hidden_calories": [string, ...],
  "clarifying_questions": [string, ...]
}
Требования:
- dish_type: тип блюда (например: "паста с курицей", "салат", "суши", "бургер")
- estimated_weight_g: оценка общего веса порции в граммах
- cooking_method: пример (жарка/запекание/варка/гриль/сырой/и т.п.)
- hidden_calories: список потенциальных источников скрытых калорий (масло, соусы, сыр, сахар и т.п.)
- clarifying_questions: конкретные вопросы про масло/соус/сахар/размер порции/добавки (5-8 вопросов)
""".strip()

PHOTO_TO_ITEMS_JSON = """
Верни строго JSON (без текста вокруг). Формат:
{
  "items": [
    {"query": string, "grams": number, "barcode": string | null, "brand_hint": string | null}
  ],
  "notes": [string, ...],
  "clarifying_questions": [string, ...]
}
Требования:
- По фото оцени состав порции (компоненты блюда) и граммовки.
- Не считай КБЖУ сам.
- Добавь вопросы про масло/соусы/сахар/сыр/жирность/размер порции.
""".strip()


MEAL_FROM_TEXT_JSON = """
Верни строго JSON (без текста вокруг). Формат:
{
  "items": [
    {"name": string, "grams": number, "calories": number, "protein_g": number, "fat_g": number, "carbs_g": number}
  ],
  "totals": {"total_weight_g": number, "calories": number, "protein_g": number, "fat_g": number, "carbs_g": number},
  "needs_clarification": boolean,
  "clarifying_questions": [string, ...]
}
Если вход неоднозначный (масло/соус/порция/марка/сырость) — needs_clarification=true и задай вопросы.
Если вход достаточен — needs_clarification=false и вопросы пустые.
КБЖУ допустимо оценивать приблизительно (укажи как оценку в questions не надо; только JSON).
""".strip()


MEAL_ITEMS_JSON = """
Верни строго JSON (без текста вокруг). Формат:
{
  "items": [
    {
      "query": string,
      "grams": number,
      "barcode": string | null,
      "brand_hint": string | null
    }
  ],
  "needs_clarification": boolean,
  "clarifying_questions": [string, ...]
}
Требования:
- Не считай КБЖУ сам. Только выдели продукты и граммовки.
- Если неясно масло/соус/сахар/порция/сырость/марка — needs_clarification=true и задай вопросы.
- Если пользователь назвал бренд или магазинный продукт — заполни brand_hint.
- Если пользователь указал штрихкод (8-14 цифр) — заполни barcode.
""".strip()


MEAL_FROM_PHOTO_FINAL_JSON = """
Верни строго JSON (без текста вокруг). Формат:
{
  "items": [
    {"name": string, "grams": number, "calories": number, "protein_g": number, "fat_g": number, "carbs_g": number}
  ],
  "totals": {"total_weight_g": number, "calories": number, "protein_g": number, "fat_g": number, "carbs_g": number},
  "notes": [string, ...]
}
Используй данные фото + ответы пользователя на уточняющие вопросы.
""".strip()


DAY_PLAN_JSON = """
Верни строго JSON (без текста вокруг). Формат:
{
  "meals": [
    {
      "title": string,
      "products": [{"name": string, "grams": number, "store": string}],
      "recipe": [string, ...],
      "kcal": number,
      "protein_g": number,
      "fat_g": number,
      "carbs_g": number
    }
  ],
  "totals": {"kcal": number, "protein_g": number, "fat_g": number, "carbs_g": number},
  "shopping_list": [{"name": string, "grams": number, "store": string}]
}
Условия: страна Чехия, магазины только Lidl/Kaufland/Albert. Продукты реальные и типовые для этих магазинов.
""".strip()


WEEKLY_ANALYSIS_JSON = """
Верни строго JSON (без текста вокруг). Формат:
{
  "summary": string,
  "mistakes": [string, ...],
  "recommendations": [string, ...],
  "calorie_adjustment": {"new_calories": number, "reason": string} | null
}
Проанализируй дневник за 7 дней и профиль. Сфокусируйся на результате и поддержке.
""".strip()


ROUTER_JSON = """
Верни строго JSON (без текста вокруг).
Задача: определить намерение пользователя и вернуть действие для бота.

Формат:
{
  "action": "log_meal" | "plan_day" | "analyze_week" | "update_weight" | "show_profile" | "help" | "unknown",
  "meal_text": string | null,
  "weight_kg": number | null,
  "note": string | null
}

Правила:
- Если сообщение похоже на описание еды/приема пищи/ингредиентов — action="log_meal" и meal_text=исходный текст.
- Если пользователь просит составить рацион/меню на день — action="plan_day".
- Если просит анализ дневника за неделю/7 дней — action="analyze_week".
- Если сообщает новый вес (например "вес 82.5" / "я вешу 82") — action="update_weight" и weight_kg.
- Если просит показать профиль — action="show_profile".
- Если просит помощь/что умеешь/команды — action="help".
- Если непонятно — action="unknown" и note с уточняющим вопросом (1 вопрос).
""".strip()

