from __future__ import annotations

import base64
import json
from typing import Any

from openai import AsyncOpenAI

from src.config import settings


client = AsyncOpenAI(api_key=settings.openai_api_key)


def _is_unsupported_param_error(e: Exception, param: str) -> bool:
    # openai-python raises different exception types across versions; parse message best-effort
    msg = str(e).lower()
    p = param.lower()
    return ("unsupported parameter" in msg or "invalid_request_error" in msg) and (p in msg or f"'{p}'" in msg)


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


def _strict_json_suffix() -> str:
    return "\n\nВАЖНО: верни ТОЛЬКО валидный JSON-объект. Без текста, без markdown."


async def _chat_create(
    *,
    model: str,
    messages: list[dict[str, Any]],
    max_output_tokens: int,
    response_format: dict[str, Any] | None,
) -> str:
    """
    Best-effort compatibility layer for Chat Completions across model/SDK differences.
    """
    # Important: some models reject max_tokens and require max_completion_tokens.
    # We only try max_tokens branch if max_completion_tokens is explicitly unsupported.
    last_err: Exception | None = None

    # 1) Try max_completion_tokens with/without response_format
    try:
        kwargs: dict[str, Any] = {"max_completion_tokens": max_output_tokens}
        if response_format is not None:
            kwargs["response_format"] = response_format
        cc = await client.chat.completions.create(model=model, messages=messages, **kwargs)
        return (cc.choices[0].message.content or "").strip()
    except Exception as e:
        last_err = e
        if response_format is not None and _is_unsupported_param_error(e, "response_format"):
            try:
                cc = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_completion_tokens=max_output_tokens,
                )
                return (cc.choices[0].message.content or "").strip()
            except Exception as e2:
                last_err = e2
        # If max_completion_tokens is supported, do NOT fall back to max_tokens.
        if not _is_unsupported_param_error(last_err, "max_completion_tokens"):
            raise RuntimeError(f"Chat completion failed. Last error: {last_err}")

    # 2) Fallback to max_tokens (only if max_completion_tokens unsupported)
    try:
        kwargs2: dict[str, Any] = {"max_tokens": max_output_tokens}
        if response_format is not None:
            kwargs2["response_format"] = response_format
        cc = await client.chat.completions.create(model=model, messages=messages, **kwargs2)
        return (cc.choices[0].message.content or "").strip()
    except Exception as e:
        last_err = e
        if response_format is not None and _is_unsupported_param_error(e, "response_format"):
            cc = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_output_tokens,
            )
            return (cc.choices[0].message.content or "").strip()
        raise RuntimeError(f"Chat completion failed. Last error: {last_err}")


async def text_output(
    *,
    system: str,
    user: str,
    model: str | None = None,
    max_output_tokens: int = 800,
) -> str:
    """
    Like text_json, but returns raw text (never parses JSON). Used as a safe fallback.
    """
    m = model or settings.openai_text_model
    if _has_responses_api():
        resp = await client.responses.create(
            model=m,
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {"role": "user", "content": [{"type": "input_text", "text": user}]},
            ],
            max_output_tokens=max_output_tokens,
        )
        return (getattr(resp, "output_text", None) or "").strip()

    return await _chat_create(
        model=m,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_output_tokens=max_output_tokens,
        response_format=None,
    )


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
        base_messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        # Try with strict response_format when possible
        text = await _chat_create(
            model=m,
            messages=base_messages,
            max_output_tokens=max_output_tokens,
            response_format={"type": "json_object"},
        )

    obj = _try_parse_json(text)
    if obj is None:
        # Retry once with extra strict instruction (works even if response_format isn't supported).
        if not _has_responses_api():
            retry_messages = [
                {"role": "system", "content": system + _strict_json_suffix()},
                {"role": "user", "content": user},
            ]
            text2 = await _chat_create(
                model=m,
                messages=retry_messages,
                max_output_tokens=max_output_tokens,
                response_format=None,
            )
            obj2 = _try_parse_json(text2)
            if obj2 is not None:
                return obj2
            raise ValueError(f"Model did not return JSON after retry. Got: {text2[:500]}")
        raise ValueError(f"Model did not return JSON. Got: {text[:500] or '<empty>'}")
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
        # Preferred content shape
        content = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        try:
            text = await _chat_create(
                model=m,
                messages=messages,
                max_output_tokens=max_output_tokens,
                response_format={"type": "json_object"},
            )
        except Exception:
            # some variants accept image_url as string
            content2 = [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": data_url},
            ]
            messages2 = [
                {"role": "system", "content": system},
                {"role": "user", "content": content2},
            ]
            text = await _chat_create(
                model=m,
                messages=messages2,
                max_output_tokens=max_output_tokens,
                response_format={"type": "json_object"},
            )

    obj = _try_parse_json(text)
    if obj is None:
        if not _has_responses_api():
            # Retry once with explicit JSON-only instruction, without response_format.
            retry_messages = [
                {"role": "system", "content": system + _strict_json_suffix()},
                {"role": "user", "content": [{"type": "text", "text": user_text}, {"type": "image_url", "image_url": {"url": data_url}}]},
            ]
            text2 = await _chat_create(
                model=m,
                messages=retry_messages,
                max_output_tokens=max_output_tokens,
                response_format=None,
            )
            obj2 = _try_parse_json(text2)
            if obj2 is not None:
                return obj2
            raise ValueError(f"Model did not return JSON after retry. Got: {text2[:500]}")
        raise ValueError(f"Model did not return JSON. Got: {text[:500] or '<empty>'}")
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

