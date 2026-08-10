from types import SimpleNamespace

import pytest

import Main
from Main import BOSS_NAMES, build_greeting, get_boss_name, get_name


class FrozenDatetime:
    """Stands in for Main.datetime so the UTC hour is controllable."""

    hour = 8

    @classmethod
    def now(cls, tz=None):
        return SimpleNamespace(hour=cls.hour)


@pytest.fixture
def at_hour(monkeypatch):
    monkeypatch.setattr(Main, "datetime", FrozenDatetime)

    def set_hour(hour):
        FrozenDatetime.hour = hour

    return set_hour


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(Main, "safe_send", messages.append)
    return messages


class TestBossNames:
    def test_names_are_unique(self):
        assert len(BOSS_NAMES) == len(set(BOSS_NAMES))

    def test_get_boss_name_returns_a_known_name(self):
        assert get_boss_name() in BOSS_NAMES

    def test_get_name_returns_a_known_name(self, monkeypatch):
        monkeypatch.setattr(Main, "_last_name_used", "")
        assert get_name() in BOSS_NAMES

    def test_get_name_never_repeats_consecutively(self, monkeypatch):
        monkeypatch.setattr(Main, "_last_name_used", "")
        picks = [get_name() for _ in range(60)]
        assert all(a != b for a, b in zip(picks, picks[1:]))

    def test_get_name_can_still_reach_every_name(self, monkeypatch):
        monkeypatch.setattr(Main, "_last_name_used", "")
        assert set(get_name() for _ in range(400)) == set(BOSS_NAMES)


class TestBuildGreeting:
    @pytest.mark.parametrize(
        "hour,marker",
        [
            (8, "🌅 Morning"),
            (14, "☀️ Afternoon"),
            (19, "🌆 Evening"),
            (2, "🌙 Late night"),
        ],
    )
    def test_greeting_matches_the_period(self, hour, marker):
        assert marker in build_greeting(hour)

    def test_includes_the_session_and_a_quote(self):
        msg = build_greeting(2)
        assert "Session: ASIAN" in msg
        assert any(q in msg for q in Main.MOTIVATIONAL_QUOTES)

    def test_addresses_the_boss_by_a_known_name(self):
        msg = build_greeting(8)
        assert any(name in msg for name in BOSS_NAMES)


class TestCheckAndSendGreeting:
    def test_sends_once_for_a_new_period(self, at_hour, sent):
        at_hour(8)

        Main.check_and_send_greeting()

        assert len(sent) == 1
        assert "🌅 Morning" in sent[0]
        assert Main.last_greeted_period == "morning"

    def test_does_not_repeat_within_the_same_period(self, at_hour, sent):
        at_hour(8)

        Main.check_and_send_greeting()
        Main.check_and_send_greeting()

        assert len(sent) == 1

    def test_sends_again_when_the_period_changes(self, at_hour, sent):
        at_hour(8)
        Main.check_and_send_greeting()
        at_hour(14)
        Main.check_and_send_greeting()

        assert len(sent) == 2
        assert "☀️ Afternoon" in sent[1]

    def test_greets_again_the_next_day(self, at_hour, sent):
        """Regression test: greetings used to stop after the first full day.

        The old reset was guarded by `len(greeted_periods) > 4` and only four
        periods exist, so it never fired.
        """
        for hour in (8, 14, 19, 2):  # morning, afternoon, evening, night
            at_hour(hour)
            Main.check_and_send_greeting()

        assert len(sent) == 4

        # Next day: the cycle starts over.
        at_hour(8)
        Main.check_and_send_greeting()

        assert len(sent) == 5
        assert "🌅 Morning" in sent[4]

    def test_keeps_cycling_across_several_days(self, at_hour, sent):
        for _ in range(3):
            for hour in (8, 14, 19, 2):
                at_hour(hour)
                Main.check_and_send_greeting()

        assert len(sent) == 12

    def test_polling_repeatedly_within_a_period_sends_nothing_extra(
        self, at_hour, sent
    ):
        """The scanner calls this every 60s, so only period changes may fire."""
        at_hour(8)
        for _ in range(10):
            Main.check_and_send_greeting()

        at_hour(9)  # still morning
        for _ in range(10):
            Main.check_and_send_greeting()

        assert len(sent) == 1
