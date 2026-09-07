"""Decision memory.

The point of this module is that Jarvis stops repeating itself, so the tests
are mostly about two things: an unfinished trade must never teach anything,
and a storage failure must never cost a signal.
"""

import json
import time
from types import SimpleNamespace

import pytest

import memory


@pytest.fixture(autouse=True)
def isolated_log(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_PATH", str(tmp_path / "mem.jsonl"))
    monkeypatch.setattr(memory, "PENDING_TTL_SECS", 24 * 3600)


def signal(symbol="XAU/USD", direction="BUY", entry=2700.0, sl=2690.0,
           tp=2730.0, session="ASIAN"):
    return SimpleNamespace(
        symbol=symbol, direction=direction, entry=entry, sl=sl, tp=tp,
        session=session, text="whatever",
    )


class TestRecording:
    def test_a_fired_signal_is_stored_as_pending(self):
        memory.record_signal(signal())

        entries = memory.load()
        assert len(entries) == 1
        assert entries[0].outcome == "pending"
        assert not entries[0].resolved

    def test_the_levels_are_kept_verbatim(self):
        memory.record_signal(signal(entry=2701.5, sl=2689.25, tp=2733.75))

        e = memory.load()[0]
        assert (e.entry, e.sl, e.tp) == (2701.5, 2689.25, 2733.75)

    def test_ids_are_unique_per_signal(self):
        memory.record_signal(signal(), now=1000)
        memory.record_signal(signal(symbol="BTC/USD"), now=1001)

        assert len({e.id for e in memory.load()}) == 2

    def test_a_storage_failure_does_not_raise(self, monkeypatch):
        """The signal still goes out even if the log cannot be written."""
        monkeypatch.setattr(memory, "MEMORY_PATH", "/nonexistent-dir/x/mem.jsonl")
        monkeypatch.setattr(memory.os, "makedirs", lambda *a, **k: None)

        assert memory.record_signal(signal()) == ""

    def test_a_corrupt_line_does_not_hide_the_rest(self):
        memory.record_signal(signal())
        with open(memory.MEMORY_PATH, "a") as f:
            f.write("{not json\n")
        memory.record_signal(signal(symbol="ETH/USD"))

        assert len(memory.load()) == 2


class TestResolution:
    def test_a_buy_reaching_tp_is_a_win(self):
        memory.record_signal(signal(direction="BUY", tp=2730.0))

        done = memory.resolve("XAU/USD", 2731.0)

        assert [e.outcome for e in done] == ["TP"]

    def test_a_buy_reaching_sl_is_a_loss(self):
        memory.record_signal(signal(direction="BUY", sl=2690.0))

        assert memory.resolve("XAU/USD", 2689.0)[0].outcome == "SL"

    def test_a_sell_is_judged_the_other_way_up(self):
        memory.record_signal(signal(direction="SELL", entry=2700, sl=2710, tp=2670))

        assert memory.resolve("XAU/USD", 2669.0)[0].outcome == "TP"
        memory.record_signal(signal(direction="SELL", entry=2700, sl=2710, tp=2670))
        assert memory.resolve("XAU/USD", 2711.0)[0].outcome == "SL"

    def test_price_between_the_levels_resolves_nothing(self):
        memory.record_signal(signal())

        assert memory.resolve("XAU/USD", 2705.0) == []
        assert memory.load()[0].outcome == "pending"

    def test_another_symbols_price_cannot_resolve_it(self):
        memory.record_signal(signal(symbol="XAU/USD"))

        assert memory.resolve("BTC/USD", 2731.0) == []

    def test_a_resolved_entry_is_not_resolved_twice(self):
        memory.record_signal(signal())
        memory.resolve("XAU/USD", 2731.0)

        assert memory.resolve("XAU/USD", 2731.0) == []

    def test_the_exit_price_and_time_are_recorded(self):
        memory.record_signal(signal())

        memory.resolve("XAU/USD", 2731.0, now=5000)

        e = memory.load()[0]
        assert e.exit_price == 2731.0
        assert e.resolved_at == 5000

    def test_a_stale_pending_entry_expires(self):
        """Otherwise they pile up and the win rate only counts trades that
        happened to resolve."""
        memory.record_signal(signal(), now=0)

        memory.resolve("XAU/USD", 2705.0, now=25 * 3600)

        assert memory.load()[0].outcome == "EXPIRED"

    def test_expiry_applies_across_symbols(self):
        memory.record_signal(signal(symbol="ETH/USD"), now=0)

        memory.resolve("XAU/USD", 1.0, now=25 * 3600)

        assert memory.load()[0].outcome == "EXPIRED"

    def test_a_fresh_entry_is_left_alone(self):
        memory.record_signal(signal(), now=0)

        memory.resolve("XAU/USD", 2705.0, now=60)

        assert memory.load()[0].outcome == "pending"


class TestRMultiple:
    def test_a_win_is_positive(self):
        memory.record_signal(signal(entry=2700, sl=2690, tp=2730))
        done = memory.resolve("XAU/USD", 2730.0)

        assert done[0].r_multiple == pytest.approx(3.0)

    def test_a_loss_is_minus_one(self):
        memory.record_signal(signal(entry=2700, sl=2690, tp=2730))
        done = memory.resolve("XAU/USD", 2690.0)

        assert done[0].r_multiple == pytest.approx(-1.0)

    def test_a_short_win_is_also_positive(self):
        memory.record_signal(signal(direction="SELL", entry=2700, sl=2710, tp=2670))
        done = memory.resolve("XAU/USD", 2670.0)

        assert done[0].r_multiple == pytest.approx(3.0)

    def test_a_zero_risk_entry_does_not_divide_by_zero(self):
        memory.record_signal(signal(entry=2700, sl=2700, tp=2730))
        done = memory.resolve("XAU/USD", 2730.0)

        assert done[0].r_multiple == 0.0


class TestLessons:
    def resolve_one(self, **kw):
        memory.record_signal(signal(**kw))
        return memory.resolve(kw.get("symbol", "XAU/USD"), 2731.0)[0]

    def test_a_lesson_is_saved_against_its_trade(self):
        e = self.resolve_one()

        assert memory.save_lesson(e.id, "Waited for the sweep. Do that again.")
        assert memory.load()[0].lesson.startswith("Waited for the sweep")

    def test_an_unknown_id_saves_nothing(self):
        self.resolve_one()

        assert memory.save_lesson("no-such-id", "x") is False

    def test_only_finished_trades_teach(self):
        """The whole point. A running trade has taught nothing yet."""
        memory.record_signal(signal())
        pending = memory.load()[0]
        memory.save_lesson(pending.id, "premature wisdom")

        assert memory.recent_lessons() == []

    def test_a_finished_trade_with_no_lesson_is_skipped(self):
        self.resolve_one()

        assert memory.recent_lessons() == []

    def test_lessons_come_back_newest_first(self):
        for i, sym in enumerate(["XAU/USD", "ETH/USD"]):
            memory.record_signal(signal(symbol=sym), now=1000 + i)
            done = memory.resolve(sym, 2731.0, now=2000 + i)[0]
            memory.save_lesson(done.id, f"lesson {i}")

        assert memory.recent_lessons()[0].lesson == "lesson 1"

    def test_lessons_can_be_filtered_to_one_symbol(self):
        for sym in ("XAU/USD", "ETH/USD"):
            memory.record_signal(signal(symbol=sym))
            done = memory.resolve(sym, 2731.0)[0]
            memory.save_lesson(done.id, f"{sym} lesson")

        got = memory.recent_lessons("ETH/USD")
        assert len(got) == 1 and got[0].symbol == "ETH/USD"

    def test_the_number_of_lessons_is_capped(self):
        for i in range(10):
            memory.record_signal(signal(), now=1000 + i)
            done = memory.resolve("XAU/USD", 2731.0, now=2000 + i)[0]
            memory.save_lesson(done.id, f"lesson {i}")

        assert len(memory.recent_lessons(limit=3)) == 3


class TestPromptInjection:
    def test_no_lessons_injects_nothing(self):
        """An empty section would just be noise in the prompt."""
        assert memory.lessons_prompt() == ""

    def test_lessons_are_labelled_with_their_outcome(self):
        memory.record_signal(signal())
        done = memory.resolve("XAU/USD", 2731.0)[0]
        memory.save_lesson(done.id, "Sweep was clean.")

        text = memory.lessons_prompt()

        assert "XAU/USD BUY → TP" in text
        assert "Sweep was clean." in text

    def test_the_block_says_these_are_finished_trades(self):
        memory.record_signal(signal())
        done = memory.resolve("XAU/USD", 2731.0)[0]
        memory.save_lesson(done.id, "x")

        assert "finished trades" in memory.lessons_prompt()


class TestStats:
    def test_counts_wins_and_losses(self):
        memory.record_signal(signal(), now=1)
        memory.resolve("XAU/USD", 2731.0)
        memory.record_signal(signal(), now=2)
        memory.resolve("XAU/USD", 2689.0)

        s = memory.stats()
        assert (s["wins"], s["losses"], s["resolved"]) == (1, 1, 2)
        assert s["win_rate"] == pytest.approx(50.0)

    def test_expired_trades_are_left_out_of_the_record(self):
        """An expired signal is not a loss — it never resolved either way."""
        memory.record_signal(signal(), now=0)
        memory.resolve("XAU/USD", 2705.0, now=25 * 3600)

        assert memory.stats()["resolved"] == 0

    def test_pending_trades_are_counted_separately(self):
        memory.record_signal(signal())

        s = memory.stats()
        assert s["pending"] == 1 and s["resolved"] == 0

    def test_an_empty_log_does_not_divide_by_zero(self):
        assert memory.stats()["win_rate"] == 0.0

    def test_total_r_adds_up(self):
        memory.record_signal(signal(entry=2700, sl=2690, tp=2730), now=1)
        memory.resolve("XAU/USD", 2730.0)
        memory.record_signal(signal(entry=2700, sl=2690, tp=2730), now=2)
        memory.resolve("XAU/USD", 2690.0)

        assert memory.stats()["total_r"] == pytest.approx(2.0)

    def test_stats_can_be_filtered_to_one_symbol(self):
        memory.record_signal(signal(symbol="XAU/USD"))
        memory.resolve("XAU/USD", 2731.0)
        memory.record_signal(signal(symbol="ETH/USD"))
        memory.resolve("ETH/USD", 2731.0)

        assert memory.stats("ETH/USD")["resolved"] == 1


class TestDurability:
    def test_the_log_survives_a_reload(self):
        memory.record_signal(signal())
        memory.resolve("XAU/USD", 2731.0)

        assert memory.load()[0].outcome == "TP"

    def test_rewrites_are_atomic(self, tmp_path):
        """A crash mid-write must not truncate the log."""
        memory.record_signal(signal())
        memory.resolve("XAU/USD", 2731.0)

        assert not list(tmp_path.glob("*.tmp"))
        lines = open(memory.MEMORY_PATH).read().strip().split("\n")
        assert len(lines) == 1
        json.loads(lines[0])


class TestJarvisWiring:
    """The scanner-side glue: reviewing a closed trade, and /memory."""

    def resolved_entry(self):
        memory.record_signal(signal())
        return memory.resolve("XAU/USD", 2731.0)[0]

    def test_a_closed_trade_is_reviewed_and_announced(self, monkeypatch):
        import Main

        sent = []
        monkeypatch.setattr(Main, "safe_send", lambda t: sent.append(t))
        monkeypatch.setattr(Main, "ask_jarvis", lambda *a, **k: "Sweep was clean.")
        monkeypatch.setattr(Main, "memory", memory)

        lesson = Main.review_resolved_trade(self.resolved_entry())

        assert lesson == "Sweep was clean."
        assert "✅" in sent[0]
        assert "Sweep was clean." in sent[0]
        assert memory.load()[0].lesson == "Sweep was clean."

    def test_the_review_does_not_feed_itself_past_lessons(self, monkeypatch):
        """A trade must be judged on its own outcome, not on earlier ones."""
        import Main

        seen = {}

        def fake(msg, prices=None, include_memory=True):
            seen["include_memory"] = include_memory
            return "x"

        monkeypatch.setattr(Main, "safe_send", lambda t: None)
        monkeypatch.setattr(Main, "ask_jarvis", fake)
        monkeypatch.setattr(Main, "memory", memory)

        Main.review_resolved_trade(self.resolved_entry())

        assert seen["include_memory"] is False

    def test_a_dead_llm_still_records_the_outcome(self, monkeypatch):
        import Main

        sent = []
        monkeypatch.setattr(Main, "safe_send", lambda t: sent.append(t))
        monkeypatch.setattr(
            Main, "ask_jarvis",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("no ai")),
        )
        monkeypatch.setattr(Main, "memory", memory)

        assert Main.review_resolved_trade(self.resolved_entry()) == ""
        assert "Closed:" in sent[0]
        assert memory.load()[0].outcome == "TP"

    def test_a_loss_is_marked_as_one(self, monkeypatch):
        import Main

        sent = []
        monkeypatch.setattr(Main, "safe_send", lambda t: sent.append(t))
        monkeypatch.setattr(Main, "ask_jarvis", lambda *a, **k: "Chased it.")
        monkeypatch.setattr(Main, "memory", memory)

        memory.record_signal(signal())
        Main.review_resolved_trade(memory.resolve("XAU/USD", 2689.0)[0])

        assert "❌" in sent[0]


