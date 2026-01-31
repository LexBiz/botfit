from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime,
        default=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
    )

    profile_complete: Mapped[bool] = mapped_column(Boolean, default=False)

    # анкета
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sex: Mapped[str | None] = mapped_column(String(16), nullable=True)  # male/female
    height_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    activity_level: Mapped[str | None] = mapped_column(String(16), nullable=True)  # low/medium/high
    goal: Mapped[str | None] = mapped_column(String(16), nullable=True)  # loss/maintain/gain

    allergies: Mapped[str | None] = mapped_column(Text, nullable=True)
    restrictions: Mapped[str | None] = mapped_column(Text, nullable=True)
    favorite_products: Mapped[str | None] = mapped_column(Text, nullable=True)
    disliked_products: Mapped[str | None] = mapped_column(Text, nullable=True)

    country: Mapped[str] = mapped_column(String(8), default="CZ")
    stores_csv: Mapped[str] = mapped_column(String(256), default="Lidl,Kaufland,Albert")

    # цели (снимок текущих расчетов)
    calories_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    protein_g_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fat_g_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    carbs_g_target: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # простая “память” диалога (анкета/уточнение фото и т.п.)
    dialog_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dialog_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dialog_data_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    meals: Mapped[list["Meal"]] = relationship(back_populates="user")
    preferences: Mapped["Preference"] = relationship(back_populates="user", uselist=False)


class Preference(Base):
    __tablename__ = "preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), unique=True, index=True)

    # расширяемые настройки (храним JSON строкой, чтобы не ограничивать)
    json: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="preferences")


class Meal(Base):
    __tablename__ = "meals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))
    eaten_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    source: Mapped[str] = mapped_column(String(16), default="text")  # text/photo/voice/manual
    description_raw: Mapped[str | None] = mapped_column(Text, nullable=True)

    # структура приема пищи (items + totals), хранится JSON-строкой
    meal_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    photo_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    calories: Mapped[int | None] = mapped_column(Integer, nullable=True)
    protein_g: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fat_g: Mapped[int | None] = mapped_column(Integer, nullable=True)
    carbs_g: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_weight_g: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped[User] = relationship(back_populates="meals")


class Stat(Base):
    __tablename__ = "stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)

    week_start: Mapped[dt.date] = mapped_column(Date, index=True)
    week_end: Mapped[dt.date] = mapped_column(Date, index=True)

    weight_start_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight_end_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight_change_kg: Mapped[float | None] = mapped_column(Float, nullable=True)

    avg_calories: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)

    date: Mapped[dt.date] = mapped_column(Date, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))

    calories_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class Food(Base):
    """
    Cache of products from external food databases (e.g. OpenFoodFacts).
    Nutrients are stored per 100g whenever possible.
    """

    __tablename__ = "foods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), default="openfoodfacts")  # openfoodfacts/manual
    barcode: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(256))
    brand: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # nutriments per 100g (JSON)
    nutriments_json: Mapped[str] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime,
        default=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
    )


Index("ix_meals_user_created", Meal.user_id, Meal.created_at)
Index("ix_foods_source_barcode", Food.source, Food.barcode)

