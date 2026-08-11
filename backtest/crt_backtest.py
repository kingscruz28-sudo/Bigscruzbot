"""Backtest harness for the live CRT signal logic.

The point of this file is that it does not reimplement the strategy. It
imports `detect_crt_signal` and its parameters straight from `Main`, then
replays historical bars through the same code path the scanner uses, so the
backtest cannot drift away from what the bot actually does.

Usage
-----
    python -m backtest.crt_backtest geometry
    python -m backtest.crt_backtest run --csv XAUUSD_M1.csv --symbol XAU/USD

The CSV needs one row per minute with timestamp, open, high, low, close.
Dukascopy's "Gmt time,Open,High,Low,Close,Volume" export works as-is, as does
any file with ISO timestamps and those column names in any order.
"""

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Main reads these at import time.
os.environ.setdefault("TELEGRAM_TOKEN", "backtest")
os.environ.setdefault("CHAT_ID", "0")
os.environ.setdefault("ER_API_KEY", "backtest")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import Main  # noqa: E402


# Indicative prices for the geometry report only — no trading logic uses these.
REFERENCE_PRICES = {
    "XAU/USD": 2700.0,
    "ETH/USD": 3000.0,
    "USD/JPY": 150.0,
    "SOL/USD": 150.0,
    "BTC/USD": 95000.0,
}

SAMPLES_PER_SIGNAL_CHECK = 5  # scanner_loop runs detect every 5th minute
DEFAULT_TIMEOUT_BARS = 1440  # abandon a trade after 24h unresolved


@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class Trade:
    symbol: str
    direction: str
    entry: float
    sl: float
    tp: float
    session: str
    opened_at: datetime
    closed_at: datetime | None = None
    exit_price: float | None = None
    outcome: str | None = None  # "TP" | "SL" | "TIMEOUT"

    @property
    def risk(self) -> float:
        return abs(self.entry - self.sl)


