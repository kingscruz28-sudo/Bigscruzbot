from datetime import datetime, timedelta, timezone

import pytest

import Main
from backtest.crt_backtest import (
    Bar,
    Trade,
    collect_signals,
    geometry_report,
    realised_r,
    resolve,
    summarise,
)

# 03:00 UTC is inside the Asian session, so signals are allowed.
START = datetime(2025, 1, 6, 3, 0, tzinfo=timezone.utc)


def bars_from_closes(closes, spread=1.0):
    """One bar per minute. High/low straddle the close so resolution has room
    to work without accidentally touching a level."""
    return [
        Bar(
            ts=START + timedelta(minutes=i),
            open=c,
            high=c + spread,
            low=c - spread,
            close=c,
        )
        for i, c in enumerate(closes)
    ]


def buy_setup_closes():
    """Flat at 2700, a dip to 2680 sweeping the low, then back up to 2705.

    The 21st sample lands on a multiple of 5, which is when the scanner checks.
    """
    return [2700.0] * 18 + [2680.0, 2700.0, 2705.0]


class TestCollectSignals:
    def test_fires_the_expected_buy(self):
        bars = bars_from_closes(buy_setup_closes())

        trades = collect_signals(bars, "XAU/USD")

        assert len(trades) == 1
        trade = trades[0]
        assert trade.direction == "BUY"
        assert (trade.entry, trade.sl, trade.tp) == (2705.0, 2630.0, 2855.0)
        assert trade.session == "ASIAN"

    def test_no_signal_before_twenty_samples(self):
        bars = bars_from_closes([2700.0] * 10 + [2680.0, 2705.0])

        assert collect_signals(bars, "XAU/USD") == []

    def test_flat_market_produces_nothing(self):
        bars = bars_from_closes([2700.0] * 200)

        assert collect_signals(bars, "XAU/USD") == []

    def test_dead_session_is_skipped(self, monkeypatch):
        # 19:00 UTC is the dead zone; the same prices must not signal.
        monkeypatch.setattr(
            "backtest.crt_backtest.SAMPLES_PER_SIGNAL_CHECK", 5, raising=False
        )
        closes = buy_setup_closes()
        bars = [
            Bar(
                ts=datetime(2025, 1, 6, 19, 0, tzinfo=timezone.utc) + timedelta(minutes=i),
                open=c,
                high=c + 1,
                low=c - 1,
                close=c,
            )
            for i, c in enumerate(closes)
        ]

        assert collect_signals(bars, "XAU/USD") == []

    def test_cooldown_uses_bar_time_not_wall_clock(self):
        # Repeat the setup back to back. The second is inside the 30-minute
        # cooldown and must be suppressed.
        closes = buy_setup_closes() + [2700.0] * 4 + buy_setup_closes()
        bars = bars_from_closes(closes)

        trades = collect_signals(bars, "XAU/USD")

        assert len(trades) == 1

    def test_leaves_main_state_clean_for_the_live_bot(self):
        real_time = Main.time
        collect_signals(bars_from_closes(buy_setup_closes()), "XAU/USD")

        assert Main.time is real_time

    def test_restores_the_clock_even_when_the_detector_raises(self, monkeypatch):
        real_time = Main.time
        monkeypatch.setattr(Main, "detect_crt_signal", lambda *a: 1 / 0)

        with pytest.raises(ZeroDivisionError):
            collect_signals(bars_from_closes(buy_setup_closes()), "XAU/USD")

        assert Main.time is real_time


class TestSignalFunnel:
    def test_reports_each_stage(self):
        stats: dict = {}
        bars = bars_from_closes(buy_setup_closes())

        collect_signals(bars, "XAU/USD", stats)

        # 21 bars → checks at indices 0, 5, 10, 15, 20.
        assert stats["checks"] == 5
        assert stats["session_allowed"] == 5
        assert stats["signals"] == 1
        assert stats["sweep_met_minimum"] >= 1

    def test_stages_never_increase_down_the_funnel(self):
        stats: dict = {}
        collect_signals(bars_from_closes([2700.0] * 300), "XAU/USD", stats)

        order = [
            "checks",
            "session_allowed",
            "enough_history",
            "swept_a_level",
            "sweep_met_minimum",
            "signals",
        ]
        values = [stats[k] for k in order]
        assert values == sorted(values, reverse=True)

    def test_flat_market_dies_at_the_sweep_stage(self):
        stats: dict = {}
        collect_signals(bars_from_closes([2700.0] * 300), "XAU/USD", stats)

        assert stats["enough_history"] > 0
        assert stats["sweep_met_minimum"] == 0
        assert stats["signals"] == 0


