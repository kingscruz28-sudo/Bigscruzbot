import os
import time
import asyncio
import logging
import threading
import requests
import feedparser
import anthropic
from datetime import datetime, timezone
from telegram import Update
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)

# ── Environment Variables ─────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
CHAT_ID           = int(os.environ["CHAT_ID"])
ER_API_KEY        = os.environ["ER_API_KEY"]
GROQ_API_KEY      = os.environ.get("GROQ_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Anthropic Client (optional fallback) ──────────────────────────────────────
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# ── Markets ───────────────────────────────────────────────────────────────────
SYMBOLS = {
    "XAU/USD": "Gold",
    "ETH/USD": "Ethereum",
    "USD/JPY": "USD/JPY",
    "SOL/USD": "Solana",
    "BTC/USD": "Bitcoin",
}

# ── Session definitions (UTC hours) ──────────────────────────────────────────
def get_session(utc_hour: int) -> str:
    if 2 <= utc_hour < 6:
        return "ASIAN"
    elif 6 <= utc_hour < 7:
        return "ASIAN_END"
    elif 7 <= utc_hour < 13:
        return "LONDON"
    elif 13 <= utc_hour < 18:
        return "NEW_YORK"
    elif 22 <= utc_hour or utc_hour < 2:
        return "SYDNEY"
    else:
        return "DEAD"

def is_signal_allowed(session: str) -> bool:
    return session in ("ASIAN", "NEW_YORK")

# ── State ─────────────────────────────────────────────────────────────────────
price_history:  dict[str, list[float]] = {s: [] for s in SYMBOLS}
last_prices:    dict[str, float]       = {}
sent_news_urls: set[str]               = set()
last_signal_time: dict[str, float]     = {}
last_signal_dir:  dict[str, str]       = {}

SIGNAL_COOLDOWN_SECS = 1800
MIN_SWEEP_PIPS = {
    "XAU/USD": 15.0,
    "ETH/USD": 20.0,
    "USD/JPY": 0.15,
    "SOL/USD": 0.50,
    "BTC/USD": 200.0,
}

_event_loop = None
_bot_ref    = None

# ─────────────────────────────────────────────────────────────────────────────
# SAFE SEND
# ─────────────────────────────────────────────────────────────────────────────

def safe_send(text: str):
    if _event_loop is None or _bot_ref is None:
        return
    asyncio.run_coroutine_threadsafe(
        _bot_ref.send_message(chat_id=CHAT_ID, text=text),
        _event_loop,
    )

# ─────────────────────────────────────────────────────────────────────────────
# PRICE FETCHING
# ─────────────────────────────────────────────────────────────────────────────

def fetch_crypto_prices() -> dict[str, float | None]:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=ethereum,solana,bitcoin&vs_currencies=usd",
            timeout=10,
        )
        data = r.json()
        return {
            "ETH/USD": data.get("ethereum", {}).get("usd"),
            "SOL/USD": data.get("solana",   {}).get("usd"),
            "BTC/USD": data.get("bitcoin",  {}).get("usd"),
        }
    except Exception as e:
        log.error(f"CoinGecko error: {e}")
        return {"ETH/USD": None, "SOL/USD": None, "BTC/USD": None}


def fetch_gold_price() -> float | None:
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF"
            "?interval=1d&range=1d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception as e:
        log.error(f"Yahoo gold error: {e}")
        return None


def fetch_usdjpy_price() -> float | None:
    try:
        r = requests.get(
            f"https://v6.exchangerate-api.com/v6/{ER_API_KEY}/latest/USD",
            timeout=10,
        )
        data = r.json()
        if data.get("result") == "success":
            return float(data["conversion_rates"].get("JPY", 0)) or None
    except Exception as e:
        log.error(f"ER API error: {e}")
    return None


def fetch_all_prices() -> dict[str, float | None]:
    crypto = fetch_crypto_prices()
    return {
        "XAU/USD": fetch_gold_price(),
        "ETH/USD": crypto.get("ETH/USD"),
        "USD/JPY": fetch_usdjpy_price(),
        "SOL/USD": crypto.get("SOL/USD"),
        "BTC/USD": crypto.get("BTC/USD"),
    }

