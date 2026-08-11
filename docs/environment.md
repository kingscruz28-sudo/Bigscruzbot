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
| `ANTHROPIC_API_KEY` | empty | chart scan, chat fallback | Preferred chart reader. If it is unset or out of credit, scans fall back to Groq instead of failing. |
| `GROQ_VISION_MODEL` | `qwen/qwen3.6-27b` | `scan_chart_image` | Comma-separated; first model that answers wins. **Does not need setting** — the default is the model Groq's vision docs name, and if it is ever retired the bot discovers a replacement itself (below). |
| `CHAT_MODEL` | `claude-opus-5` | `ask_anthropic` | Override without a code change. |
| `CHART_SCAN_MODEL` | `claude-opus-5` | `scan_chart_image` | Override without a code change. Set to `claude-haiku-4-5` to cut scan cost roughly fivefold. |

### The vision model is self-healing

Pinning model names is what broke this feature once already — two Llama 4
vision models were hardcoded, Groq retired both, and the scan had nothing
left to try.

So once every configured model has failed, the bot calls
`GET /openai/v1/models`, filters the listing down to plausible image readers,
and tries up to three of them. Speech, TTS, moderation, embedding and
`compound` models are excluded outright — a chart sent to Whisper is a
guaranteed wasted upload. Anything whose metadata advertises image input is
ranked ahead of the guesses.

Discovery runs **only after** the configured list is spent, so a working scan
is still a single request, and the result is cached for the life of the
process. A failed discovery is not fatal; the scan still reports why it
could not read the chart.

### Reasoning models and the token budget

`qwen/qwen3.6-27b` thinks in a `<think>` block before answering, and those
tokens come out of the same `max_tokens` as the reply. At 500 it spent the
whole budget thinking and the answer was truncated mid-sentence, so the scan
now asks for 2000 and sends `reasoning_format: hidden`.

Models that do not accept that parameter return HTTP 400; the request is
retried once without it and `strip_reasoning()` removes the tags locally.
An *unclosed* `<think>` is treated as a failure rather than a read — it means
the model ran out of room mid-thought, so there is no analysis to show.

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

## Commitment of Traders

| Variable | Default | Used by | Notes |
|---|---|---|---|
| `COT_MARKET_CODE` | `088691` | `/cot` | CFTC contract code. The default is COMEX Gold. `/cot <code>` overrides it per call without changing this. |

The CFTC feed is open — no key, no account, no quota to manage. Rows come back
from Socrata with every number as a *string*, and empty columns omitted rather
than sent as null, so the parsing coerces defensively.

The report is measured on a Tuesday and published the following Friday, so it
is structurally a few days behind price. It is positioning context for a level
you already have, not an entry trigger, and the command's output says so.
