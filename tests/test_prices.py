import pytest

import Main
from Main import (
    fetch_all_prices,
    fetch_crypto_prices,
    fetch_gold_price,
    fetch_usdjpy_price,
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeGet:
    """Stands in for requests.get, routing by URL substring.

    Route values are either a FakeResponse or an Exception instance to raise.
    Records every URL so tests can assert which sources were tried.
    """

    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def __call__(self, url, headers=None, timeout=None):
        self.urls.append(url)
        for fragment, outcome in self.routes.items():
            if fragment in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"unrouted URL in test: {url}")


def yahoo(price):
    return FakeResponse({"chart": {"result": [{"meta": {"regularMarketPrice": price}}]}})


def rates(price):
    return FakeResponse({"rates": {"USD": price}})


@pytest.fixture
def fake_get(monkeypatch):
    def install(routes):
        getter = FakeGet(routes)
        monkeypatch.setattr(Main.requests, "get", getter)
        return getter

    return install


class TestFetchGoldPrice:
    def test_uses_first_yahoo_ticker_when_it_returns_a_sane_price(self, fake_get):
        getter = fake_get({"GC%3DF": yahoo(2700.5)})

        assert fetch_gold_price() == 2700.5
        # Later sources must not be consulted once one succeeds.
        assert len(getter.urls) == 1

    def test_falls_through_to_second_ticker_when_first_raises(self, fake_get):
        getter = fake_get(
            {
                "GC%3DF": ConnectionError("yahoo down"),
                "XAUUSD%3DX": yahoo(2710.0),
            }
        )

        assert fetch_gold_price() == 2710.0
        assert len(getter.urls) == 2

    def test_rejects_out_of_range_price_and_tries_next_source(self, fake_get):
        # A price outside the 2000-5000 sanity band is discarded, not returned.
        getter = fake_get(
            {
                "GC%3DF": yahoo(320.0),
                "XAUUSD%3DX": yahoo(2715.0),
            }
        )

        assert fetch_gold_price() == 2715.0
        assert len(getter.urls) == 2

    def test_does_not_query_the_gld_etf(self, fake_get):
        """Regression: GLD is a gold ETF trading in the low hundreds, so it
        could never clear the 2000-5000 band. Querying it was always a wasted
        request before the real fallbacks."""
        getter = fake_get(
            {
                "GC%3DF": ConnectionError("down"),
                "XAUUSD%3DX": ConnectionError("down"),
                "frankfurter": rates(2725.0),
            }
        )

        assert fetch_gold_price() == 2725.0
        assert not any("GLD" in u for u in getter.urls)

    def test_falls_back_to_frankfurter_when_all_yahoo_tickers_fail(self, fake_get):
        fake_get(
            {
                "query1.finance.yahoo.com": ConnectionError("yahoo down"),
                "frankfurter": rates(2730.0),
            }
        )

        assert fetch_gold_price() == 2730.0

    def test_falls_back_to_fxratesapi_as_last_resort(self, fake_get):
        fake_get(
            {
                "query1.finance.yahoo.com": ConnectionError("yahoo down"),
                "frankfurter": ConnectionError("frankfurter down"),
                "fxratesapi": rates(2740.0),
            }
        )

        assert fetch_gold_price() == 2740.0

    def test_returns_none_when_every_source_fails(self, fake_get):
        fake_get(
            {
                "query1.finance.yahoo.com": ConnectionError("down"),
                "frankfurter": ConnectionError("down"),
                "fxratesapi": ConnectionError("down"),
            }
        )

        assert fetch_gold_price() is None

    def test_returns_none_when_every_source_is_out_of_range(self, fake_get):
        fake_get(
            {
                "query1.finance.yahoo.com": yahoo(320.0),
                "frankfurter": rates(9999.0),
                "fxratesapi": rates(1.0),
            }
        )

        assert fetch_gold_price() is None

    def test_malformed_payload_is_treated_as_a_failed_source(self, fake_get):
        fake_get(
            {
                "query1.finance.yahoo.com": FakeResponse({"chart": {"result": []}}),
                "frankfurter": rates(2750.0),
            }
        )

        assert fetch_gold_price() == 2750.0


