import time

import pytest

import Main
from Main import SIGNAL_COOLDOWN_SECS, cooldown_ok, detect_crt_signal, pip_size


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("USD/JPY", 0.01),
        ("XAU/USD", 1.0),
        ("ETH/USD", 1.0),
        ("SOL/USD", 0.10),
        ("BTC/USD", 10.0),
        ("EUR/USD", 0.0001),  # unknown symbol falls back to default pip size
        ("GBP/JPY", 0.01),    # unknown symbol, but still a JPY pair
    ],
)
def test_pip_size(symbol, expected):
    assert pip_size(symbol) == expected


def test_cooldown_ok_true_when_no_prior_signal():
    assert cooldown_ok("XAU/USD") is True


def test_cooldown_ok_false_within_window():
    Main.last_signal_time["XAU/USD"] = time.time()
    assert cooldown_ok("XAU/USD") is False


def test_cooldown_ok_true_after_window_elapsed():
    Main.last_signal_time["XAU/USD"] = time.time() - SIGNAL_COOLDOWN_SECS - 1
    assert cooldown_ok("XAU/USD") is True


def _seed_history(symbol, lookback_value, recent_values):
    """20 candles total: 15 flat lookback candles + 5 recent candles,
    mirroring how cmd_signal/scanner_loop append the live price before
    calling detect_crt_signal."""
    Main.price_history[symbol] = [lookback_value] * 15 + recent_values


class TestDetectCrtSignal:
    def test_no_signal_outside_allowed_session(self):
        _seed_history("XAU/USD", 2700, [2700, 2700, 2680, 2700, 2705])
        assert detect_crt_signal("XAU/USD", 2705, "DEAD") is None

    def test_no_signal_with_insufficient_history(self):
        Main.price_history["XAU/USD"] = [2700] * 10
        assert detect_crt_signal("XAU/USD", 2705, "ASIAN") is None

    def test_no_signal_when_sweeps_insufficient(self):
        # High sweep fails the price<prev_high check; low sweep is only
        # 5 pips, below XAU/USD's 15 pip minimum.
        _seed_history("XAU/USD", 2700, [2700, 2700, 2695, 2700, 2705])
        assert detect_crt_signal("XAU/USD", 2705, "ASIAN") is None

    def test_buy_signal_on_low_sweep(self):
        _seed_history("XAU/USD", 2700, [2700, 2700, 2680, 2700, 2705])
        result = detect_crt_signal("XAU/USD", 2705, "ASIAN")
        assert result is not None
        assert "🟢 BUY SIGNAL" in result
        assert "Entry: 2705.0000" in result
        assert "SL: 2630.0000" in result
        assert "TP: 2855.0000" in result
        assert Main.last_signal_dir["XAU/USD"] == "BUY"

    def test_sell_signal_on_high_sweep(self):
        _seed_history("XAU/USD", 2700, [2700, 2700, 2720, 2700, 2695])
        result = detect_crt_signal("XAU/USD", 2695, "ASIAN")
        assert result is not None
        assert "🔴 SELL SIGNAL" in result
        assert "Entry: 2695.0000" in result
        assert "SL: 2770.0000" in result
        assert "TP: 2545.0000" in result
        assert Main.last_signal_dir["XAU/USD"] == "SELL"

    def test_london_session_appends_warning(self):
        _seed_history("XAU/USD", 2700, [2700, 2700, 2680, 2700, 2705])
        result = detect_crt_signal("XAU/USD", 2705, "LONDON")
        assert result is not None
        assert "LONDON SESSION" in result

    def test_cooldown_blocks_repeat_signal(self):
        _seed_history("XAU/USD", 2700, [2700, 2700, 2680, 2700, 2705])
        first = detect_crt_signal("XAU/USD", 2705, "ASIAN")
        assert first is not None

        second = detect_crt_signal("XAU/USD", 2705, "ASIAN")
        assert second is None