class TestMemoryCommand:
    def run(self, monkeypatch, args=None):
        import asyncio

        import Main

        sent = []

        class Msg:
            async def reply_text(self, text):
                sent.append(text)

        monkeypatch.setattr(Main, "memory", memory)
        asyncio.run(
            Main.cmd_memory(
                SimpleNamespace(message=Msg()), SimpleNamespace(args=args or [])
            )
        )
        return sent[0]

    def test_an_empty_log_says_so_plainly(self, monkeypatch):
        assert "No calls recorded yet" in self.run(monkeypatch)

    def test_shows_the_record_and_the_lessons(self, monkeypatch):
        memory.record_signal(signal(), now=1)
        done = memory.resolve("XAU/USD", 2731.0)[0]
        memory.save_lesson(done.id, "Sweep was clean.")

        text = self.run(monkeypatch)

        assert "✅ 1" in text
        assert "100%" in text
        assert "Sweep was clean." in text

    def test_an_open_trade_shows_as_open_not_as_a_result(self, monkeypatch):
        memory.record_signal(signal())

        text = self.run(monkeypatch)

        assert "Still open: 1" in text
        assert "Resolved: 0" in text

    def test_a_symbol_argument_narrows_it(self, monkeypatch):
        memory.record_signal(signal(symbol="ETH/USD"))
        memory.resolve("ETH/USD", 2731.0)

        assert "ETH/USD" in self.run(monkeypatch, ["ethusd"])

    def test_an_unknown_symbol_is_rejected_with_the_valid_ones(self, monkeypatch):
        text = self.run(monkeypatch, ["DOGE"])

        assert "Unknown symbol" in text
        assert "XAU/USD" in text

    def test_the_reply_fits_telegram(self, monkeypatch):
        for i in range(40):
            memory.record_signal(signal(), now=1000 + i)
            done = memory.resolve("XAU/USD", 2731.0, now=2000 + i)[0]
            memory.save_lesson(done.id, "a long lesson " * 30)

        assert len(self.run(monkeypatch)) <= 4000