class TestFetchUsdJpyPrice:
    def test_returns_rate_on_success(self, fake_get):
        fake_get(
            {
                "exchangerate-api.com": FakeResponse(
                    {"result": "success", "conversion_rates": {"JPY": 150.25}}
                )
            }
        )

        assert fetch_usdjpy_price() == 150.25

    def test_returns_none_when_api_reports_failure(self, fake_get):
        fake_get(
            {
                "exchangerate-api.com": FakeResponse(
                    {"result": "error", "conversion_rates": {"JPY": 150.25}}
                )
            }
        )

        assert fetch_usdjpy_price() is None

    def test_returns_none_when_jpy_rate_is_missing(self, fake_get):
        fake_get(
            {
                "exchangerate-api.com": FakeResponse(
                    {"result": "success", "conversion_rates": {}}
                )
            }
        )

        assert fetch_usdjpy_price() is None

    def test_returns_none_on_network_error(self, fake_get):
        fake_get({"exchangerate-api.com": ConnectionError("ER down")})

        assert fetch_usdjpy_price() is None


class TestFetchCryptoPrices:
    def test_maps_coingecko_ids_to_pairs(self, fake_get):
        fake_get(
            {
                "coingecko": FakeResponse(
                    {
                        "ethereum": {"usd": 3000.0},
                        "solana": {"usd": 150.0},
                        "bitcoin": {"usd": 95000.0},
                    }
                )
            }
        )

        assert fetch_crypto_prices() == {
            "ETH/USD": 3000.0,
            "SOL/USD": 150.0,
            "BTC/USD": 95000.0,
        }

    def test_missing_coin_becomes_none_without_losing_the_others(self, fake_get):
        fake_get(
            {"coingecko": FakeResponse({"ethereum": {"usd": 3000.0}, "bitcoin": {}})}
        )

        prices = fetch_crypto_prices()
        assert prices["ETH/USD"] == 3000.0
        assert prices["SOL/USD"] is None
        assert prices["BTC/USD"] is None

    def test_network_error_yields_all_none(self, fake_get):
        fake_get({"coingecko": ConnectionError("coingecko down")})

        assert fetch_crypto_prices() == {
            "ETH/USD": None,
            "SOL/USD": None,
            "BTC/USD": None,
        }


class TestFetchAllPrices:
    def test_composes_every_tracked_symbol(self, monkeypatch):
        monkeypatch.setattr(Main, "fetch_gold_price", lambda: 2700.0)
        monkeypatch.setattr(Main, "fetch_usdjpy_price", lambda: 150.0)
        monkeypatch.setattr(
            Main,
            "fetch_crypto_prices",
            lambda: {"ETH/USD": 3000.0, "SOL/USD": 150.0, "BTC/USD": 95000.0},
        )

        assert fetch_all_prices() == {
            "XAU/USD": 2700.0,
            "ETH/USD": 3000.0,
            "USD/JPY": 150.0,
            "SOL/USD": 150.0,
            "BTC/USD": 95000.0,
        }

    def test_covers_exactly_the_scanned_symbols(self, monkeypatch):
        monkeypatch.setattr(Main, "fetch_gold_price", lambda: None)
        monkeypatch.setattr(Main, "fetch_usdjpy_price", lambda: None)
        monkeypatch.setattr(
            Main,
            "fetch_crypto_prices",
            lambda: {"ETH/USD": None, "SOL/USD": None, "BTC/USD": None},
        )

        assert set(fetch_all_prices()) == set(Main.SYMBOLS)

    def test_crypto_outage_does_not_suppress_gold_or_jpy(self, monkeypatch):
        monkeypatch.setattr(Main, "fetch_gold_price", lambda: 2700.0)
        monkeypatch.setattr(Main, "fetch_usdjpy_price", lambda: 150.0)
        monkeypatch.setattr(
            Main,
            "fetch_crypto_prices",
            lambda: {"ETH/USD": None, "SOL/USD": None, "BTC/USD": None},
        )

        prices = fetch_all_prices()
        assert prices["XAU/USD"] == 2700.0
        assert prices["USD/JPY"] == 150.0
        assert prices["BTC/USD"] is None
