from types import SimpleNamespace

import pytest

import Main
from Main import ask_anthropic, ask_jarvis


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


class TestAskAnthropic:
    def test_raises_without_a_client(self, monkeypatch):
        monkeypatch.setattr(Main, "ai_client", None)

        with pytest.raises(Exception, match="No Anthropic key"):
            ask_anthropic("what's gold doing", "")

    def test_returns_the_reply_text(self, monkeypatch):
        client = FakeClient(response([block("text", "Gold sweeping. SELL bias.")]))
        monkeypatch.setattr(Main, "ai_client", client)

        assert ask_anthropic("gold?", "") == "Gold sweeping. SELL bias."

    def test_skips_thinking_blocks_when_reading_the_answer(self, monkeypatch):
        # Thinking blocks arrive first and carry no text; indexing content[0]
        # would blow up here.
        client = FakeClient(
            response([block("thinking", None), block("text", "SELL bias.")])
        )
        monkeypatch.setattr(Main, "ai_client", client)

        assert ask_anthropic("gold?", "") == "SELL bias."

    def test_sends_system_prompt_and_price_context(self, monkeypatch):
        client = FakeClient(response([block("text", "ok")]))
        monkeypatch.setattr(Main, "ai_client", client)
        monkeypatch.setattr(Main, "CHAT_MODEL", "claude-opus-5")

        ask_anthropic("what's gold doing", "\nCurrent session: ASIAN")

        call = client.messages.calls[0]
        assert call["model"] == "claude-opus-5"
        assert call["system"] == Main.SYSTEM_PROMPT
        assert call["messages"][0]["content"] == (
            "what's gold doing\nCurrent session: ASIAN"
        )

    def test_leaves_budget_for_thinking_plus_reply(self, monkeypatch):
        """Thinking shares max_tokens with the answer on this model, so a
        budget sized only for the reply would truncate it."""
        client = FakeClient(response([block("text", "ok")]))
        monkeypatch.setattr(Main, "ai_client", client)

        ask_anthropic("gold?", "")

        assert client.messages.calls[0]["max_tokens"] >= 2000

    def test_refusal_is_reported_not_crashed(self, monkeypatch):
        client = FakeClient(response([], stop_reason="refusal"))
        monkeypatch.setattr(Main, "ai_client", client)

        assert "declined" in ask_anthropic("gold?", "").lower()

    def test_empty_reply_raises_so_the_caller_can_fall_back(self, monkeypatch):
        client = FakeClient(response([block("text", "   ")]))
        monkeypatch.setattr(Main, "ai_client", client)

        with pytest.raises(Exception, match="no text"):
            ask_anthropic("gold?", "")


class TestAskJarvisRouting:
    def test_prefers_groq_when_configured(self, monkeypatch):
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main, "ANTHROPIC_API_KEY", "anthropic-key")
        monkeypatch.setattr(Main, "ask_groq", lambda m, c: "from groq")
        monkeypatch.setattr(
            Main, "ask_anthropic", lambda m, c: pytest.fail("should not be called")
        )

        assert ask_jarvis("gold?") == "from groq"

    def test_falls_back_to_anthropic_when_groq_fails(self, monkeypatch):
        def broken_groq(message, ctx):
            raise ConnectionError("groq down")

        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main, "ANTHROPIC_API_KEY", "anthropic-key")
        monkeypatch.setattr(Main, "ask_groq", broken_groq)
        monkeypatch.setattr(Main, "ask_anthropic", lambda m, c: "from anthropic")

        assert ask_jarvis("gold?") == "from anthropic"

    def test_uses_anthropic_when_groq_is_unset(self, monkeypatch):
        monkeypatch.setattr(Main, "GROQ_API_KEY", "")
        monkeypatch.setattr(Main, "ANTHROPIC_API_KEY", "anthropic-key")
        monkeypatch.setattr(Main, "ask_anthropic", lambda m, c: "from anthropic")

        assert ask_jarvis("gold?") == "from anthropic"

    def test_reports_unavailable_when_both_providers_fail(self, monkeypatch):
        def broken(message, ctx):
            raise ConnectionError("down")

        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main, "ANTHROPIC_API_KEY", "anthropic-key")
        monkeypatch.setattr(Main, "ask_groq", broken)
        monkeypatch.setattr(Main, "ask_anthropic", broken)

        assert "AI unavailable" in ask_jarvis("gold?")

    def test_reports_unavailable_when_no_keys_configured(self, monkeypatch):
        monkeypatch.setattr(Main, "GROQ_API_KEY", "")
        monkeypatch.setattr(Main, "ANTHROPIC_API_KEY", "")

        assert "AI unavailable" in ask_jarvis("gold?")

    def test_includes_session_and_prices_in_the_context(self, monkeypatch):
        captured = {}

        def capture(message, ctx):
            captured["ctx"] = ctx
            return "ok"

        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main, "ask_groq", capture)

        ask_jarvis("gold?", {"XAU/USD": 2700.0, "BTC/USD": None})

        assert "Current session:" in captured["ctx"]
        assert "Gold: 2700.0000" in captured["ctx"]
        # Unavailable prices are left out rather than sent as None.
        assert "Bitcoin" not in captured["ctx"]
