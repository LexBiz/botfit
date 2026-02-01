from __future__ import annotations

import types

import pytest

import src.openai_client as oc


class DummyChoice:
    def __init__(self, content: str):
        self.message = types.SimpleNamespace(content=content)


class DummyCC:
    def __init__(self, content: str):
        self.choices = [DummyChoice(content)]


@pytest.mark.asyncio
async def test_text_json_fallback_retries_when_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force "no responses api"
    monkeypatch.setattr(oc, "_has_responses_api", lambda: False)

    calls = {"n": 0}

    async def fake_create(**kwargs):
        calls["n"] += 1
        # first returns non-json, second returns json
        if calls["n"] == 1:
            return DummyCC("hello")
        return DummyCC('{"ok": true}')

    fake_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create))
    )
    monkeypatch.setattr(oc, "client", fake_client)

    obj = await oc.text_json(system="s", user="u", model="x", max_output_tokens=10)
    assert obj["ok"] is True


@pytest.mark.asyncio
async def test_text_json_uses_max_completion_tokens_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "_has_responses_api", lambda: False)

    seen = {"max_completion_tokens": False}

    async def fake_create(**kwargs):
        if "max_completion_tokens" in kwargs:
            seen["max_completion_tokens"] = True
        return DummyCC('{"ok": true}')

    fake_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create))
    )
    monkeypatch.setattr(oc, "client", fake_client)

    obj = await oc.text_json(system="s", user="u", model="x", max_output_tokens=10)
    assert obj["ok"] is True
    assert seen["max_completion_tokens"] is True


@pytest.mark.asyncio
async def test_text_output_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "_has_responses_api", lambda: False)

    async def fake_create(**kwargs):
        return DummyCC("plain text")

    fake_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create))
    )
    monkeypatch.setattr(oc, "client", fake_client)

    t = await oc.text_output(system="s", user="u", model="x", max_output_tokens=10)
    assert "plain text" in t

