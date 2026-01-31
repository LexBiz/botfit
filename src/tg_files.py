from __future__ import annotations

from io import BytesIO

from aiogram import Bot


async def download_telegram_file(bot: Bot, file_id: str) -> bytes:
    f = await bot.get_file(file_id)
    buf = BytesIO()
    await bot.download_file(f.file_path, destination=buf)
    return buf.getvalue()

