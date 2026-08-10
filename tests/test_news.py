from types import SimpleNamespace

import pytest

import Main
from Main import analyse_news_pairs, fetch_news, format_news_alert, is_market_relevant


class TestAnalyseNewsPairs:
    def test_returns_empty_string_for_unrelated_headline(self):
        assert analyse_news_pairs("Cat rescued from tree") == ""

    def test_maps_gold_headline_to_a_buy_on_xau(self):
        result = analyse_news_pairs("Gold hits record high")
        assert "Gold (XAU/USD)" in result
        assert "BUY" in result
        assert result.startswith("📊 Pair Impact:")

    def test_matching_is_case_insensitive(self):
        assert analyse_news_pairs("GOLD SURGES") == analyse_news_pairs("gold surges")

    def test_one_keyword_can_flag_several_pairs(self):
        result = analyse_news_pairs("FOMC decision due")
        assert "XAU/USD" in result
        assert "USD/JPY" in result

    def test_same_pair_and_direction_is_not_listed_twice(self):
        # "bitcoin" and "btc" both map to BTC/USD BUY.
        result = analyse_news_pairs("Bitcoin (BTC) rallies")
        assert result.count("BTC/USD") == 1

    def test_buy_and_sell_use_opposite_arrows(self):
        buy = analyse_news_pairs("Gold hits record high")
        sell = analyse_news_pairs("SEC announces crackdown")
        assert "⬆️" in buy
        assert "⬇️" in sell

    def test_conflicting_keywords_produce_contradictory_guidance(self):
        """Characterisation test for existing behaviour, not an endorsement.

        "fed" maps XAU/USD to SELL and "rate cut" maps it to BUY, and the hit
        map is keyed by pair+direction, so one headline emits both.
        """
        result = analyse_news_pairs("Fed signals rate cut")
        gold_lines = [ln for ln in result.splitlines() if "XAU/USD" in ln]
        assert len(gold_lines) == 2
        assert any("BUY" in ln for ln in gold_lines)
        assert any("SELL" in ln for ln in gold_lines)

    def test_keywords_match_inside_unrelated_words(self):
        """Characterisation test for a known false positive.

        Matching is a plain substring check, so "whether" contains "eth" and
        trips the Ethereum rule. Word-boundary matching would fix this.
        """
        assert "ETH/USD" in analyse_news_pairs("Whether the rally holds")


class TestFormatNewsAlert:
    def test_includes_title_and_link(self):
        msg = format_news_alert("Cat rescued from tree", "https://example.com/cat")
        assert "📰 NEWS" in msg
        assert "Cat rescued from tree" in msg
        assert "https://example.com/cat" in msg

    def test_appends_pair_analysis_when_relevant(self):
        msg = format_news_alert("Gold hits record high", "https://example.com/gold")
        assert "📊 Pair Impact:" in msg

    def test_omits_analysis_section_when_no_pairs_match(self):
        msg = format_news_alert("Cat rescued from tree", "https://example.com/cat")
        assert "📊 Pair Impact:" not in msg


class TestIsMarketRelevant:
    @pytest.mark.parametrize(
        "title",
        [
            "Gold hits record high",
            "Bitcoin ETF inflows surge",
            "Fed holds rates steady",
            "Trump announces new tariff",
        ],
    )
    def test_accepts_market_headlines(self, title):
        assert is_market_relevant(title) is True

    def test_rejects_unrelated_headline(self):
        assert is_market_relevant("Cat rescued from tree") is False

    def test_substring_matching_causes_false_positives(self):
        """Characterisation test: "award" contains "war"."""
        assert is_market_relevant("Award ceremony tonight") is True


class TestFetchNews:
    @pytest.fixture
    def feeds(self, monkeypatch):
        """Route feedparser.parse by URL; default to an empty feed."""

        def install(by_url, default_entries=()):
            def fake_parse(url):
                for fragment, outcome in by_url.items():
                    if fragment in url:
                        if isinstance(outcome, Exception):
                            raise outcome
                        return SimpleNamespace(entries=outcome)
                return SimpleNamespace(entries=list(default_entries))

            monkeypatch.setattr(Main.feedparser, "parse", fake_parse)

        return install

    def test_collects_entries_across_feeds(self, feeds):
        feeds(
            {
                "investinglive": [{"title": "A", "link": "https://x/a"}],
                "forexlive": [{"title": "B", "link": "https://x/b"}],
                "bbci": [{"title": "C", "link": "https://x/c"}],
            }
        )

        articles = fetch_news()
        assert [a["title"] for a in articles] == ["A", "B", "C"]

    def test_skips_links_already_sent(self, feeds):
        feeds({}, default_entries=[{"title": "A", "link": "https://x/a"}])
        Main.sent_news_urls.add("https://x/a")

        assert fetch_news() == []

    def test_skips_entries_without_a_link(self, feeds):
        feeds({}, default_entries=[{"title": "No link", "link": ""}])

        assert fetch_news() == []

    def test_respects_the_limit(self, feeds):
        feeds(
            {},
            default_entries=[
                {"title": f"T{i}", "link": f"https://x/{i}"} for i in range(5)
            ],
        )

        assert len(fetch_news(limit=2)) == 2

    def test_takes_at_most_ten_entries_per_feed(self, feeds):
        feeds(
            {
                "investinglive": [
                    {"title": f"T{i}", "link": f"https://x/{i}"} for i in range(25)
                ]
            }
        )

        assert len(fetch_news(limit=50)) == 10

    def test_one_broken_feed_does_not_lose_the_others(self, feeds):
        feeds(
            {
                "investinglive": RuntimeError("feed down"),
                "forexlive": [{"title": "B", "link": "https://x/b"}],
                "bbci": [{"title": "C", "link": "https://x/c"}],
            }
        )

        assert [a["title"] for a in fetch_news()] == ["B", "C"]

    def test_same_story_from_two_feeds_is_not_deduplicated(self, feeds):
        """Characterisation test: dedup only happens via sent_news_urls, which
        the scanner populates after sending, so one call can return repeats."""
        feeds({}, default_entries=[{"title": "Same", "link": "https://x/same"}])

        articles = fetch_news()
        assert len(articles) == 3
