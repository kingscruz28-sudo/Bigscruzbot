import asyncio
from types import SimpleNamespace

import pytest

import Main
from Main import scan_chart_image


def block(kind, text=None):
    """A response content block. Thinking blocks carry no text."""
    return SimpleNamespace(type=kind, text=text)


def response(content, stop_reason="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


class FakeMessages:
    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return self.result


class FakeClient:
    def __init__(self, result=None, raises=None):
        self.messages = FakeMessages(result, raises)
        self.beta = SimpleNamespace(messages=self.messages)


def scan(image=b"chart-bytes", mime="image/jpeg"):
    return asyncio.run(scan_chart_image(image, mime))


def test_without_api_key_returns_setup_hint(monkeypatch):
    monkeypatch.setattr(Main, "ai_client", None)

    result = scan()

    assert "ANTHROPIC_API_KEY" in result


def test_returns_analysis_text(monkeypatch):
    client = FakeClient(response([block("text", "Sweep low on Gold. BUY bias.")]))
    monkeypatch.setattr(Main, "ai_client", client)

    result = scan()

    assert "🔍 Chart Scan:" in result
    assert "Sweep low on Gold. BUY bias." in result


def test_sends_image_and_prompt_to_the_vision_model(monkeypatch):
    client = FakeClient(response([block("text", "ok")]))
    monkeypatch.setattr(Main, "ai_client", client)
    monkeypatch.setattr(Main, "CHART_SCAN_MODEL", "claude-opus-5")

    scan(image=b"abc", mime="image/png")

    assert len(client.messages.calls) == 1
    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-5"

    content = call["messages"][0]["content"]
    image_block = next(b for b in content if b["type"] == "image")
    assert image_block["source"]["media_type"] == "image/png"
    assert image_block["source"]["data"] == "YWJj"  # base64 of b"abc"
    assert any(b["type"] == "text" and "CRT" in b["text"] for b in content)


def test_leaves_budget_for_thinking_plus_reply(monkeypatch):
    """Thinking shares max_tokens with the answer on this model, so a budget
    sized only for the reply would truncate it."""
    client = FakeClient(response([block("text", "ok")]))
    monkeypatch.setattr(Main, "ai_client", client)

    scan()

    assert client.messages.calls[0]["max_tokens"] >= 2000


def test_skips_thinking_blocks_when_reading_the_answer(monkeypatch):
    # Thinking blocks arrive first and carry no text; indexing content[0]
    # would blow up here.
    client = FakeClient(
        response([block("thinking", None), block("text", "BUY bias on Gold.")])
    )
    monkeypatch.setattr(Main, "ai_client", client)

    assert "BUY bias on Gold." in scan()


def test_refusal_is_reported_not_crashed(monkeypatch):
    client = FakeClient(response([], stop_reason="refusal"))
    monkeypatch.setattr(Main, "ai_client", client)

    result = scan()

    assert "declined" in result.lower()


def test_empty_analysis_is_reported(monkeypatch):
    client = FakeClient(response([block("text", "   ")]))
    monkeypatch.setattr(Main, "ai_client", client)

    assert "empty" in scan().lower()


def test_api_failure_is_reported_not_raised(monkeypatch):
    client = FakeClient(raises=ConnectionError("anthropic unreachable"))
    monkeypatch.setattr(Main, "ai_client", client)

    result = scan()

    assert "Chart scan failed" in result
    assert "anthropic unreachable" in result


@pytest.mark.parametrize("mime", ["image/jpeg", "image/png", "image/webp"])
def test_passes_through_the_source_mime_type(monkeypatch, mime):
    client = FakeClient(response([block("text", "ok")]))
    monkeypatch.setattr(Main, "ai_client", client)

    scan(mime=mime)

    content = client.messages.calls[0]["messages"][0]["content"]
    image_block = next(b for b in content if b["type"] == "image")
    assert image_block["source"]["media_type"] == mime
