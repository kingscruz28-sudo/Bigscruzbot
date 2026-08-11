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

    def test_reasoning_scratchpad_never_reaches_the_user(self, monkeypatch):
        """qwen thinks in <think> tags. Shipping that to Telegram sent him a
        model arguing with itself instead of a read."""
        monkeypatch.setattr(Main, "ai_client", None)
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(
            Main.requests,
            "post",
            FakeGroqPost(
                "<think>Wait, let me re-read the labels. Is it 64,118?</think>\n"
                "Resistance at 64,100. Wait for the sweep."
            ),
        )

        result = scan()

        assert "<think>" not in result
        assert "re-read the labels" not in result
        assert "Resistance at 64,100. Wait for the sweep." in result

    def test_asks_groq_to_hide_the_scratchpad(self, monkeypatch):
        post = FakeGroqPost("ok")
        monkeypatch.setattr(Main, "ai_client", None)
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main.requests, "post", post)

        scan()

        assert post.calls[0]["json"]["reasoning_format"] == "hidden"

    def test_retries_without_the_parameter_when_the_model_rejects_it(self, monkeypatch):
        """Not every Groq model knows reasoning_format. A 400 on it must not
        burn the model — the tags get stripped locally instead."""
        bodies = []

        def post(url, headers=None, json=None, timeout=None):
            bodies.append(json)
            if "reasoning_format" in json:
                return SimpleNamespace(
                    status_code=400,
                    text='{"error":{"message":"reasoning_format not supported"}}',
                    json=lambda: {},
                )
            return SimpleNamespace(
                status_code=200,
                text="",
                json=lambda: {
                    "choices": [{"message": {"content": "<think>hm</think>Clean read."}}]
                },
            )

        monkeypatch.setattr(Main, "ai_client", None)
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main.requests, "post", post)

        result = scan()

        assert len(bodies) == 2
        assert "reasoning_format" not in bodies[1]
        assert "Clean read." in result
        assert "<think>" not in result

    def test_a_400_unrelated_to_reasoning_is_not_retried(self, monkeypatch):
        post = FakeGroqPost(status=400, body='{"error":{"code":"rate_limit"}}')
        monkeypatch.setattr(Main, "ai_client", None)
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main.requests, "post", post)

        result = scan()

        assert len(post.calls) == 1
        assert "rate_limit" in result

    def test_leaves_room_for_thinking_and_the_answer(self, monkeypatch):
        """500 tokens went entirely on thinking and the reply was cut off."""
        post = FakeGroqPost("ok")
        monkeypatch.setattr(Main, "ai_client", None)
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main.requests, "post", post)

        scan()

        assert post.calls[0]["json"]["max_tokens"] >= 2000

    def test_thinking_that_never_finished_is_a_failure_not_a_read(self, monkeypatch):
        """An unclosed <think> is a truncated monologue. Reporting it as
        unavailable beats presenting it as analysis."""
        monkeypatch.setattr(Main, "ai_client", None)
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(
            Main.requests,
            "post",
            FakeGroqPost("<think>The user wants analysis. Let's look at the"),
        )

        result = scan()

        assert "unavailable" in result.lower()
        assert "budget" in result.lower()
        assert "Let's look at the" not in result

    def test_discovery_is_skipped_when_a_configured_model_answers(self, monkeypatch):
        """The happy path stays one request. Discovery is a safety net."""
        monkeypatch.setattr(Main, "ai_client", None)
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main.requests, "post", FakeGroqPost("Read it."))

        def explode(*a, **k):
            raise AssertionError("/models should not be called")

        monkeypatch.setattr(Main.requests, "get", explode)

        assert "Read it." in scan()

    def test_dead_llama_models_are_gone_from_the_defaults(self):
        """Both returned 'model not found' on his account, so they only cost
        a wasted round-trip on the way to the real failure."""
        assert Main.GROQ_VISION_MODELS == ["qwen/qwen3.6-27b"]


class FakeModelsGet:
    """Stands in for GET /models."""

    def __init__(self, models=None, raises=None, status=200, body=""):
        self.models = models or []
        self.raises = raises
        self.status = status
        self.body = body
        self.calls = 0

    def __call__(self, url, headers=None, timeout=None):
        self.calls += 1
        if self.raises:
            raise self.raises
        return SimpleNamespace(
            status_code=self.status,
            text=self.body,
            json=lambda: {"data": self.models},
        )


@pytest.fixture(autouse=True)
def reset_discovery_cache():
    Main._discovered_vision_models = None
    yield
    Main._discovered_vision_models = None


