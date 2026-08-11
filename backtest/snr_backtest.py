"""Python mirror of JarvisSNR.mq5, for cross-checking against Strategy Tester.

This is deliberately a *mirror*, not a second opinion. Every rule is
implemented to match the EA line for line — same level construction, same MISS
count, same touch test, same second-candle rule, same structural stop. Run
both over the same window and the trade lists should line up. Where they
don't, one of the two has a bug, and that is the whole point: a number you
can't reproduce in a second engine isn't a result yet.

Usage
-----
    python -m backtest.snr_backtest run --csv XAUUSD_M1.csv \
        --level-tf D1 --entry-tf M15 --spread 0.25 --slippage 0.10

The CSV is M1 OHLC; the higher timeframes are aggregated from it, so one
export drives everything.
"""

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.crt_backtest import (  # noqa: E402
    Bar,
    Trade,
    load_bars,
    realised_r,
    slice_bars,
    summarise,
)

TIMEFRAMES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}


@dataclass
class Level:
    lo: float
    hi: float
    formed: datetime
    is_resistance: bool
    miss_candles: int = 0
    stacked: int = 1
    spent: bool = False

    @property
    def mid(self) -> float:
        return (self.lo + self.hi) / 2.0


@dataclass
class SnrConfig:
    """Mirrors the EA's inputs. Defaults match JarvisSNR.mq5."""

    level_tf: str = "D1"
    entry_tf: str = "M15"
    level_lookback: int = 120
    zone_buffer: float = 0.0
    require_wick_touch: bool = True
    min_miss_candles: int = 2
    require_second_candle_hold: bool = True
    min_stacked_labels: int = 1
    stack_tolerance_atr: float = 0.25
    use_session_filter: bool = True
    session_start_hour: int = 7
    session_end_hour: int = 17
    swing_lookback: int = 12
    stop_buffer: float = 0.0
    reward_multiple: float = 3.0
    max_open_positions: int = 1
    timeout_bars: int = 1440


# ── Aggregation ───────────────────────────────────────────────────────────


