# CRT backtest harness

Replays historical M1 bars through the **live** `detect_crt_signal` — the
detector is imported from `Main`, not reimplemented, so the backtest cannot
drift from what the bot actually trades.

## Why this lives here rather than being already run

The cloud session that wrote it has no network route to any market-data host
(Dukascopy, Binance, Yahoo and Stooq are all refused by the egress policy), so
it has never been run against real prices. Run it where the data is.

## Geometry — no data required

    python -m backtest.crt_backtest geometry

Prints how far price must move to reach the configured SL and TP for each
symbol. Read this before anything else.

## Backtest

    python -m backtest.crt_backtest run --csv XAUUSD_M1.csv --symbol XAU/USD

The CSV needs one row per minute with timestamp, open, high, low and close.
Dukascopy's `Gmt time,Open,High,Low,Close,Volume` export works unmodified, as
does any file with those column names and ISO or MT5 timestamps.

Options:

    --spread    per leg, in price units (default 0.25 — gold)
    --slippage  per leg, in price units (default 0.10)

## What it models

- One price sample per bar close, appended to `price_history`, capped at 200 —
  the same series `scanner_loop` builds.
- A signal check every 5th minute, matching `loop_count % 5`.
- Session gating and the 30-minute per-symbol cooldown, with the cooldown
  measured against bar timestamps rather than wall clock.
- Exit on whichever of SL/TP is touched first. When one bar touches both, the
  stop is taken — a minute bar carries no intrabar ordering, so this is the
  conservative reading.
- Costs on the live fill model: entry is a market order and pays a leg, a
  stop-loss exit pays another, a take-profit exit is a limit fill and pays
  nothing.

## What it does not model

- Slippage that varies with volatility; a single fixed figure is used.
- Partial fills, requotes, weekend gaps and swap.
- Overlapping positions are each treated as a full-size trade.
- `/signal` inserting extra samples into the same history the scanner uses,
  which shifts the live sweep windows in a way this replay does not reproduce.

## Reading the output

The signal funnel shows which stage rejected each window. If a run produces
few or no trades, that is the first thing to look at — the sweep minimum and
the requirement that price sweep a level *and* recover past it within the same
five samples are both demanding.