# ─────────────────────────────────────────────────────────────────────────────
# AI — Groq primary, Anthropic fallback
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are Jarvis, an elite AI trading assistant specialising in:\n"
    "- CRT (Candle Range Theory) High/Low sweep strategy\n"
    "- Malaysian S/R confluences\n"
    "- Change in State of Delivery (CISD)\n"
    "- Session trading: Asian sweeps (2-6am UTC), London quiet, NY entries\n"
    "- Target: 150 pips minimum per trade on Gold\n\n"
    "Be concise and direct. Always give entry, SL, and TP.\n"
    "Always mention which session is active and whether it is a good time to trade."
)


def ask_groq(user_message: str, price_ctx: str) -> str:
    """Primary AI — Groq (free, fast)."""
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": f"{user_message}{price_ctx}"},
                ],
                "max_tokens": 500,
            },
            timeout=15,
        )
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"Groq error: {e}")
        raise


def ask_anthropic(user_message: str, price_ctx: str) -> str:
    """Fallback AI — Anthropic."""
    if not ai_client:
        raise Exception("No Anthropic key configured")
    resp = ai_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"{user_message}{price_ctx}"}],
    )
    return resp.content[0].text


def ask_jarvis(user_message: str, prices: dict | None = None) -> str:
    utc_hour  = datetime.now(timezone.utc).hour
    session   = get_session(utc_hour)
    price_ctx = f"\nCurrent session: {session} (UTC {utc_hour}:00)\n"
    if prices:
        lines = [f"  {SYMBOLS.get(s, s)}: {p:.4f}" for s, p in prices.items() if p]
        price_ctx += "Current prices:\n" + "\n".join(lines)

    # Try Groq first
    if GROQ_API_KEY:
        try:
            return ask_groq(user_message, price_ctx)
        except Exception as e:
            log.warning(f"Groq failed, trying Anthropic: {e}")

    # Fall back to Anthropic
    if ANTHROPIC_API_KEY:
        try:
            return ask_anthropic(user_message, price_ctx)
        except Exception as e:
            log.error(f"Anthropic also failed: {e}")

    return "AI unavailable. Add GROQ_API_KEY to Railway variables (free at console.groq.com)"

# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def pip_size(symbol: str) -> float:
    if "JPY" in symbol:       return 0.01
    if symbol == "XAU/USD":   return 0.10
    if symbol in ("ETH/USD", "SOL/USD", "BTC/USD"): return 1.0
    return 0.0001


def cooldown_ok(symbol: str) -> bool:
    now = time.time()
    return now - last_signal_time.get(symbol, 0) >= SIGNAL_COOLDOWN_SECS


def detect_crt_signal(symbol: str, price: float, session: str) -> str | None:
    if not is_signal_allowed(session):
        return None

    history = price_history[symbol]
    if len(history) < 20:
        return None

    recent    = history[-5:]
    lookback  = history[-20:-5]
    prev_high = max(lookback)
    prev_low  = min(lookback)
    curr_high = max(recent)
    curr_low  = min(recent)
    ps        = pip_size(symbol)
    name      = SYMBOLS[symbol]
    min_sweep = MIN_SWEEP_PIPS.get(symbol, 10.0)

    # HIGH SWEEP → SELL
    sweep_high = (curr_high - prev_high) / ps
    if curr_high > prev_high and price < prev_high and sweep_high >= min_sweep:
        if cooldown_ok(symbol):
            entry = price
            sl    = curr_high + (50 * ps)
            tp    = entry - (150 * ps)
            last_signal_time[symbol] = time.time()
            last_signal_dir[symbol]  = "SELL"
            return (
                f"SELL SIGNAL  {name} [{session}]\n"
                f"High swept: {prev_high:.4f} ({sweep_high:.0f} pips)\n"
                f"Entry: {entry:.4f}  SL: {sl:.4f}  TP: {tp:.4f}\n"
                f"150 pips | CRT + Malaysian S/R"
            )

    # LOW SWEEP → BUY
    sweep_low = (prev_low - curr_low) / ps
    if curr_low < prev_low and price > prev_low and sweep_low >= min_sweep:
        if cooldown_ok(symbol):
            entry = price
            sl    = curr_low - (50 * ps)
            tp    = entry + (150 * ps)
            last_signal_time[symbol] = time.time()
            last_signal_dir[symbol]  = "BUY"
            return (
                f"BUY SIGNAL  {name} [{session}]\n"
                f"Low swept: {prev_low:.4f} ({sweep_low:.0f} pips)\n"
                f"Entry: {entry:.4f}  SL: {sl:.4f}  TP: {tp:.4f}\n"
                f"150 pips | CRT + Malaysian S/R"
            )

    return None


