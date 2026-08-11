from datetime import datetime, timedelta, timezone

import pytest

from backtest.crt_backtest import Bar
from backtest.snr_backtest import (
    SnrConfig,
    build_levels,
    collect_signals,
    in_session,
    resample,
)

START = datetime(2025, 3, 3, 0, 0, tzinfo=timezone.utc)


def bar(minute, o, h, l, c):
    return Bar(ts=START + timedelta(minutes=minute), open=o, high=h, low=l, close=c)


def candle(index, o, h, l, c, minutes=1):
    """A bar placed on an arbitrary timeframe grid."""
    return Bar(
        ts=START + timedelta(minutes=index * minutes),
        open=o,
        high=h,
        low=l,
        close=c,
    )


class TestResample:
    def test_aggregates_ohlc_correctly(self):
        m1 = [
            bar(0, 100, 105, 99, 102),
            bar(1, 102, 108, 101, 104),
            bar(2, 104, 106, 95, 97),
        ]

        m5 = resample(m1, 5)

        assert len(m5) == 1
        assert (m5[0].open, m5[0].high, m5[0].low, m5[0].close) == (100, 108, 95, 97)

    def test_splits_across_bucket_boundaries(self):
        m1 = [bar(i, 100, 101, 99, 100) for i in range(12)]

        assert len(resample(m1, 5)) == 3  # 0-4, 5-9, 10-11

    def test_m1_passes_through_unchanged(self):
        m1 = [bar(i, 100, 101, 99, 100) for i in range(3)]

        assert resample(m1, 1) == m1

    def test_buckets_align_to_the_clock(self):
        m5 = resample([bar(i, 100, 101, 99, 100) for i in range(10)], 5)

        assert m5[0].ts.minute == 0
        assert m5[1].ts.minute == 5


class TestBuildLevels:
    def flat(self, n, price=2700.0):
        return [candle(i, price, price + 1, price - 1, price) for i in range(n)]

    def test_bullish_then_bearish_is_resistance(self):
        bars = self.flat(10)
        # A bullish candle followed by a bearish one.
        bars.append(candle(10, 2700, 2712, 2699, 2710))  # bull, closes 2710
        bars.append(candle(11, 2708, 2709, 2695, 2698))  # bear, opens 2708
        bars += [candle(i, 2650, 2652, 2648, 2650) for i in range(12, 20)]

        levels = build_levels(bars, SnrConfig(level_lookback=0, stack_tolerance_atr=0))

        res = [l for l in levels if l.is_resistance]
        assert res
        assert any(abs(l.lo - 2708) < 1e-6 and abs(l.hi - 2710) < 1e-6 for l in res)

    def test_bearish_then_bullish_is_support(self):
        bars = self.flat(10)
        bars.append(candle(10, 2700, 2701, 2688, 2690))  # bear, closes 2690
        bars.append(candle(11, 2692, 2705, 2691, 2704))  # bull, opens 2692
        bars += [candle(i, 2750, 2752, 2748, 2750) for i in range(12, 20)]

        levels = build_levels(bars, SnrConfig(level_lookback=0, stack_tolerance_atr=0))

        sup = [l for l in levels if not l.is_resistance]
        assert any(abs(l.lo - 2690) < 1e-6 and abs(l.hi - 2692) < 1e-6 for l in sup)

    def test_doji_pairs_make_no_level(self):
        bars = [candle(i, 2700, 2701, 2699, 2700) for i in range(20)]

        assert build_levels(bars, SnrConfig(level_lookback=0)) == []

    def test_miss_counts_candles_that_never_reach_the_zone(self):
        bars = self.flat(6)
        bars.append(candle(6, 2700, 2712, 2699, 2710))
        bars.append(candle(7, 2708, 2709, 2695, 2698))
        # Four candles well below the zone — none touch it.
        bars += [candle(i, 2600, 2605, 2595, 2600) for i in range(8, 12)]
        # Then one that reaches back up into it.
        bars.append(candle(12, 2600, 2711, 2599, 2700))

        levels = build_levels(bars, SnrConfig(level_lookback=0, stack_tolerance_atr=0))

        res = [l for l in levels if l.is_resistance][0]
        assert res.miss_candles == 4

    def test_levels_at_the_same_price_stack(self):
        bars = self.flat(4)
        for base in (4, 10):
            bars.append(candle(base, 2700, 2712, 2699, 2710))
            bars.append(candle(base + 1, 2708, 2709, 2695, 2698))
            bars += [candle(i, 2600, 2605, 2595, 2600) for i in range(base + 2, base + 6)]

        cfg = SnrConfig(level_lookback=0, stack_tolerance_atr=5.0)
        levels = build_levels(bars, cfg)

        assert any(l.stacked >= 2 for l in levels)


class TestSessionFilter:
    def test_normal_window(self):
        cfg = SnrConfig(session_start_hour=7, session_end_hour=17)

        assert in_session(START.replace(hour=9), cfg)
        assert not in_session(START.replace(hour=18), cfg)

    def test_window_wrapping_midnight(self):
        cfg = SnrConfig(session_start_hour=22, session_end_hour=6)

        assert in_session(START.replace(hour=23), cfg)
        assert in_session(START.replace(hour=2), cfg)
        assert not in_session(START.replace(hour=12), cfg)

    def test_filter_can_be_disabled(self):
        cfg = SnrConfig(use_session_filter=False)

        assert in_session(START.replace(hour=3), cfg)