class _Clock:
    """Stands in for Main's `time` module so the 30-minute signal cooldown is
    measured against bar timestamps rather than wall clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now


# ── Data loading ──────────────────────────────────────────────────────────


def _parse_timestamp(raw: str) -> datetime:
    raw = raw.strip()
    formats = (
        "%d.%m.%Y %H:%M:%S.%f",  # Dukascopy export
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y.%m.%d %H:%M",  # MT5 export
    )
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Last resort: ISO 8601 with offset
    return datetime.fromisoformat(raw).astimezone(timezone.utc)


def load_bars(path: str) -> list[Bar]:
    """Read an OHLC CSV into bars, oldest first."""
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))

    if not rows:
        raise SystemExit(f"{path}: no rows")

    keys = {k.strip().lower(): k for k in rows[0]}

    def column(*candidates):
        for c in candidates:
            if c in keys:
                return keys[c]
        raise SystemExit(
            f"{path}: need a {candidates[0]} column, found {sorted(keys)}"
        )

    ts_col = column("gmt time", "timestamp", "time", "date", "datetime")
    o, h, l, c = (column(n) for n in ("open", "high", "low", "close"))

    bars = []
    for row in rows:
        try:
            bar = Bar(
                ts=_parse_timestamp(row[ts_col]),
                open=float(row[o]),
                high=float(row[h]),
                low=float(row[l]),
                close=float(row[c]),
            )
        except (ValueError, KeyError):
            continue  # skip malformed / weekend gap rows
        if bar.high >= bar.low > 0:
            bars.append(bar)

    bars.sort(key=lambda b: b.ts)
    return bars


def slice_bars(
    bars: list[Bar], since: str | None = None, until: str | None = None, days: int | None = None
) -> list[Bar]:
    """Narrow a bar list by date. `days` counts back from the last bar, which
    is what you want when a file runs to the present and you only care about a
    recent window."""
    out = bars
    if since:
        start = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
        out = [b for b in out if b.ts >= start]
    if until:
        end = datetime.fromisoformat(until).replace(tzinfo=timezone.utc)
        out = [b for b in out if b.ts <= end]
    if days and out:
        cutoff = out[-1].ts - timedelta(days=days)
        out = [b for b in out if b.ts >= cutoff]
    return out


# ── Replay ────────────────────────────────────────────────────────────────


def collect_signals(
    bars: list[Bar], symbol: str, stats: dict | None = None
) -> list[Trade]:
    """Replay bars through the live detector and collect every signal it fires.

    Mirrors scanner_loop: one price sample per bar close, history capped at
    200, and a signal check only on every 5th sample while the session allows
    trading.

    Pass `stats` to have the funnel counts filled in — which stage rejected how
    many windows. A run that produces no trades is usually a threshold problem,
    and the funnel is what tells you which threshold.
    """
    real_time = Main.time
    clock = _Clock()
    Main.time = clock

    Main.price_history[symbol] = []
    Main.last_signal_time.clear()
    Main.last_signal_dir.clear()

    counts = {
        "checks": 0,
        "session_allowed": 0,
        "enough_history": 0,
        "swept_a_level": 0,
        "sweep_met_minimum": 0,
        "signals": 0,
    }
    ps = Main.pip_size(symbol)
    min_sweep = Main.MIN_SWEEP_PIPS.get(symbol, 10.0)

    trades: list[Trade] = []
    try:
        for index, bar in enumerate(bars):
            clock.now = bar.ts.timestamp()
            session = Main.get_session(bar.ts.hour)

            history = Main.price_history[symbol]
            history.append(bar.close)
            if len(history) > 200:
                Main.price_history[symbol] = history[-200:]

            if index % SAMPLES_PER_SIGNAL_CHECK:
                continue
            counts["checks"] += 1

            if not Main.is_signal_allowed(session):
                continue
            counts["session_allowed"] += 1

            history = Main.price_history[symbol]
            if len(history) >= 20:
                counts["enough_history"] += 1
                recent, lookback = history[-5:], history[-20:-5]
                highest = (max(recent) - max(lookback)) / ps
                lowest = (min(lookback) - min(recent)) / ps
                if highest > 0 or lowest > 0:
                    counts["swept_a_level"] += 1
                if highest >= min_sweep or lowest >= min_sweep:
                    counts["sweep_met_minimum"] += 1

            signal = Main.detect_crt_signal(symbol, bar.close, session)
            if signal is None:
                continue
            counts["signals"] += 1

            trades.append(
                Trade(
                    symbol=signal.symbol,
                    direction=signal.direction,
                    entry=signal.entry,
                    sl=signal.sl,
                    tp=signal.tp,
                    session=signal.session,
                    opened_at=bar.ts,
                )
            )
            trades[-1]._open_index = index  # type: ignore[attr-defined]
    finally:
        Main.time = real_time

    if stats is not None:
        stats.update(counts)

    return trades


def resolve(
    trade: Trade,
    bars: list[Bar],
    start_index: int,
    timeout_bars: int = DEFAULT_TIMEOUT_BARS,
) -> Trade:
    """Walk forward until SL or TP is touched.

    When a single bar touches both, the stop is taken — the conservative
    tie-break, since a minute bar gives no intrabar ordering.
    """
    for bar in bars[start_index + 1 : start_index + 1 + timeout_bars]:
        if trade.direction == "BUY":
            hit_sl = bar.low <= trade.sl
            hit_tp = bar.high >= trade.tp
        else:
            hit_sl = bar.high >= trade.sl
            hit_tp = bar.low <= trade.tp

        if hit_sl:
            trade.outcome, trade.exit_price, trade.closed_at = "SL", trade.sl, bar.ts
            return trade
        if hit_tp:
            trade.outcome, trade.exit_price, trade.closed_at = "TP", trade.tp, bar.ts
            return trade

    last = bars[min(start_index + timeout_bars, len(bars) - 1)]
    trade.outcome, trade.exit_price, trade.closed_at = "TIMEOUT", last.close, last.ts
    return trade


def realised_r(trade: Trade, cost_per_leg: float = 0.0) -> float:
    """Return in R after execution cost.

    Entry is a market order and always pays a leg. A stop-loss exit becomes a
    market order and pays another. A take-profit exit is a limit fill and pays
    nothing — the same asymmetric model used in the earlier gold stress test.
    """
    if trade.exit_price is None:
        raise ValueError("trade is not resolved")

    if trade.direction == "BUY":
        entry = trade.entry + cost_per_leg
        exit_price = trade.exit_price - (cost_per_leg if trade.outcome == "SL" else 0.0)
        gross = exit_price - entry
    else:
        entry = trade.entry - cost_per_leg
        exit_price = trade.exit_price + (cost_per_leg if trade.outcome == "SL" else 0.0)
        gross = entry - exit_price

    return gross / trade.risk if trade.risk else 0.0


def summarise(trades: list[Trade], cost_per_leg: float = 0.0) -> dict:
    results = [realised_r(t, cost_per_leg) for t in trades]
    wins = [r for r in results if r > 0]
    losses = [r for r in results if r <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        "signals": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "total_r": sum(results),
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "max_drawdown_r": _max_drawdown(results),
    }


def _max_drawdown(results: list[float]) -> float:
    peak = equity = 0.0
    worst = 0.0
    for r in results:
        equity += r
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


# ── Reports ───────────────────────────────────────────────────────────────


def geometry_report(prices: dict[str, float] | None = None) -> str:
    """How far price must travel to reach the configured SL and TP.

    Needs no market data: SL_PIPS/TP_PIPS and pip_size fully determine it.
    """
    prices = prices or REFERENCE_PRICES
    lines = [
        "CRT target geometry — distance to SL/TP as a share of price",
        "",
        f"{'Symbol':<10}{'Ref price':>12}{'SL':>10}{'TP':>10}{'SL %':>9}{'TP %':>9}{'RR':>7}",
        "-" * 67,
    ]
    for symbol, price in prices.items():
        ps = Main.pip_size(symbol)
        sl = Main.SL_PIPS.get(symbol, 50) * ps
        tp = Main.TP_PIPS.get(symbol, 150) * ps
        lines.append(
            f"{symbol:<10}{price:>12,.2f}{sl:>10,.2f}{tp:>10,.2f}"
            f"{sl / price * 100:>8.2f}%{tp / price * 100:>8.2f}%{tp / sl:>7.1f}"
        )
    lines += [
        "",
        "TP % is the move required for a win. Compare it against the",
        "instrument's typical range over the holding period before reading",
        "anything into a win rate.",
    ]
    return "\n".join(lines)


def _funnel_lines(stats: dict, symbol: str) -> list[str]:
    min_sweep = Main.MIN_SWEEP_PIPS.get(symbol, 10.0)
    labels = [
        ("checks", "signal checks (every 5th minute)"),
        ("session_allowed", "in an allowed session"),
        ("enough_history", "with 20+ samples of history"),
        ("swept_a_level", "that swept a prior high or low"),
        ("sweep_met_minimum", f"where the sweep cleared {min_sweep:g} pips"),
        ("signals", "that became a signal"),
    ]
    out = ["Signal funnel", "-" * 52]
    for key, label in labels:
        out.append(f"{stats.get(key, 0):>10,}  {label}")
    return out


def run_report(bars: list[Bar], symbol: str, spread: float, slippage: float) -> str:
    stats: dict = {}
    trades = collect_signals(bars, symbol, stats)
    for trade in trades:
        resolve(trade, bars, trade._open_index)  # type: ignore[attr-defined]

    span = f"{bars[0].ts:%Y-%m-%d} → {bars[-1].ts:%Y-%m-%d}" if bars else "no data"
    lines = [
        f"CRT backtest — {symbol}",
        f"{len(bars):,} bars   {span}",
        "",
    ]
    lines += _funnel_lines(stats, symbol)
    lines.append("")

    if not trades:
        lines.append(
            "No signals fired. The funnel above shows which stage rejected the\n"
            "windows — usually MIN_SWEEP_PIPS, or the requirement that price\n"
            "sweep a level and recover back past it inside the same 5 samples."
        )
        return "\n".join(lines)

    outcomes = {o: sum(1 for t in trades if t.outcome == o) for o in ("TP", "SL", "TIMEOUT")}
    lines.append(f"Outcomes: {outcomes['TP']} TP · {outcomes['SL']} SL · {outcomes['TIMEOUT']} timed out")
    lines.append("")
    lines.append(f"{'Cost scenario':<28}{'Signals':>9}{'Win%':>8}{'Total R':>10}{'PF':>8}{'MaxDD':>9}")
    lines.append("-" * 72)

    scenarios = [("No cost (baseline)", 0.0)]
    leg = spread + slippage
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
            f"{label:<28}{s['signals']:>9}{s['win_rate']:>7.1f}%"
            f"{s['total_r']:>10.1f}{pf:>8}{s['max_drawdown_r']:>9.1f}"
        )

    breakeven = _breakeven_cost(trades)
    lines += [
        "",
        f"Break-even cost: {breakeven:.4f} per leg"
        if breakeven is not None
        else "Break-even cost: never profitable, even at zero cost",
        "",
        "Costs are applied per the live fill model: entry is a market order and",
        "always pays a leg, a stop-loss exit pays another, a take-profit exit is",
        "a limit fill and pays nothing.",
    ]
    return "\n".join(lines)


def _breakeven_cost(trades: list[Trade], limit: float = 100.0) -> float | None:
    """Largest per-leg cost at which total R is still positive."""
    if summarise(trades, 0.0)["total_r"] <= 0:
        return None
    low, high = 0.0, limit
    for _ in range(60):
        mid = (low + high) / 2
        if summarise(trades, mid)["total_r"] > 0:
            low = mid
        else:
            high = mid
    return low


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("geometry", help="SL/TP distances as a share of price (no data needed)")

    run = sub.add_parser("run", help="replay an OHLC CSV through the live detector")
    run.add_argument("--csv", required=True)
    run.add_argument("--symbol", required=True, choices=sorted(Main.SYMBOLS))
    run.add_argument("--since", help="ISO date, inclusive")
    run.add_argument("--until", help="ISO date, inclusive")
    run.add_argument("--days", type=int, help="window counting back from the last bar")
    run.add_argument("--spread", type=float, default=0.25, help="per leg, price units")
    run.add_argument("--slippage", type=float, default=0.10, help="per leg, price units")

    args = parser.parse_args(argv)

    if args.command == "geometry":
        print(geometry_report())
        return 0

    bars = slice_bars(load_bars(args.csv), args.since, args.until, args.days)
    if not bars:
        raise SystemExit('no bars in that window')
    print(run_report(bars, args.symbol, args.spread, args.slippage))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
