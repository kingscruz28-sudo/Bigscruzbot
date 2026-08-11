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


def test_without_any_provider_names_both(monkeypatch):
    """With no reader configured, say what to fix for each of them."""
    monkeypatch.setattr(Main, "ai_client", None)
    monkeypatch.setattr(Main, "GROQ_API_KEY", "")

    result = scan()

    assert "unavailable" in result.lower()
    assert "Anthropic" in result
    assert "GROQ_API_KEY" in result


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


def test_empty_analysis_with_no_backup_is_reported(monkeypatch):
    monkeypatch.setattr(Main, "ai_client", FakeClient(response([block("text", "   ")])))
    monkeypatch.setattr(Main, "GROQ_API_KEY", "")

    assert "unavailable" in scan().lower()


def test_api_failure_is_reported_not_raised(monkeypatch):
    """The reason is surfaced, but bounded.

    The original code pasted the whole exception in — a JSON dump with a
    request_id, unreadable on a phone. Suppressing it entirely turned out to be
    the wrong correction: the reason is the one thing that makes the message
    actionable. So it is kept, truncated, and labelled per provider.
    """
    monkeypatch.setattr(
        Main, "ai_client", FakeClient(raises=ConnectionError("anthropic unreachable"))
    )
    monkeypatch.setattr(Main, "GROQ_API_KEY", "")

    result = scan()

    assert "unavailable" in result.lower()
    assert "anthropic unreachable" in result
    assert len(result) < 400  # a reason, not a stack trace


def test_long_failures_are_truncated(monkeypatch):
    monkeypatch.setattr(Main, "ai_client", FakeClient(raises=Exception("x" * 5000)))
    monkeypatch.setattr(Main, "GROQ_API_KEY", "")

    assert len(scan()) < 400


@pytest.mark.parametrize("mime", ["image/jpeg", "image/png", "image/webp"])
def test_passes_through_the_source_mime_type(monkeypatch, mime):
    client = FakeClient(response([block("text", "ok")]))
    monkeypatch.setattr(Main, "ai_client", client)

    scan(mime=mime)

    content = client.messages.calls[0]["messages"][0]["content"]
    image_block = next(b for b in content if b["type"] == "image")
    assert image_block["source"]["media_type"] == mime


class FakeGroqPost:
    def __init__(self, text=None, raises=None, status=200, body=""):
        self.text_out = text
        self.raises = raises
        self.status = status
        self.body = body
        self.calls = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.raises:
            raise self.raises
        return SimpleNamespace(
            status_code=self.status,
            text=self.body,
            json=lambda: {"choices": [{"message": {"content": self.text_out}}]},
        )


class TestGroqFallback:
    def test_anthropic_success_never_calls_groq(self, monkeypatch):
        client = FakeClient(response([block("text", "Gold sweeping low.")]))
        post = FakeGroqPost("should not be used")
        monkeypatch.setattr(Main, "ai_client", client)
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main.requests, "post", post)

        result = scan()

        assert "Gold sweeping low." in result
        assert post.calls == []
        assert "backup reader" not in result

    def test_credit_error_falls_back_to_groq(self, monkeypatch):
        client = FakeClient(raises=Exception("credit balance is too low"))
        post = FakeGroqPost("Rough read: resistance overhead.")
        monkeypatch.setattr(Main, "ai_client", client)
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main.requests, "post", post)

        result = scan()

        assert "Rough read: resistance overhead." in result
        assert len(post.calls) == 1

    def test_backup_output_says_it_is_the_weaker_model(self, monkeypatch):
        """He must never mistake a Groq read for an Anthropic one."""
        monkeypatch.setattr(Main, "ai_client", FakeClient(raises=Exception("no credit")))
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main.requests, "post", FakeGroqPost("Some analysis."))

        result = scan()

        assert "backup reader" in result
        assert "weaker model" in result

    def test_uses_groq_when_no_anthropic_client(self, monkeypatch):
        post = FakeGroqPost("Read without Anthropic.")
        monkeypatch.setattr(Main, "ai_client", None)
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main.requests, "post", post)

        assert "Read without Anthropic." in scan()

    def test_sends_the_image_as_a_data_uri(self, monkeypatch):
        post = FakeGroqPost("ok")
        monkeypatch.setattr(Main, "ai_client", None)
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main.requests, "post", post)

        scan(image=b"abc", mime="image/png")

        content = post.calls[0]["json"]["messages"][0]["content"]
        image = next(c for c in content if c["type"] == "image_url")
        assert image["image_url"]["url"] == "data:image/png;base64,YWJj"
        assert any(c["type"] == "text" and "CRT" in c["text"] for c in content)

    def test_both_failing_names_each_reason(self, monkeypatch):
        """The message has to be self-diagnosing — no log-diving on a phone."""
        monkeypatch.setattr(
            Main, "ai_client", FakeClient(raises=Exception("credit balance is too low"))
        )
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(
            Main.requests, "post", FakeGroqPost(raises=ConnectionError("groq down"))
        )

        result = scan()

        assert "both readers failed" in result.lower()
        assert "credit balance is too low" in result
        assert "groq down" in result

    def test_reports_an_unknown_model_verbatim(self, monkeypatch):
        """Groq puts 'model not found' in the body, not the status line."""
        monkeypatch.setattr(Main, "ai_client", None)
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(
            Main.requests,
            "post",
            FakeGroqPost(status=404, body='{"error":{"code":"model_not_found"}}'),
        )

        result = scan()

        assert "model_not_found" in result

    def test_falls_through_to_the_next_vision_model(self, monkeypatch):
        attempts = []

        def post(url, headers=None, json=None, timeout=None):
            attempts.append(json["model"])
            if len(attempts) == 1:
                return SimpleNamespace(status_code=404, text="model decommissioned",
                                       json=lambda: {})
            return SimpleNamespace(
                status_code=200, text="",
                json=lambda: {"choices": [{"message": {"content": "Second model read it."}}]},
            )

        monkeypatch.setattr(Main, "ai_client", None)
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main, "GROQ_VISION_MODELS", ["vendor/dead", "vendor/alive"])
        monkeypatch.setattr(Main.requests, "post", post)

        result = scan()

        assert attempts == ["vendor/dead", "vendor/alive"]
        assert "Second model read it." in result
        assert "alive" in result

    def test_empty_anthropic_reply_falls_through_to_groq(self, monkeypatch):
        monkeypatch.setattr(Main, "ai_client", FakeClient(response([block("text", "  ")])))
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main.requests, "post", FakeGroqPost("Backup got it."))

        assert "Backup got it." in scan()
