from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from src.config import settings


def _ffmpeg_exe() -> str | None:
    if settings.ffmpeg_path:
        p = Path(settings.ffmpeg_path)
        if p.exists():
            return str(p)
    return shutil.which("ffmpeg")


def ogg_opus_to_wav_bytes(ogg_bytes: bytes) -> bytes | None:
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        return None

    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "in.ogg"
        out = Path(td) / "out.wav"
        inp.write_bytes(ogg_bytes)

        # 16kHz mono wav
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(inp),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(out),
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if not out.exists():
            return None
        return out.read_bytes()

