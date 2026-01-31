from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings


def _ensure_db_dir(db_path: str) -> None:
    p = Path(db_path)
    if p.parent and str(p.parent) not in ("", "."):
        os.makedirs(p.parent, exist_ok=True)


def make_engine() -> AsyncEngine:
    if settings.database_url:
        return create_async_engine(
            settings.database_url,
            future=True,
            echo=False,
        )

    _ensure_db_dir(settings.db_path)
    return create_async_engine(f"sqlite+aiosqlite:///{settings.db_path}", future=True, echo=False)


engine: AsyncEngine = make_engine()
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)


async def session() -> AsyncSession:
    return SessionLocal()