def resample(bars: list[Bar], minutes: int) -> list[Bar]:
    """Aggregate M1 bars into a higher timeframe, oldest first.

    Buckets are aligned to the UTC epoch, which matches MT5 only when the
    broker's day starts at 00:00 server time. If the broker rolls at 22:00 or
    23:00, D1 levels will differ from Strategy Tester by one bucket — pass
    --level-tf H4 to take that variable out of a comparison run.
    """
    if minutes <= 1:
        return list(bars)

    out: list[Bar] = []
    bucket: list[Bar] = []
    current: datetime | None = None
    width = timedelta(minutes=minutes)

    for bar in bars:
        epoch_minutes = int(bar.ts.replace(tzinfo=timezone.utc).timestamp() // 60)
        start = datetime.fromtimestamp(
            (epoch_minutes // minutes) * minutes * 60, tz=timezone.utc
        )
        if current is None:
            current = start
        if start != current:
            if bucket:
                out.append(_merge(bucket, current))
            bucket, current = [], start
        bucket.append(bar)

    if bucket and current is not None:
        out.append(_merge(bucket, current))
    return out


def _merge(bucket: list[Bar], start: datetime) -> Bar:
    return Bar(
        ts=start,
        open=bucket[0].open,
        high=max(b.high for b in bucket),
        low=min(b.low for b in bucket),
        close=bucket[-1].close,
    )


def average_true_range(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    window = bars[-(period + 1) :]
    ranges = []
    for prev, cur in zip(window, window[1:]):
        ranges.append(
            max(
                cur.high - cur.low,
                abs(cur.high - prev.close),
                abs(cur.low - prev.close),
            )
        )
    return sum(ranges) / len(ranges) if ranges else 0.0


# ── Level construction (mirrors BuildLevels) ──────────────────────────────


def build_levels(htf: list[Bar], cfg: SnrConfig) -> list[Level]:
    """Draw SNR levels body close -> next body open, wicks ignored.

    Bullish then bearish is resistance (the "A" shape on a line chart);
    bearish then bullish is support (the "V").
    """
    scope = htf[-cfg.level_lookback :] if cfg.level_lookback else htf
    if len(scope) < 10:
        return []

    atr = average_true_range(scope)
    tolerance = atr * cfg.stack_tolerance_atr if atr > 0 else 0.0
    levels: list[Level] = []

    for i in range(len(scope) - 1):
        first, nxt = scope[i], scope[i + 1]
        first_bull = first.close > first.open
        first_bear = first.close < first.open
        next_bull = nxt.close > nxt.open
        next_bear = nxt.close < nxt.open

        is_resistance = first_bull and next_bear
        is_support = first_bear and next_bull
        if not (is_resistance or is_support):
            continue

        a, b = first.close, nxt.open
        level = Level(
            lo=min(a, b) - cfg.zone_buffer,
            hi=max(a, b) + cfg.zone_buffer,
            formed=nxt.ts,
            is_resistance=is_resistance,
        )

        # MISS: candles after formation whose wicks failed to reach the zone,
        # counted until the first one that touches.
        for candle in scope[i + 2 :]:
            if candle.low <= level.hi and candle.high >= level.lo:
                break
            level.miss_candles += 1

        merged = False
        for existing in levels:
            if tolerance and abs(existing.mid - level.mid) <= tolerance:
                existing.stacked += 1
                existing.miss_candles = max(existing.miss_candles, level.miss_candles)
                merged = True
                break
        if not merged:
            levels.append(level)

    return levels


def in_session(ts: datetime, cfg: SnrConfig) -> bool:
    if not cfg.use_session_filter:
        return True
    hour = ts.hour
    if cfg.session_start_hour <= cfg.session_end_hour:
        return cfg.session_start_hour <= hour < cfg.session_end_hour
    return hour >= cfg.session_start_hour or hour < cfg.session_end_hour


# ── Replay (mirrors OnTick) ───────────────────────────────────────────────


@dataclass
class SnrRun:
    trades: list[Trade] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def collect_signals(m1: list[Bar], cfg: SnrConfig) -> SnrRun:
    htf_all = resample(m1, TIMEFRAMES[cfg.level_tf])
    entry = resample(m1, TIMEFRAMES[cfg.entry_tf])

    counts = {
        "entry_bars": 0,
        "in_session": 0,
        "levels_available": 0,
        "wick_touched_a_level": 0,
        "passed_miss": 0,
        "passed_stacking": 0,
        "signals": 0,
        "level_rebuilds": 0,
    }

    run = SnrRun(stats=counts)
    levels: list[Level] = []
    open_until: datetime | None = None

    # How many HTF bars have fully closed. Entry bars only move forward, so
    # this pointer only ever advances — rescanning the whole HTF series on
    # every entry bar made the run quadratic and unusable on multi-year files.
    htf_width = timedelta(minutes=TIMEFRAMES[cfg.level_tf])
    htf_closed_count = 0
    lookback = cfg.level_lookback

    for index, bar in enumerate(entry):
        if index < 2:
            continue
        counts["entry_bars"] += 1

        # Advance over any HTF bars that closed before this entry bar, and
        # rebuild only when at least one new one has. No look-ahead: a bar is
        # only visible once its close is in the past.
        rebuilt = False
        while (
            htf_closed_count < len(htf_all)
            and htf_all[htf_closed_count].ts + htf_width <= bar.ts
        ):
            htf_closed_count += 1
            rebuilt = True

        if rebuilt and htf_closed_count:
            counts["level_rebuilds"] += 1
            # Slice only the lookback window rather than the whole history,
            # so a rebuild costs the same on year 20 as on year 1.
            start = max(0, htf_closed_count - lookback) if lookback else 0
            levels = build_levels(htf_all[start:htf_closed_count], cfg)

        if not levels:
            continue
        counts["levels_available"] += 1

        if not in_session(bar.ts, cfg):
            continue
        counts["in_session"] += 1

        # One position at a time, matching MaxOpenPositions=1.
        if open_until is not None and bar.ts <= open_until:
            continue

        touch, prior = bar, entry[index - 1]

        for level in levels:
            if level.spent:
                continue

            if not (touch.low <= level.hi and touch.high >= level.lo):
                continue
            counts["wick_touched_a_level"] += 1

            if level.miss_candles < cfg.min_miss_candles:
                continue
            counts["passed_miss"] += 1

            if level.stacked < cfg.min_stacked_labels:
                continue
            counts["passed_stacking"] += 1

            body_lo = min(touch.open, touch.close)
            body_hi = max(touch.open, touch.close)
            body_in = body_lo <= level.hi and body_hi >= level.lo
            poked_out = touch.low < level.lo or touch.high > level.hi
            if cfg.require_wick_touch and body_in and not poked_out:
                continue

            window = entry[max(0, index - cfg.swing_lookback + 1) : index + 1]

            if level.is_resistance:
                if touch.high < level.hi:
                    continue
                closed_through = touch.close > level.hi
                if cfg.require_second_candle_hold:
                    if closed_through and prior.close > level.hi:
                        continue
                elif closed_through:
                    continue

                stop = max(b.high for b in window) + cfg.stop_buffer
                distance = stop - touch.close
                if distance <= 0:
                    continue
                trade = Trade(
                    symbol="SNR",
                    direction="SELL",
                    entry=touch.close,
                    sl=stop,
                    tp=touch.close - distance * cfg.reward_multiple,
                    session="SNR",
                    opened_at=touch.ts,
                )
            else:
                if touch.low > level.lo:
                    continue
                closed_through = touch.close < level.lo
                if cfg.require_second_candle_hold:
                    if closed_through and prior.close < level.lo:
                        continue
                elif closed_through:
                    continue

                stop = min(b.low for b in window) - cfg.stop_buffer
                distance = touch.close - stop
                if distance <= 0:
                    continue
                trade = Trade(
                    symbol="SNR",
                    direction="BUY",
                    entry=touch.close,
                    sl=stop,
                    tp=touch.close + distance * cfg.reward_multiple,
                    session="SNR",
                    opened_at=touch.ts,
                )

            trade._open_index = index  # type: ignore[attr-defined]
            run.trades.append(trade)
            level.spent = True
            counts["signals"] += 1
            open_until = _resolve(trade, entry, index, cfg)
            break

    return run


def _resolve(trade: Trade, bars: list[Bar], start: int, cfg: SnrConfig) -> datetime:
    """Walk forward to SL or TP. Stop wins a bar that touches both."""
    for bar in bars[start + 1 : start + 1 + cfg.timeout_bars]:
        if trade.direction == "BUY":
            hit_sl, hit_tp = bar.low <= trade.sl, bar.high >= trade.tp
        else:
            hit_sl, hit_tp = bar.high >= trade.sl, bar.low <= trade.tp

        if hit_sl:
            trade.outcome, trade.exit_price, trade.closed_at = "SL", trade.sl, bar.ts
            return bar.ts
        if hit_tp:
            trade.outcome, trade.exit_price, trade.closed_at = "TP", trade.tp, bar.ts
            return bar.ts

    last = bars[min(start + cfg.timeout_bars, len(bars) - 1)]
    trade.outcome, trade.exit_price, trade.closed_at = "TIMEOUT", last.close, last.ts
    return last.ts


# ── Report ────────────────────────────────────────────────────────────────


def run_report(m1: list[Bar], cfg: SnrConfig, spread: float, slippage: float) -> str:
    run = collect_signals(m1, cfg)
    trades = run.trades

    span = f"{m1[0].ts:%Y-%m-%d} → {m1[-1].ts:%Y-%m-%d}" if m1 else "no data"
    lines = [
        f"Malaysian SNR backtest — mirror of JarvisSNR.mq5",
        f"{len(m1):,} M1 bars   {span}",
        f"levels {cfg.level_tf} · entries {cfg.entry_tf} · minMiss {cfg.min_miss_candles}"
        f" · stacked>={cfg.min_stacked_labels} · R {cfg.reward_multiple:g}",
        "",
        "Funnel",
        "-" * 52,
    ]
    for key, label in [
        ("entry_bars", "entry bars examined"),
        ("levels_available", "with levels built"),
        ("in_session", "inside the session window"),
        ("wick_touched_a_level", "touching a level"),
        ("passed_miss", "level had enough MISS candles"),
        ("passed_stacking", "level met the stacking minimum"),
        ("signals", "became a trade"),
    ]:
        lines.append(f"{run.stats.get(key, 0):>10,}  {label}")
    lines.append("")

    if not trades:
        lines.append("No trades. The funnel shows which rule rejected them.")
        return "\n".join(lines)

    outcomes = {o: sum(1 for t in trades if t.outcome == o) for o in ("TP", "SL", "TIMEOUT")}
    lines.append(
        f"Outcomes: {outcomes['TP']} TP · {outcomes['SL']} SL · {outcomes['TIMEOUT']} timed out"
    )
    lines.append("")
    lines.append(
        f"{'Cost scenario':<28}{'Trades':>8}{'Win%':>8}{'Total R':>10}{'PF':>8}{'MaxDD':>9}"
    )
    lines.append("-" * 71)

    leg = spread + slippage
    scenarios = [("No cost (baseline)", 0.0)]
    if leg:
        scenarios += [
            (f"Half cost ({leg / 2:.2f}/leg)", leg / 2),
            (f"Stated cost ({leg:.2f}/leg)", leg),
            (f"Double cost ({leg * 2:.2f}/leg)", leg * 2),
        ]

    for label, cost in scenarios:
        s = summarise(trades, cost)
        pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        lines.append(
            f"{label:<28}{s['signals']:>8}{s['win_rate']:>7.1f}%"
            f"{s['total_r']:>10.1f}{pf:>8}{s['max_drawdown_r']:>9.1f}"
        )

    lines += [
        "",
        "Trade list (for lining up against Strategy Tester)",
        "-" * 71,
        f"{'opened':<18}{'dir':<6}{'entry':>11}{'stop':>11}{'target':>11}{'out':>9}",
    ]
    for t in trades[:40]:
        lines.append(
            f"{t.opened_at:%Y-%m-%d %H:%M}  {t.direction:<6}"
            f"{t.entry:>11.2f}{t.sl:>11.2f}{t.tp:>11.2f}{str(t.outcome):>9}"
        )
    if len(trades) > 40:
        lines.append(f"... {len(trades) - 40} more")

    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run")
    run.add_argument("--csv", required=True)
    run.add_argument("--level-tf", default="D1", choices=sorted(TIMEFRAMES))
    run.add_argument("--entry-tf", default="M15", choices=sorted(TIMEFRAMES))
    run.add_argument("--min-miss", type=int, default=2)
    run.add_argument("--min-stacked", type=int, default=1)
    run.add_argument("--reward", type=float, default=3.0)
    run.add_argument("--no-session-filter", action="store_true")
    run.add_argument("--loose-body-rule", action="store_true",
                     help="p21 r3: allow one close through (default). Off = strict.")
    run.add_argument("--since", help="ISO date, inclusive")
    run.add_argument("--until", help="ISO date, inclusive")
    run.add_argument("--days", type=int, help="window counting back from the last bar")
    run.add_argument("--spread", type=float, default=0.25)
    run.add_argument("--slippage", type=float, default=0.10)

    args = parser.parse_args(argv)

    cfg = SnrConfig(
        level_tf=args.level_tf,
        entry_tf=args.entry_tf,
        min_miss_candles=args.min_miss,
        min_stacked_labels=args.min_stacked,
        reward_multiple=args.reward,
        use_session_filter=not args.no_session_filter,
        require_second_candle_hold=not args.loose_body_rule,
    )
    bars = slice_bars(load_bars(args.csv), args.since, args.until, args.days)
    if not bars:
        raise SystemExit('no bars in that window')
    print(run_report(bars, cfg, args.spread, args.slippage))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
