from __future__ import annotations

import base64
import json
from typing import Any

from openai import AsyncOpenAI

from src.config import settings


client = AsyncOpenAI(api_key=settings.openai_api_key)

def _has_responses_api() -> bool:
    return getattr(client, "responses", None) is not None


def _try_parse_json(text: str) -> dict[str, Any] | None:
    t = text.strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    # attempt to extract first {...} block
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(t[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


async def text_json(
    *,
    system: str,
    user: str,
    model: str | None = None,
    max_output_tokens: int = 800,
) -> dict[str, Any]:
    m = model or settings.openai_text_model

    text = ""
    if _has_responses_api():
        # Prefer Responses API.
        resp = await client.responses.create(
            model=m,
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {"role": "user", "content": [{"type": "input_text", "text": user}]},
            ],
            max_output_tokens=max_output_tokens,
        )
        text = (getattr(resp, "output_text", None) or "").strip()
    else:
        # Fallback: Chat Completions API (older SDKs).
        cc = await client.chat.completions.create(
            model=m,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            max_tokens=max_output_tokens,
        )
        text = (cc.choices[0].message.content or "").strip()

    obj = _try_parse_json(text)
    if obj is None:
        raise ValueError(f"Model did not return JSON. Got: {text[:500]}")
    return obj


async def vision_json(
    *,
    system: str,
    user_text: str,
    image_bytes: bytes,
    image_mime: str,
    model: str | None = None,
    max_output_tokens: int = 900,
) -> dict[str, Any]:
    m = model or settings.openai_vision_model
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{image_mime};base64,{b64}"

    text = ""
    if _has_responses_api():
        resp = await client.responses.create(
            model=m,
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": user_text},
                        {"type": "input_image", "image_url": data_url},
                    ],
                },
            ],
            max_output_tokens=max_output_tokens,
        )
        text = (getattr(resp, "output_text", None) or "").strip()
    else:
        # Fallback: Chat Completions with image_url content
        content = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        try:
            cc = await client.chat.completions.create(
                model=m,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
                response_format={"type": "json_object"},
                max_tokens=max_output_tokens,
            )
        except Exception:
            # some variants accept image_url as string
            content2 = [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": data_url},
            ]
            cc = await client.chat.completions.create(
                model=m,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": content2},
                ],
                response_format={"type": "json_object"},
                max_tokens=max_output_tokens,
            )
        text = (cc.choices[0].message.content or "").strip()

    obj = _try_parse_json(text)
    if obj is None:
        raise ValueError(f"Model did not return JSON. Got: {text[:500]}")
    return obj


async def transcribe_audio(*, audio_bytes: bytes, filename: str = "audio.ogg") -> str:
    # Best-effort: transcription API may differ by model/version; keep it isolated.
    from io import BytesIO

    bio = BytesIO(audio_bytes)
    bio.name = filename  # some clients rely on name
    tr = await client.audio.transcriptions.create(
        model=settings.openai_transcribe_model,
        file=bio,
    )
    # openai-python returns object with `.text`
    return getattr(tr, "text", "") or ""

