from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.jsonutil import dumps, loads
from src.models import CoachNote, DailyCheckin, Food, Goal, Meal, Plan, Preference, Stat, User, WeightLog


class UserRepo:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_or_create(self, telegram_id: int, username: str | None) -> User:
        q: Select[tuple[User]] = select(User).where(User.telegram_id == telegram_id)
        res = await self.db.execute(q)
        u = res.scalar_one_or_none()
        if u:
            if username and u.username != username:
                u.username = username
            return u
        u = User(telegram_id=telegram_id, username=username)
        self.db.add(u)
        await self.db.flush()

        pref = Preference(user_id=u.id, json=dumps({}))
        self.db.add(pref)
        await self.db.flush()
        return u

    async def set_dialog(self, user: User, state: str | None, step: int | None, data: Any | None) -> None:
        user.dialog_state = state
        user.dialog_step = step
        user.dialog_data_json = dumps(data) if data is not None else None

    async def get_dialog_data(self, user: User) -> Any:
        return loads(user.dialog_data_json)


class PreferenceRepo:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, user_id: int) -> Preference:
        q: Select[tuple[Preference]] = select(Preference).where(Preference.user_id == user_id)
        res = await self.db.execute(q)
        pref = res.scalar_one_or_none()
        if pref:
            return pref
        pref = Preference(user_id=user_id, json=dumps({}))
        self.db.add(pref)
        await self.db.flush()
        return pref

    async def get_json(self, user_id: int) -> dict[str, Any]:
        pref = await self.get(user_id)
        obj = loads(pref.json) if pref.json else {}
        return obj if isinstance(obj, dict) else {}

    async def set_json(self, user_id: int, obj: dict[str, Any]) -> None:
        pref = await self.get(user_id)
        pref.json = dumps(obj)

    async def merge(self, user_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        obj = await self.get_json(user_id)
        obj.update(patch)
        await self.set_json(user_id, obj)
        return obj


class MealRepo:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def add_meal(
        self,
        user_id: int,
        source: str,
        description_raw: str | None,
        meal_json: dict[str, Any] | None,
        totals: dict[str, Any] | None,
        photo_file_id: str | None = None,
        eaten_at: dt.datetime | None = None,
    ) -> Meal:
        cal = int(totals.get("calories")) if totals and totals.get("calories") is not None else None
        p = int(totals.get("protein_g")) if totals and totals.get("protein_g") is not None else None
        f = int(totals.get("fat_g")) if totals and totals.get("fat_g") is not None else None
        c = int(totals.get("carbs_g")) if totals and totals.get("carbs_g") is not None else None
        w = int(totals.get("total_weight_g")) if totals and totals.get("total_weight_g") is not None else None

        m = Meal(
            user_id=user_id,
            source=source,
            description_raw=description_raw,
            meal_json=dumps(meal_json) if meal_json is not None else None,
            photo_file_id=photo_file_id,
            eaten_at=eaten_at,
            calories=cal,
            protein_g=p,
            fat_g=f,
            carbs_g=c,
            total_weight_g=w,
        )
        self.db.add(m)
        await self.db.flush()
        return m

    async def last_meals(self, user_id: int, limit: int = 30) -> list[Meal]:
        q = select(Meal).where(Meal.user_id == user_id).order_by(Meal.created_at.desc()).limit(limit)
        res = await self.db.execute(q)
        return list(res.scalars().all())

    async def meals_between(self, user_id: int, start_utc: dt.datetime, end_utc: dt.datetime) -> list[Meal]:
        q = (
            select(Meal)
            .where(Meal.user_id == user_id)
            .where(Meal.created_at >= start_utc)
            .where(Meal.created_at < end_utc)
            .order_by(Meal.created_at.asc())
        )
        res = await self.db.execute(q)
        return list(res.scalars().all())


class PlanRepo:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def upsert_day_plan(self, user_id: int, date: dt.date, calories_target: int | None, plan: dict[str, Any]) -> Plan:
        q: Select[tuple[Plan]] = select(Plan).where(Plan.user_id == user_id).where(Plan.date == date)
        res = await self.db.execute(q)
        p = res.scalar_one_or_none()
        if p:
            p.calories_target = calories_target
            p.plan_json = dumps(plan)
            return p

        p = Plan(user_id=user_id, date=date, calories_target=calories_target, plan_json=dumps(plan))
        self.db.add(p)
        await self.db.flush()
        return p

    async def get_day_plan_json(self, user_id: int, date: dt.date) -> dict[str, Any] | None:
        q: Select[tuple[Plan]] = select(Plan).where(Plan.user_id == user_id).where(Plan.date == date)
        res = await self.db.execute(q)
        p = res.scalar_one_or_none()
        if not p or not p.plan_json:
            return None
        obj = loads(p.plan_json)
        return obj if isinstance(obj, dict) else None


class CoachNoteRepo:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def add_note(
        self,
        *,
        user_id: int,
        kind: str,
        title: str | None = None,
        note_json: dict[str, Any] | None = None,
        note_text: str | None = None,
    ) -> CoachNote:
        n = CoachNote(
            user_id=user_id,
            kind=kind,
            title=title,
            note_json=dumps(note_json) if note_json is not None else None,
            note_text=note_text,
        )
        self.db.add(n)
        await self.db.flush()
        return n

    async def last_notes(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        q = select(CoachNote).where(CoachNote.user_id == user_id).order_by(CoachNote.created_at.desc()).limit(limit)
        res = await self.db.execute(q)
        out: list[dict[str, Any]] = []
        for n in res.scalars().all():
            obj = loads(n.note_json) if n.note_json else None
            out.append(
                {
                    "created_at": n.created_at.isoformat(),
                    "kind": n.kind,
                    "title": n.title,
                    "note_json": obj if isinstance(obj, (dict, list)) else None,
                    "note_text": n.note_text,
                }
            )
        return out


class GoalRepo:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_active(self, user_id: int) -> Goal | None:
        q: Select[tuple[Goal]] = select(Goal).where(Goal.user_id == user_id).where(Goal.active == True)  # noqa: E712
        res = await self.db.execute(q)
        return res.scalar_one_or_none()

    async def set_goal(
        self,
        *,
        user_id: int,
        phase: str,
        target_weight_kg: float | None,
        target_date: dt.date | None,
        notes: str | None,
    ) -> Goal:
        cur = await self.get_active(user_id)
        if cur:
            cur.phase = phase
            cur.target_weight_kg = target_weight_kg
            cur.target_date = target_date
            cur.notes = notes
            return cur
        g = Goal(user_id=user_id, phase=phase, target_weight_kg=target_weight_kg, target_date=target_date, notes=notes, active=True)
        self.db.add(g)
        await self.db.flush()
        return g


class WeightLogRepo:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def upsert(self, *, user_id: int, date: dt.date, weight_kg: float) -> WeightLog:
        q: Select[tuple[WeightLog]] = select(WeightLog).where(WeightLog.user_id == user_id).where(WeightLog.date == date)
        res = await self.db.execute(q)
        w = res.scalar_one_or_none()
        if w:
            w.weight_kg = float(weight_kg)
            return w
        w = WeightLog(user_id=user_id, date=date, weight_kg=float(weight_kg))
        self.db.add(w)
        await self.db.flush()
        return w

    async def last_days(self, user_id: int, days: int = 21) -> list[dict[str, Any]]:
        start = dt.date.today() - dt.timedelta(days=days)
        q = (
            select(WeightLog)
            .where(WeightLog.user_id == user_id)
            .where(WeightLog.date >= start)
            .order_by(WeightLog.date.asc())
        )
        res = await self.db.execute(q)
        return [{"date": x.date.isoformat(), "weight_kg": x.weight_kg} for x in res.scalars().all()]


class DailyCheckinRepo:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def upsert(
        self,
        *,
        user_id: int,
        date: dt.date,
        calories_ok: bool | None,
        protein_ok: bool | None,
        steps: int | None,
        sleep_hours: float | None,
        training_done: bool | None,
        alcohol: bool | None,
        note_text: str | None,
        raw_json: dict[str, Any] | None,
    ) -> DailyCheckin:
        q: Select[tuple[DailyCheckin]] = select(DailyCheckin).where(DailyCheckin.user_id == user_id).where(DailyCheckin.date == date)
        res = await self.db.execute(q)
        c = res.scalar_one_or_none()
        if not c:
            c = DailyCheckin(user_id=user_id, date=date)
            self.db.add(c)
        c.calories_ok = calories_ok
        c.protein_ok = protein_ok
        c.steps = steps
        c.sleep_hours = sleep_hours
        c.training_done = training_done
        c.alcohol = alcohol
        c.note_text = note_text
        c.raw_json = dumps(raw_json) if raw_json is not None else None
        await self.db.flush()
        return c

    async def last_days(self, user_id: int, days: int = 14) -> list[dict[str, Any]]:
        start = dt.date.today() - dt.timedelta(days=days)
        q = (
            select(DailyCheckin)
            .where(DailyCheckin.user_id == user_id)
            .where(DailyCheckin.date >= start)
            .order_by(DailyCheckin.date.asc())
        )
        res = await self.db.execute(q)
        out: list[dict[str, Any]] = []
        for x in res.scalars().all():
            out.append(
                {
                    "date": x.date.isoformat(),
                    "calories_ok": x.calories_ok,
                    "protein_ok": x.protein_ok,
                    "steps": x.steps,
                    "sleep_hours": x.sleep_hours,
                    "training_done": x.training_done,
                    "alcohol": x.alcohol,
                    "note_text": x.note_text,
                }
            )
        return out


class StatRepo:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def add_week_stat(
        self,
        *,
        user_id: int,
        week_start: dt.date,
        week_end: dt.date,
        avg_calories: int | None,
        notes: dict[str, Any] | None = None,
        weight_start_kg: float | None = None,
        weight_end_kg: float | None = None,
    ) -> Stat:
        wc = None
        if weight_start_kg is not None and weight_end_kg is not None:
            wc = float(weight_end_kg) - float(weight_start_kg)
        s = Stat(
            user_id=user_id,
            week_start=week_start,
            week_end=week_end,
            avg_calories=avg_calories,
            notes=dumps(notes) if notes is not None else None,
            weight_start_kg=weight_start_kg,
            weight_end_kg=weight_end_kg,
            weight_change_kg=wc,
        )
        self.db.add(s)
        await self.db.flush()
        return s


class FoodRepo:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_barcode(self, source: str, barcode: str) -> Food | None:
        q: Select[tuple[Food]] = select(Food).where(Food.source == source).where(Food.barcode == barcode)
        res = await self.db.execute(q)
        return res.scalar_one_or_none()

    async def upsert(
        self,
        *,
        source: str,
        barcode: str | None,
        name: str,
        brand: str | None,
        nutriments_json: str,
    ) -> Food:
        if barcode:
            existing = await self.get_by_barcode(source, barcode)
            if existing:
                existing.name = name
                existing.brand = brand
                existing.nutriments_json = nutriments_json
                return existing

        f = Food(source=source, barcode=barcode, name=name, brand=brand, nutriments_json=nutriments_json)
        self.db.add(f)
        await self.db.flush()
        return f

