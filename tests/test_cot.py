"""Commitment of Traders feed.

The CFTC publishes positioning weekly, free and without a key. Everything
here is shaped around two facts about that feed: Socrata returns every number
as a *string*, and it omits columns that are empty rather than sending null.
Both will crash naive parsing on a live row.
"""

import asyncio
from types import SimpleNamespace

import pytest

import Main
from Main import fetch_cot, summarise_cot


def row(date="2026-08-04", spec_long=200_000, spec_short=50_000,
        comm_long=80_000, comm_short=260_000, oi=500_000,
        name="GOLD - COMMODITY EXCHANGE INC."):
    """A COT row as Socrata sends it — numbers as strings."""
    return {
        "report_date_as_yyyy_mm_dd": f"{date}T00:00:00.000",
        "market_and_exchange_names": name,
        "noncomm_positions_long_all": str(spec_long),
        "noncomm_positions_short_all": str(spec_short),
        "comm_positions_long_all": str(comm_long),
        "comm_positions_short_all": str(comm_short),
        "open_interest_all": str(oi),
    }


class FakeGet:
    def __init__(self, rows=None, raises=None, status=200, body=""):
        self.rows = rows if rows is not None else []
        self.raises = raises
        self.status = status
        self.body = body
        self.calls = []

    def __call__(self, url, params=None, timeout=None, headers=None):
        self.calls.append({"url": url, "params": params})
        if self.raises:
            raise self.raises
        return SimpleNamespace(
            status_code=self.status, text=self.body, json=lambda: self.rows
        )


class TestSummary:
    def test_reports_the_spec_net_position(self):
        text = summarise_cot([row(spec_long=200_000, spec_short=50_000)])

        assert "+150,000" in text

    def test_reports_the_commercial_net_position(self):
        text = summarise_cot([row(comm_long=80_000, comm_short=260_000)])

        assert "-180,000" in text

    def test_names_the_contract_and_the_week(self):
        text = summarise_cot([row(date="2026-07-28")])

        assert "GOLD - COMMODITY EXCHANGE INC." in text
        assert "2026-07-28" in text

    def test_strips_the_timestamp_off_the_date(self):
        assert "T00:00:00" not in summarise_cot([row()])

    def test_week_on_week_change_is_signed(self):
        latest = row(spec_long=210_000, spec_short=50_000)   # net +160,000
        previous = row(spec_long=190_000, spec_short=50_000)  # net +140,000

        text = summarise_cot([latest, previous])

        assert "+20,000" in text
        assert "added longs" in text

    def test_a_reduction_is_described_as_cutting(self):
        latest = row(spec_long=100_000, spec_short=50_000)
        previous = row(spec_long=180_000, spec_short=50_000)

        assert "cut longs" in summarise_cot([latest, previous])

    def test_a_single_week_omits_the_comparison(self):
        assert "Week on week" not in summarise_cot([row()])

    @pytest.mark.parametrize(
        "long_, short_, lean",
        [(200_000, 50_000, "long"), (50_000, 200_000, "short"), (100_000, 100_000, "flat")],
    )
    def test_describes_which_way_specs_lean(self, long_, short_, lean):
        text = summarise_cot([row(spec_long=long_, spec_short=short_)])

        assert f"net {lean}" in text

    def test_says_the_data_is_stale_by_design(self):
        """Measured Tuesday, published Friday. Trading it as live is a mistake."""
        text = summarise_cot([row()])

        assert "behind price" in text

    def test_does_not_present_positioning_as_a_signal(self):
        assert "not a signal" in summarise_cot([row()])

    def test_empty_response_is_reported_not_crashed(self):
        assert "No COT data" in summarise_cot([])

    def test_missing_columns_are_treated_as_zero(self):
        """Socrata omits empty columns rather than sending null."""
        text = summarise_cot([{"market_and_exchange_names": "SILVER"}])

        assert "SILVER" in text
        assert "+0" in text

    def test_non_numeric_values_do_not_crash(self):
        bad = row()
        bad["noncomm_positions_long_all"] = ""
        bad["open_interest_all"] = "n/a"

        assert "COT" in summarise_cot([bad])

    def test_decimal_strings_are_accepted(self):
        r = row()
        r["noncomm_positions_long_all"] = "200000.0"

        assert "+150,000" in summarise_cot([r])

    def test_stays_within_telegrams_message_limit(self):
        assert len(summarise_cot([row(), row()])) < 4096


class TestFetch:
    def test_requests_the_configured_contract_newest_first(self, monkeypatch):
        get = FakeGet([row()])
        monkeypatch.setattr(Main.requests, "get", get)
        monkeypatch.setattr(Main, "COT_MARKET_CODE", "088691")

        fetch_cot()

        params = get.calls[0]["params"]
        assert params["cftc_contract_market_code"] == "088691"
        assert "DESC" in params["$order"]
        assert params["$limit"] == 2

    def test_an_explicit_contract_overrides_the_default(self, monkeypatch):
        get = FakeGet([row()])
        monkeypatch.setattr(Main.requests, "get", get)

        fetch_cot("084691")

        assert get.calls[0]["params"]["cftc_contract_market_code"] == "084691"

    def test_an_http_error_carries_the_body(self, monkeypatch):
        monkeypatch.setattr(Main.requests, "get", FakeGet(status=404, body="not found"))

        with pytest.raises(RuntimeError, match="not found"):
            fetch_cot()

    def test_no_api_key_is_sent(self, monkeypatch):
        """The CFTC feed is open. A key would be a needless dependency."""
        get = FakeGet([row()])
        monkeypatch.setattr(Main.requests, "get", get)

        fetch_cot()

        assert "headers" not in get.calls[0] or not get.calls[0].get("headers")


class TestCommand:
    def run(self, monkeypatch, get, args=None):
        sent = []

        class Msg:
            async def reply_text(self, text):
                sent.append(text)

        monkeypatch.setattr(Main.requests, "get", get)
        update = SimpleNamespace(message=Msg())
        ctx = SimpleNamespace(args=args or [])
        asyncio.run(Main.cmd_cot(update, ctx))
        return sent[0]

    def test_replies_with_the_summary(self, monkeypatch):
        text = self.run(monkeypatch, FakeGet([row(), row(spec_long=190_000)]))

        assert "COT" in text
        assert "GOLD" in text

    def test_an_unreachable_cftc_is_reported_not_raised(self, monkeypatch):
        text = self.run(monkeypatch, FakeGet(raises=ConnectionError("no route")))

        assert "Could not reach the CFTC" in text
        assert "no route" in text

    def test_an_argument_selects_another_contract(self, monkeypatch):
        get = FakeGet([row(name="SILVER - COMMODITY EXCHANGE INC.")])

        text = self.run(monkeypatch, get, args=["084691"])

        assert get.calls[0]["params"]["cftc_contract_market_code"] == "084691"
        assert "SILVER" in text

    def test_no_data_for_a_contract_is_reported(self, monkeypatch):
        assert "No COT data" in self.run(monkeypatch, FakeGet([]))