def detect_spike(symbol: str, price: float) -> str | None:
    last = last_prices.get(symbol)
    if last is None:
        return None
    pct = abs(price - last) / last * 100
    if pct >= 1.0:
        arrow = "UP" if price > last else "DOWN"
        return (
            f"SPIKE {arrow}  {SYMBOLS[symbol]}\n"
            f"{pct:.2f}% move  |  {last:.4f} to {price:.4f}"
        )
    return None

# ─────────────────────────────────────────────────────────────────────────────
# NEWS
# ─────────────────────────────────────────────────────────────────────────────

NEWS_FEEDS = [
    "https://feeds.investinglive.com/investinglive/news",
    "https://www.forexlive.com/feed/news",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
]
MARKET_KEYWORDS = [
    "gold", "bitcoin", "btc", "ethereum", "eth", "solana",
    "usd/jpy", "dollar", "yen", "fed", "fomc", "inflation",
    "interest rate", "oil", "crypto", "forex", "market",
    "trump", "iran", "war", "sanctions", "tariff", "recession",
]


def fetch_news(limit: int = 5) -> list[dict]:
    articles = []
    for url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                title = entry.get("title", "")
                link  = entry.get("link",  "")
                if link and link not in sent_news_urls:
                    articles.append({"title": title, "link": link})
        except Exception as e:
            log.error(f"News error: {e}")
    return articles[:limit]


def is_market_relevant(title: str) -> bool:
    return any(kw in title.lower() for kw in MARKET_KEYWORDS)

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    utc_hour = datetime.now(timezone.utc).hour
    session  = get_session(utc_hour)
    await update.message.reply_text(
        f"Jarvis online.\n"
        f"Current session: {session}\n\n"
        f"Signal zones:\n"
        f"  ASIAN 02:00-06:00 UTC = ACTIVE\n"
        f"  LONDON 07:00-13:00 UTC = QUIET\n"
        f"  NEW YORK 13:00-18:00 UTC = ACTIVE\n\n"
        f"/price   - current prices\n"
        f"/status  - bot status\n"
        f"/news    - market news\n"
        f"/signal  - manual CRT scan\n"
        f"/session - current session info\n"
        f"Or ask me anything."
    )


async def cmd_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    utc_hour = datetime.now(timezone.utc).hour
    session  = get_session(utc_hour)
    advice = {
        "ASIAN":     "ACTIVE - Asian session. Watch for Sydney high sweeps. Clean entries.",
        "ASIAN_END": "TRANSITION - Asian ending. Be cautious.",
        "LONDON":    "QUIET MODE - London chop. No new entries. Manage existing trades only.",
        "NEW_YORK":  "ACTIVE - NY session. Secondary entries if trend confirms.",
        "SYDNEY":    "WATCH ONLY - Sydney setting up highs for Asian sweep.",
        "DEAD":      "DEAD ZONE - No trading. Rest.",
    }
    await update.message.reply_text(
        f"Session: {session}\n"
        f"UTC: {utc_hour:02d}:00\n\n"
        f"{advice.get(session, 'Unknown')}"
    )


async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prices = fetch_all_prices()
    lines  = ["Current Prices:"]
    for sym, price in prices.items():
        val = f"{price:,.4f}" if price else "unavailable"
        lines.append(f"  {SYMBOLS[sym]}: {val}")
    await update.message.reply_text("\n".join(lines))


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    utc_hour = datetime.now(timezone.utc).hour
    session  = get_session(utc_hour)
    n        = sum(1 for p in last_prices.values() if p)
    depth    = len(price_history.get("XAU/USD", []))
    ai_status = "Groq" if GROQ_API_KEY else ("Anthropic" if ANTHROPIC_API_KEY else "NONE")
    await update.message.reply_text(
        f"Jarvis Status\n"
        f"Session: {session}\n"
        f"AI: {ai_status}\n"
        f"Markets: {n}/{len(SYMBOLS)}\n"
        f"History: {depth} candles\n"
        f"Status: RUNNING"
    )