class TestCollectSignals:
    def test_flat_market_makes_no_trades(self):
        m1 = [bar(i, 2700, 2700.5, 2699.5, 2700) for i in range(5000)]
        cfg = SnrConfig(use_session_filter=False)

        run = collect_signals(m1, cfg)

        assert run.trades == []
        assert run.stats["signals"] == 0

    def test_funnel_never_increases_down_the_stages(self):
        m1 = [
            bar(i, 2700 + (i % 7), 2705 + (i % 7), 2695 + (i % 7), 2700 + (i % 5))
            for i in range(6000)
        ]
        cfg = SnrConfig(use_session_filter=False, level_tf="H1", entry_tf="M15")

        run = collect_signals(m1, cfg)
        s = run.stats

        assert s["entry_bars"] >= s["levels_available"] >= s["in_session"]
        assert s["passed_miss"] >= s["passed_stacking"] >= s["signals"]

    def test_miss_requirement_can_only_reduce_trades(self):
        m1 = [
            bar(i, 2700 + (i % 11), 2708 + (i % 11), 2692 + (i % 11), 2700 + (i % 9))
            for i in range(8000)
        ]
        base = SnrConfig(use_session_filter=False, level_tf="H1", min_miss_candles=0)
        strict = SnrConfig(use_session_filter=False, level_tf="H1", min_miss_candles=6)

        assert len(collect_signals(m1, strict).trades) <= len(
            collect_signals(m1, base).trades
        )

    def test_every_trade_has_a_stop_on_the_correct_side(self):
        m1 = [
            bar(i, 2700 + (i % 13), 2710 + (i % 13), 2690 + (i % 13), 2700 + (i % 7))
            for i in range(8000)
        ]
        cfg = SnrConfig(use_session_filter=False, level_tf="H1", min_miss_candles=0)

        for t in collect_signals(m1, cfg).trades:
            if t.direction == "BUY":
                assert t.sl < t.entry < t.tp
            else:
                assert t.tp < t.entry < t.sl

    def test_reward_multiple_is_honoured(self):
        m1 = [
            bar(i, 2700 + (i % 13), 2710 + (i % 13), 2690 + (i % 13), 2700 + (i % 7))
            for i in range(8000)
        ]
        cfg = SnrConfig(
            use_session_filter=False, level_tf="H1", min_miss_candles=0, reward_multiple=2.5
        )

        for t in collect_signals(m1, cfg).trades:
            assert abs(t.entry - t.tp) == pytest.approx(2.5 * abs(t.entry - t.sl))

    def test_positions_do_not_overlap(self):
        m1 = [
            bar(i, 2700 + (i % 13), 2710 + (i % 13), 2690 + (i % 13), 2700 + (i % 7))
            for i in range(8000)
        ]
        cfg = SnrConfig(use_session_filter=False, level_tf="H1", min_miss_candles=0)

        trades = collect_signals(m1, cfg).trades
        for earlier, later in zip(trades, trades[1:]):
            assert earlier.closed_at is not None
            assert later.opened_at >= earlier.closed_at

    def test_every_trade_is_resolved(self):
        m1 = [
            bar(i, 2700 + (i % 13), 2710 + (i % 13), 2690 + (i % 13), 2700 + (i % 7))
            for i in range(8000)
        ]
        cfg = SnrConfig(use_session_filter=False, level_tf="H1", min_miss_candles=0)

        for t in collect_signals(m1, cfg).trades:
            assert t.outcome in ("TP", "SL", "TIMEOUT")
            assert t.exit_price is not None


class TestScaling:
    """Levels must be rebuilt once per HTF bar, not once per entry bar.

    Rebuilding per entry bar made the replay quadratic — fine on a week of
    data, hours on a multi-year file. This is the deterministic version of
    that check, with no timing flakiness.
    """

    def series(self, minutes):
        return [
            bar(i, 2700 + (i % 13), 2710 + (i % 13), 2690 + (i % 13), 2700 + (i % 7))
            for i in range(minutes)
        ]

    def test_rebuilds_track_htf_bars_not_entry_bars(self):
        cfg = SnrConfig(level_tf="H4", entry_tf="M15", use_session_filter=False)

        run = collect_signals(self.series(20_000), cfg)

        entry_bars = run.stats["entry_bars"]
        rebuilds = run.stats["level_rebuilds"]
        expected_htf = 20_000 // 240  # H4 buckets in the series

        assert rebuilds <= expected_htf + 2
        assert rebuilds < entry_bars / 10  # nowhere near per-entry-bar

    def test_rebuild_count_grows_with_the_level_timeframe_not_the_data(self):
        coarse = SnrConfig(level_tf="D1", entry_tf="M15", use_session_filter=False)
        fine = SnrConfig(level_tf="H1", entry_tf="M15", use_session_filter=False)
        series = self.series(20_000)

        assert (
            collect_signals(series, coarse).stats["level_rebuilds"]
            < collect_signals(series, fine).stats["level_rebuilds"]
        )
