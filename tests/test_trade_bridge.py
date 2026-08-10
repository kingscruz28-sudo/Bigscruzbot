import pytest

import Main
from Main import TradeSignal, build_bridge_payload, validate_trade_levels


def make_signal(**overrides):
    """A well-formed BUY on Gold; override fields to build the bad cases."""
    defaults = dict(
        symbol="XAU/USD",
        direction="BUY",
        entry=2705.0,
        sl=2630.0,
        tp=2855.0,
        session="ASIAN",
        text="🟢 BUY SIGNAL — Gold [ASIAN]",
    )
    defaults.update(overrides)
    return TradeSignal(**defaults)


SELL_KWARGS = dict(direction="SELL", entry=2695.0, sl=2770.0, tp=2545.0)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class RecordingPost:
    """Stands in for requests.post and records how it was called."""

    def __init__(self, response=None, raises=None):
        self.response = response or FakeResponse()
        self.raises = raises
        self.calls = []

    def __call__(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.raises:
            raise self.raises
        return self.response


@pytest.fixture
def sent(monkeypatch):
    """Capture everything the bot would push to Telegram."""
    messages = []
    monkeypatch.setattr(Main, "safe_send", messages.append)
    return messages


@pytest.fixture
def bridge_enabled(monkeypatch):
    """Auto-trading on, with deterministic lot/risk regardless of env vars."""
    monkeypatch.setattr(Main, "AUTO_TRADE", True)
    monkeypatch.setattr(Main, "MT5_BRIDGE_URL", "http://bridge.test/trade")
    monkeypatch.setattr(Main, "MAX_LOT", 0.02)
    monkeypatch.setattr(Main, "RISK_PERCENT", 1.5)


class TestBuildBridgePayload:
    def test_maps_symbol_to_mt5_name(self, bridge_enabled):
        payload = build_bridge_payload(make_signal())
        assert payload == {
            "symbol": "XAUUSD",
            "direction": "BUY",
            "entry": 2705.0,
            "sl": 2630.0,
            "tp": 2855.0,
            "lot": 0.02,
            "risk_percent": 1.5,
        }

    def test_unmapped_symbol_falls_back_to_stripped_slash(self, bridge_enabled):
        payload = build_bridge_payload(make_signal(symbol="EUR/USD"))
        assert payload["symbol"] == "EURUSD"

    @pytest.mark.parametrize(
        "symbol,expected",
        [
            ("XAU/USD", "XAUUSD"),
            ("BTC/USD", "BTCUSD"),
            ("ETH/USD", "ETHUSD"),
            ("SOL/USD", "SOLUSD"),
            ("USD/JPY", "USDJPY"),
        ],
    )
    def test_every_traded_symbol_has_an_mt5_name(self, symbol, expected):
        assert build_bridge_payload(make_signal(symbol=symbol))["symbol"] == expected

    def test_symbol_map_covers_every_scanned_symbol(self):
        """A new symbol without an MT5 mapping would silently fall back to a
        stripped name, which XM may not recognise."""
        missing = [s for s in Main.SYMBOLS if s not in Main.MT5_SYMBOL_MAP]
        assert missing == []

    def test_prices_are_sent_at_full_precision(self, bridge_enabled):
        """Levels used to round-trip through a '.4f' string on the way to the
        bridge; they are now passed through untouched."""
        payload = build_bridge_payload(make_signal(entry=2705.123456789))
        assert payload["entry"] == 2705.123456789


class TestValidateTradeLevels:
    def test_accepts_well_formed_buy(self):
        assert validate_trade_levels(make_signal()) is None

    def test_accepts_well_formed_sell(self):
        assert validate_trade_levels(make_signal(**SELL_KWARGS)) is None

    def test_rejects_buy_with_sl_above_entry(self):
        reason = validate_trade_levels(make_signal(sl=2800.0))
        assert reason is not None
        assert "SL" in reason

    def test_rejects_buy_with_tp_below_entry(self):
        reason = validate_trade_levels(make_signal(tp=2600.0))
        assert reason is not None
        assert "TP" in reason

    def test_rejects_sell_with_sl_below_entry(self):
        reason = validate_trade_levels(make_signal(**{**SELL_KWARGS, "sl": 2600.0}))
        assert reason is not None
        assert "SL" in reason

    def test_rejects_sell_with_tp_above_entry(self):
        reason = validate_trade_levels(make_signal(**{**SELL_KWARGS, "tp": 2800.0}))
        assert reason is not None
        assert "TP" in reason

    @pytest.mark.parametrize("field", ["entry", "sl", "tp"])
    def test_rejects_non_positive_prices(self, field):
        reason = validate_trade_levels(make_signal(**{field: 0.0}))
        assert reason is not None
        assert "positive price" in reason

    def test_rejects_none_price(self):
        reason = validate_trade_levels(make_signal(tp=None))
        assert reason is not None
        assert "positive price" in reason

    def test_rejects_unknown_direction(self):
        reason = validate_trade_levels(make_signal(direction="HOLD"))
        assert reason is not None
        assert "unknown direction" in reason


class TestExecuteTradeViaBridge:
    def test_no_post_when_auto_trade_disabled(self, monkeypatch, sent):
        post = RecordingPost()
        monkeypatch.setattr(Main, "AUTO_TRADE", False)
        monkeypatch.setattr(Main, "MT5_BRIDGE_URL", "http://bridge.test/trade")
        monkeypatch.setattr(Main.requests, "post", post)

        Main.execute_trade_via_bridge(make_signal())

        assert post.calls == []
        assert sent == []

    def test_no_post_when_bridge_url_unset(self, monkeypatch, sent):
        post = RecordingPost()
        monkeypatch.setattr(Main, "AUTO_TRADE", True)
        monkeypatch.setattr(Main, "MT5_BRIDGE_URL", "")
        monkeypatch.setattr(Main.requests, "post", post)

        Main.execute_trade_via_bridge(make_signal())

        assert post.calls == []
        assert sent == []

    def test_posts_payload_to_bridge(self, monkeypatch, sent, bridge_enabled):
        post = RecordingPost(FakeResponse(200, {"lot": 0.05}))
        monkeypatch.setattr(Main.requests, "post", post)

        Main.execute_trade_via_bridge(make_signal())

        assert len(post.calls) == 1
        call = post.calls[0]
        assert call["url"] == "http://bridge.test/trade"
        assert call["timeout"] == 10
        assert call["json"]["direction"] == "BUY"
        assert call["json"]["entry"] == 2705.0
        assert call["json"]["sl"] == 2630.0
        assert call["json"]["tp"] == 2855.0

    def test_confirms_execution_using_lot_returned_by_bridge(
        self, monkeypatch, sent, bridge_enabled
    ):
        monkeypatch.setattr(Main.requests, "post", RecordingPost(FakeResponse(200, {"lot": 0.05})))

        Main.execute_trade_via_bridge(make_signal())

        assert len(sent) == 1
        assert "✅ EXECUTED BUY Gold" in sent[0]
        assert "Lot: 0.05" in sent[0]

    def test_falls_back_to_max_lot_when_bridge_omits_it(
        self, monkeypatch, sent, bridge_enabled
    ):
        monkeypatch.setattr(Main.requests, "post", RecordingPost(FakeResponse(200, {})))

        Main.execute_trade_via_bridge(make_signal())

        assert "Lot: 0.02" in sent[0]

    def test_reports_non_200_from_bridge(self, monkeypatch, sent, bridge_enabled):
        monkeypatch.setattr(
            Main.requests,
            "post",
            RecordingPost(FakeResponse(500, text="MT5 terminal not connected")),
        )

        Main.execute_trade_via_bridge(make_signal())

        assert len(sent) == 1
        assert "❌ Bridge error" in sent[0]
        assert "MT5 terminal not connected" in sent[0]

    def test_network_failure_is_reported_not_raised(
        self, monkeypatch, sent, bridge_enabled
    ):
        monkeypatch.setattr(
            Main.requests,
            "post",
            RecordingPost(raises=ConnectionError("bridge unreachable")),
        )

        Main.execute_trade_via_bridge(make_signal())

        assert len(sent) == 1
        assert "⚠️ Auto-trade error" in sent[0]

    def test_invalid_levels_are_never_sent_to_the_bridge(
        self, monkeypatch, sent, bridge_enabled
    ):
        """The whole point of the validation: a bad SL must not reach MT5."""
        post = RecordingPost()
        monkeypatch.setattr(Main.requests, "post", post)

        Main.execute_trade_via_bridge(make_signal(sl=2800.0))

        assert post.calls == []
        assert len(sent) == 1
        assert "Auto-trade skipped" in sent[0]