async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    articles = fetch_news(5)
    if not articles:
        await update.message.reply_text("No new market news right now.")
        return
    lines = ["Latest Market News:"]
    for a in articles:
        lines.append(f"\n- {a['title']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    utc_hour = datetime.now(timezone.utc).hour
    session  = get_session(utc_hour)

    if not is_signal_allowed(session):
        await update.message.reply_text(
            f"Session: {session}\n"
            f"No signals during this session.\n"
            f"Active windows: Asian 02:00-06:00 UTC or NY 13:00-18:00 UTC"
        )
        return

    prices = fetch_all_prices()
    found  = []
    for sym, price in prices.items():
        if price is None:
            continue
        price_history[sym].append(price)
        sig = detect_crt_signal(sym, price, session)
        if sig:
            found.append(sig)

    if found:
        for sig in found:
            await update.message.reply_text(sig)
    else:
        depth = len(price_history.get("XAU/USD", []))
        await update.message.reply_text(
            f"Session: {session}\n"
            f"No CRT setups right now.\n"
            f"History: {depth}/20 candles."
        )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prices = fetch_all_prices()
    reply  = ask_jarvis(update.message.text, prices)
    await update.message.reply_text(reply)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        log.warning("Conflict: waiting 10s...")
        await asyncio.sleep(10)
        return
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning(f"Network blip: {err}")
        return
    log.error(f"Unhandled error: {err}", exc_info=err)

# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND SCANNER
# ─────────────────────────────────────────────────────────────────────────────

def scanner_loop():
    while _event_loop is None:
        time.sleep(1)

    loop_count = 0
    prev_session = None

    while True:
        try:
            utc_hour = datetime.now(timezone.utc).hour
            session  = get_session(utc_hour)
            now      = datetime.utcnow().strftime("%H:%M:%S")

            log.info(f"[{now}] Session: {session}")

            # Session change notification
            if session != prev_session and prev_session is not None:
                messages = {
                    "ASIAN":    "ASIAN SESSION OPEN\nWatch for Sydney high sweeps. Gold focus. Clean entries.",
                    "NEW_YORK": "NEW YORK SESSION OPEN\nSecondary entries. Confirm trend before entering.",
                    "LONDON":   "LONDON SESSION\nQuiet mode ON. No new signals. Manage existing trades only.",
                    "DEAD":     "Markets closing. Rest up.",
                }
                msg = messages.get(session)
                if msg:
                    safe_send(msg)
            prev_session = session

            prices = fetch_all_prices()

            for sym, price in prices.items():
                if price is None:
                    continue

                log.info(f"  [{sym}] {SYMBOLS[sym]}: {price:,.4f} | {session}")

                spike = detect_spike(sym, price)
                if spike:
                    safe_send(spike)

                price_history[sym].append(price)
                if len(price_history[sym]) > 200:
                    price_history[sym] = price_history[sym][-200:]

                if loop_count % 5 == 0 and is_signal_allowed(session):
                    sig = detect_crt_signal(sym, price, session)
                    if sig:
                        safe_send(sig)

                last_prices[sym] = price

            if loop_count % 10 == 0:
                articles = fetch_news(3)
                for article in articles:
                    if is_market_relevant(article["title"]):
                        safe_send(f"NEWS\n\n{article['title']}\n{article['link']}")
                        sent_news_urls.add(article["link"])

            loop_count += 1

        except Exception as e:
            log.error(f"Scanner error: {e}")

        time.sleep(60)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    global _event_loop, _bot_ref

    log.info("JARVIS STARTING...")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    _bot_ref = app.bot

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("price",   cmd_price))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("news",    cmd_news))
    app.add_handler(CommandHandler("signal",  cmd_signal))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    async def post_init(application):
        global _event_loop
        _event_loop = asyncio.get_running_loop()
        log.info("[TELEGRAM] Jarvis is ONLINE")

    app.post_init = post_init

    threading.Thread(target=scanner_loop, daemon=True).start()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
