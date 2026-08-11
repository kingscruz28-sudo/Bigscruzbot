# Railway environment variables

Every variable `Main.py` actually reads, what reads it, and what happens if it
is missing. Anything not on this list has no effect on the bot.

## Required — the bot will not start without these

| Variable | Used by | Notes |
|---|---|---|
| `TELEGRAM_TOKEN` | `main()` | Read with `os.environ[...]`, so a missing value is a hard crash at import, not a warning. |
| `CHAT_ID` | `safe_send` | Same. Must parse as an integer. |
| `ER_API_KEY` | `fetch_usdjpy_price` | Same. exchangerate-api.com key. |

## AI

| Variable | Default | Used by | Notes |
|---|---|---|---|
| `GROQ_API_KEY` | empty | `ask_jarvis` | **Primary** chat provider. While this is set, Anthropic is only the fallback. Clear it to route chat to Claude. |
| `ANTHROPIC_API_KEY` | empty | chart scan, chat fallback | Chart scanning has no fallback — if this is unset or the account is out of credit, `/photo` scans fail outright. |
| `CHAT_MODEL` | `claude-opus-5` | `ask_anthropic` | Override without a code change. |
| `CHART_SCAN_MODEL` | `claude-opus-5` | `scan_chart_image` | Override without a code change. Set to `claude-haiku-4-5` to cut scan cost roughly fivefold. |

### Credit exhaustion looks like a key problem but is not

A spent balance returns HTTP 400 with:

```
invalid_request_error — Your credit balance is too low to access the Anthropic API
```

A bad key returns `authentication_error` instead. If the message mentions
credit, the key is fine and rotating it changes nothing — top up, or point
`CHART_SCAN_MODEL` at a cheaper model.

### A Claude Pro or Max subscription is not API credit

Two separate wallets, and paying one never funds the other:

| | Covers | Billed at |
|---|---|---|
| Claude Pro / Max | claude.ai, the apps, Claude Code | Flat monthly subscription |
| API credit | `ANTHROPIC_API_KEY`, so everything this bot does | Pay-as-you-go, console.anthropic.com |

The key is tied to one console account, and that account's balance is what
gets spent. A subscription on a different profile — or the same one — adds
nothing to it. Credit must be bought on the console account the key was
issued from, so check which account that is before topping up.

Opus 5 runs thinking by default and thinking tokens are billed, so a scan
costs several times what the same scan cost on Haiku. That is the trade for
the accuracy gain on charts; `CHART_SCAN_MODEL` is the dial if the balance
matters more than the read.

## Trading

| Variable | Default | Used by | Notes |
|---|---|---|---|
| `AUTO_TRADE` | `false` | `scanner_loop`, `execute_trade_via_bridge`, `/autotrade` | Master switch. `/autotrade on` flips it at runtime without a redeploy. |
| `MT5_BRIDGE_URL` | empty | `execute_trade_via_bridge` | Where signals are POSTed. Empty means no order ever leaves the bot, whatever `AUTO_TRADE` says. |
| `MAX_LOT` | `0.01` | payload to the bridge | |
| `RISK_PERCENT` | `0.5` | payload to the bridge | |

## Week 2

| Variable | Default | Used by |
|---|---|---|
| `WEEK2_BOT_URL` | `http://localhost:5002` | `/poly`, `/sol`, `/week2` |

`localhost` here means *the Railway container*, not the laptop, so these
commands report "Week 2 bot offline" unless the URL points somewhere Railway
can actually reach.

## Read but never used — setting these does nothing

| Variable | Why it is inert |
|---|---|
| `MT5_LOGIN` | Assigned at line 41 and never referenced again. |
| `MT5_PASSWORD` | Assigned at line 42 and never referenced again. |
| `MT5_SERVER` | Assigned at line 43 and never referenced again. |

The Railway bot never logs into MT5. It has no MT5 library and no terminal —
it POSTs a JSON payload to `MT5_BRIDGE_URL` and the bridge on the Windows
machine is what holds the broker session. Demo or live credentials belong on
that machine, not here.

They are kept rather than deleted because a native Railway-side MT5 connection
is on the roadmap; until something reads them they are documentation of intent,
not configuration.

## Not read at all

Kaggle credentials have no effect here. Nothing in the bot downloads datasets —
that is backtesting work, which runs on a machine with the data, not on the
Telegram host.
