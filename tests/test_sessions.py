import pytest

from Main import get_greeting_period, get_session, is_signal_allowed

SESSION_CASES = [
    (0, "SYDNEY"), (1, "SYDNEY"),
    (2, "ASIAN"), (3, "ASIAN"), (4, "ASIAN"), (5, "ASIAN"),
    (6, "ASIAN_END"),
    (7, "LONDON"), (8, "LONDON"), (12, "LONDON"),
    (13, "NEW_YORK"), (14, "NEW_YORK"), (17, "NEW_YORK"),
    (18, "DEAD"), (19, "DEAD"), (20, "DEAD"), (21, "DEAD"),
    (22, "SYDNEY"), (23, "SYDNEY"),
]


@pytest.mark.parametrize("hour,expected", SESSION_CASES)
def test_get_session_boundaries(hour, expected):
    assert get_session(hour) == expected


@pytest.mark.parametrize(
    "session,expected",
    [
        ("ASIAN", True),
        ("LONDON", True),
        ("NEW_YORK", True),
        ("ASIAN_END", False),
        ("SYDNEY", False),
        ("DEAD", False),
    ],
)
def test_is_signal_allowed(session, expected):
    assert is_signal_allowed(session) is expected


GREETING_CASES = [
    (0, "night"), (4, "night"),
    (5, "morning"), (11, "morning"),
    (12, "afternoon"), (16, "afternoon"),
    (17, "evening"), (20, "evening"),
    (21, "night"), (23, "night"),
]


@pytest.mark.parametrize("hour,expected", GREETING_CASES)
def test_get_greeting_period_boundaries(hour, expected):
    assert get_greeting_period(hour) == expected
