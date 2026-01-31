from __future__ import annotations

from sqlalchemy import text

from src.db import engine
from src.models import Base


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Pragmas for better concurrency/durability on SQLite
        await conn.execute(text("PRAGMA journal_mode=WAL;"))
        await conn.execute(text("PRAGMA synchronous=NORMAL;"))