class TestResolve:
    def make_trade(self):
        return Trade(
            symbol="XAU/USD",
            direction="BUY",
            entry=2705.0,
            sl=2630.0,
            tp=2855.0,
            session="ASIAN",
            opened_at=START,
        )

    def test_take_profit(self):
        bars = bars_from_closes([2705.0, 2800.0, 2860.0], spread=0.0)

        trade = resolve(self.make_trade(), bars, start_index=0)

        assert trade.outcome == "TP"
        assert trade.exit_price == 2855.0

    def test_stop_loss(self):
        bars = bars_from_closes([2705.0, 2660.0, 2625.0], spread=0.0)

        trade = resolve(self.make_trade(), bars, start_index=0)

        assert trade.outcome == "SL"
        assert trade.exit_price == 2630.0

    def test_stop_wins_when_one_bar_touches_both(self):
        """A minute bar gives no intrabar ordering, so take the loss."""
        bars = [
            Bar(ts=START, open=2705, high=2705, low=2705, close=2705),
            Bar(ts=START + timedelta(minutes=1), open=2705, high=2900, low=2600, close=2700),
        ]

        trade = resolve(self.make_trade(), bars, start_index=0)

        assert trade.outcome == "SL"

    def test_times_out_when_neither_level_is_touched(self):
        bars = bars_from_closes([2705.0] * 50, spread=0.0)

        trade = resolve(self.make_trade(), bars, start_index=0, timeout_bars=10)

        assert trade.outcome == "TIMEOUT"

    def test_sell_resolves_the_other_way(self):
        trade = Trade(
            symbol="XAU/USD",
            direction="SELL",
            entry=2695.0,
            sl=2770.0,
            tp=2545.0,
            session="ASIAN",
            opened_at=START,
        )
        bars = bars_from_closes([2695.0, 2600.0, 2540.0], spread=0.0)

        assert resolve(trade, bars, start_index=0).outcome == "TP"


class TestRealisedR:
    def resolved(self, outcome, exit_price, direction="BUY"):
        trade = Trade(
            symbol="XAU/USD",
            direction=direction,
            entry=2705.0,
            sl=2630.0,
            tp=2855.0,
            session="ASIAN",
            opened_at=START,
        )
        trade.outcome, trade.exit_price = outcome, exit_price
        return trade

    def test_winner_is_the_configured_reward_multiple(self):
        # 150 of profit against 75 of risk.
        assert realised_r(self.resolved("TP", 2855.0)) == pytest.approx(2.0)

    def test_loser_is_minus_one_r(self):
        assert realised_r(self.resolved("SL", 2630.0)) == pytest.approx(-1.0)

    def test_take_profit_pays_entry_cost_only(self):
        """A TP exit is a limit fill, so only the market entry pays."""
        r = realised_r(self.resolved("TP", 2855.0), cost_per_leg=0.5)

        assert r == pytest.approx((2855.0 - 2705.5) / 75.0)

    def test_stop_loss_pays_both_legs(self):
        r = realised_r(self.resolved("SL", 2630.0), cost_per_leg=0.5)

        assert r == pytest.approx((2629.5 - 2705.5) / 75.0)

    def test_cost_hurts_a_sell_in_the_same_direction(self):
        trade = self.resolved("SL", 2770.0, direction="SELL")
        trade.entry, trade.sl, trade.tp = 2695.0, 2770.0, 2545.0

        assert realised_r(trade, cost_per_leg=0.5) < realised_r(trade, cost_per_leg=0.0)


class TestSummarise:
    def resolved(self, outcome):
        trade = Trade(
            symbol="XAU/USD",
            direction="BUY",
            entry=2705.0,
            sl=2630.0,
            tp=2855.0,
            session="ASIAN",
            opened_at=START,
        )
        trade.outcome = outcome
        trade.exit_price = 2855.0 if outcome == "TP" else 2630.0
        return trade

    def test_counts_and_profit_factor(self):
        trades = [self.resolved("TP"), self.resolved("SL"), self.resolved("SL")]

        s = summarise(trades)

        assert s["signals"] == 3
        assert (s["wins"], s["losses"]) == (1, 2)
        assert s["win_rate"] == pytest.approx(33.33, abs=0.01)
        assert s["total_r"] == pytest.approx(0.0)
        assert s["profit_factor"] == pytest.approx(1.0)

    def test_drawdown_tracks_the_worst_peak_to_trough(self):
        trades = [self.resolved("TP"), self.resolved("SL"), self.resolved("SL")]

        # +2, then -1, -1 → peak 2, trough 0.
        assert summarise(trades)["max_drawdown_r"] == pytest.approx(2.0)

    def test_empty_input_does_not_divide_by_zero(self):
        assert summarise([])["signals"] == 0


class TestGeometryReport:
    def test_reports_every_traded_symbol(self):
        report = geometry_report()

        for symbol in Main.SYMBOLS:
            assert symbol in report

    def test_gold_target_is_a_multi_percent_move(self):
        """The headline finding: a gold win needs price to travel over 5%."""
        report = geometry_report({"XAU/USD": 2700.0})

        assert "5.56%" in report
        assert "1.85%" in report