class TestModelDiscovery:
    def setup_groq(self, monkeypatch):
        monkeypatch.setattr(Main, "ai_client", None)
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")

    def test_discovery_runs_once_the_configured_models_are_spent(self, monkeypatch):
        """A retired pinned model must not be the end of the road."""
        self.setup_groq(monkeypatch)
        monkeypatch.setattr(Main, "GROQ_VISION_MODELS", ["vendor/retired"])
        get = FakeModelsGet([{"id": "vendor/current"}])
        monkeypatch.setattr(Main.requests, "get", get)

        attempts = []

        def post(url, headers=None, json=None, timeout=None):
            attempts.append(json["model"])
            if json["model"] == "vendor/retired":
                return SimpleNamespace(status_code=404, text="model not found",
                                       json=lambda: {})
            return SimpleNamespace(
                status_code=200, text="",
                json=lambda: {"choices": [{"message": {"content": "Discovered read."}}]},
            )

        monkeypatch.setattr(Main.requests, "post", post)

        result = scan()

        assert attempts == ["vendor/retired", "vendor/current"]
        assert "Discovered read." in result
        assert get.calls == 1

    def test_speech_and_moderation_models_are_never_tried(self, monkeypatch):
        """Sending a chart to Whisper is a guaranteed wasted upload."""
        models = [
            {"id": "whisper-large-v3"},
            {"id": "playai-tts"},
            {"id": "meta-llama/llama-guard-4-12b"},
            {"id": "text-embedding-3"},
            {"id": "groq/compound-mini"},
            {"id": "vendor/real-vision"},
        ]
        assert Main.discover_from(models) == ["vendor/real-vision"]

    def test_models_declaring_image_input_are_tried_first(self, monkeypatch):
        models = [
            {"id": "vendor/text-only"},
            {"id": "vendor/sees-images", "input_modalities": ["text", "image"]},
        ]

        assert Main.discover_from(models)[0] == "vendor/sees-images"

    def test_inactive_models_are_skipped(self):
        models = [{"id": "vendor/dead", "active": False}, {"id": "vendor/live"}]

        assert Main.discover_from(models) == ["vendor/live"]

    def test_the_attempt_list_is_capped(self):
        models = [{"id": f"vendor/m{i}"} for i in range(30)]

        assert len(Main.discover_from(models)) <= 3

    def test_a_failed_discovery_does_not_break_the_scan(self, monkeypatch):
        """No key, no network, a 500 — the scan still reports honestly."""
        self.setup_groq(monkeypatch)
        monkeypatch.setattr(Main, "GROQ_VISION_MODELS", ["vendor/retired"])
        monkeypatch.setattr(
            Main.requests, "get", FakeModelsGet(raises=ConnectionError("no route"))
        )
        monkeypatch.setattr(
            Main.requests, "post", FakeGroqPost(status=404, body="model not found")
        )

        result = scan()

        assert "unavailable" in result.lower()
        assert "model not found" in result

    def test_discovery_is_not_repeated_on_the_next_failing_scan(self, monkeypatch):
        self.setup_groq(monkeypatch)
        monkeypatch.setattr(Main, "GROQ_VISION_MODELS", ["vendor/retired"])
        get = FakeModelsGet([])
        monkeypatch.setattr(Main.requests, "get", get)
        monkeypatch.setattr(
            Main.requests, "post", FakeGroqPost(status=404, body="gone")
        )

        scan()
        scan()

        assert get.calls == 1

    def test_a_model_already_tried_is_not_tried_again(self, monkeypatch):
        self.setup_groq(monkeypatch)
        monkeypatch.setattr(Main, "GROQ_VISION_MODELS", ["vendor/same"])
        monkeypatch.setattr(Main.requests, "get", FakeModelsGet([{"id": "vendor/same"}]))
        post = FakeGroqPost(status=404, body="gone")
        monkeypatch.setattr(Main.requests, "post", post)

        scan()

        assert len(post.calls) == 1


class TestStripReasoning:
    def test_keeps_plain_text_untouched(self):
        assert Main.strip_reasoning("Just an answer.") == "Just an answer."

    def test_removes_a_closed_block(self):
        assert Main.strip_reasoning("<think>noise</think>Answer.") == "Answer."

    def test_removes_several_blocks(self):
        text = "<think>a</think>One. <think>b</think>Two."
        assert Main.strip_reasoning(text) == "One. Two."

    def test_matches_across_newlines(self):
        assert Main.strip_reasoning("<think>line\nline</think>Done.") == "Done."

    def test_drops_an_unterminated_block_and_all_that_follows(self):
        assert Main.strip_reasoning("Lead.\n<think>cut off mid-thou") == "Lead."

    def test_is_case_insensitive(self):
        assert Main.strip_reasoning("<THINK>x</THINK>Answer.") == "Answer."


class TestGroqChat:
    def test_chat_replies_are_stripped_too(self, monkeypatch):
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(
            Main.requests, "post", FakeGroqPost("<think>hmm</think>London is live.")
        )

        assert Main.ask_groq("what session", "") == "London is live."


class TestAnthropicHandoff:
    def test_empty_anthropic_reply_falls_through_to_groq(self, monkeypatch):
        monkeypatch.setattr(Main, "ai_client", FakeClient(response([block("text", "  ")])))
        monkeypatch.setattr(Main, "GROQ_API_KEY", "groq-key")
        monkeypatch.setattr(Main.requests, "post", FakeGroqPost("Backup got it."))

        assert "Backup got it." in scan()
