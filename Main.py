import os
import time
import asyncio
import logging
import threading
import requests
import feedparser
import anthropic
from datetime import datetime
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
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CHAT_ID           = int(os.environ["CHAT_ID"])
ER_API_KEY        = os.environ["ER_API_KEY"]

# ── Anthropic Client ──────────────────────────────────────────────────────────
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Markets ───────────────────────────────────────────────────────────────────
SYMBOLS = {
    "XAU/USD": "Gold",
    "ETH/USD": "Ethereum",
    "USD/JPY": "USD/JPY",
    "SOL/USD": "Solana",
    "BTC/USD": "Bitcoin",
}

# ── State ─────────────────────────────────────────────────────────────────────
price_history: dict[str, list[float]] = {s: [] for s in SYMBOLS}
last_prices: dict[str, float] = {}
sent_news_urls: set[str] = set()

_event_loop: asyncio.AbstractEventLoop | None = None
_bot_ref = None

# ─────────────────────────────────────────────────────────────────────────────
# SAFE SEND
# ─────────────────────────────────────────────────────────────────────────────


def safe_send(text: str):
    if _event_loop is None or _bot_ref is None:
        log.warning("safe_send called before bot is ready")
        return
    asyncio.run_coroutine_threadsafe(
        _bot_ref.send_message(chat_id=CHAT_ID, text=text),
        _event_loop,
    )


# ─────────────────────────────────────────────────────────────────────────────
# PRICE FETCHING
# ─────────────────────────────────────────────────────────────────────────────


def fetch_crypto_prices() -> dict[str, float | None]:
    """CoinGecko free API — ETH, SOL, BTC."""
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


def fetch_forex_gold() -> dict[str, float | None]:
    """
    Single ER API call gives us both USD/JPY and Gold.
    Response includes conversion_rates with JPY and XAU.
    Gold price = 1 / XAU_rate  (since XAU rate = troy oz per USD)
    """
    results: dict[str, float | None] = {"USD/JPY": None, "XAU/USD": None}
    try:
        r = requests.get(
            f"https://v6.exchangerate-api.com/v6/{ER_API_KEY}/latest/USD",
            timeout=10,
        )
        data = r.json()
        if data.get("result") == "success":
            rates = data["conversion_rates"]

            # USD/JPY direct
            jpy = rates.get("JPY")
            if jpy:
                results["USD/JPY"] = float(jpy)
                log.info(f"USD/JPY from ER API: {results['USD/JPY']}")

            # Gold: XAU rate is oz-per-USD, so flip it to get USD-per-oz
            xau = rates.get("XAU")
            if xau and xau > 0:
                results["XAU/USD"] = round(1.0 / float(xau), 2)
                log.info(f"XAU/USD from ER API: {results['XAU/USD']}")
        else:
            log.warning(f"ER API bad response: {data.get('result')}")
    except Exception as e:
        log.error(f"ER API error: {e}")
    return results


def fetch_all_prices() -> dict[str, float | None]:
    return {**fetch_crypto_prices(), **fetch_forex_gold()}


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL DETECTION
# ─────────────────────────────────────────────────────────────────────────────


def pip_size(symbol: str) -> float:
    if "JPY" in symbol:
        return 0.01
    if symbol == "XAU/USD":
        return 0.10
    if symbol in ("ETH/USD", "SOL/USD", "BTC/USD"):
        return 1.0
    return 0.0001


def detect_crt_signal(symbol: str, price: float) -> str | None:
    history = price_history[symbol]
    if len(history) < 20:
        return None

    recent   = history[-5:]
    lookback = history[-20:-5]
    prev_high = max(lookback)
    prev_low  = min(lookback)
    curr_high = max(recent)
    curr_low  = min(recent)
    ps   = pip_size(symbol)
    name = SYMBOLS[symbol]

    if curr_high > prev_high and price < prev_high:
        entry = price
        sl = curr_high + (50 * ps)
        tp = entry - (150 * ps)
        return (
            f"SELL SIGNAL  {name}\n"
            f"High swept: {prev_high:.4f}\n"
            f"Entry: {entry:.4f}  SL: {sl:.4f}  TP: {tp:.4f}\n"
            f"150 pips target | CRT + Malaysian S/R"
        )

    if curr_low < prev_low and price > prev_low:
        entry = price
        sl = curr_low - (50 * ps)
        tp = entry + (150 * ps)
        return (
            f"BUY SIGNAL  {name}\n"
            f"Low swept: {prev_low:.4f}\n"
            f"Entry: {entry:.4f}  SL: {sl:.4f}  TP: {tp:.4f}\n"
            f"150 pips target | CRT + Malaysian S/R"
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
            f"PRICE SPIKE {arrow}  {SYMBOLS[symbol]}\n"
            f"Move: {pct:.2f}%  |  {last:.4f} to {price:.4f}"
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
    for feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                title = entry.get("title", "")
                link  = entry.get("link",  "")
                if link and link not in sent_news_urls:
                    articles.append({"title": title, "link": link})
        except Exception as e:
            log.error(f"News feed error ({feed_url}): {e}")
    return articles[:limit]


def is_market_relevant(title: str) -> bool:
    return any(kw in title.lower() for kw in MARKET_KEYWORDS)


# ─────────────────────────────────────────────────────────────────────────────
# AI
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are Jarvis, an elite AI trading assistant specialising in:\n"
    "- CRT (Candle Range Theory) High/Low sweep strategy\n"
    "- Malaysian S/R confluences\n"
    "- Forex, Gold, Crypto, and indices\n"
    "- Target: 100-150 pips minimum per trade\n\n"
    "Be concise and direct. Always give entry, SL, and TP when discussing trades."
)


def ask_jarvis(user_message: str, context_prices: dict | None = None) -> str:
    price_lines = ""
    if context_prices:
        lines = [
            f"  {SYMBOLS.get(s, s)}: {p:.4f}" for s, p in context_prices.items() if p
        ]
        price_lines = "\nCurrent prices:\n" + "\n".join(lines)
    try:
        resp = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"{user_message}{price_lines}"}],
        )
        return resp.content[0].text
    except Exception as e:
        log.error(f"Anthropic error: {e}")
        return f"Jarvis AI error: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Jarvis online.\n"
        "Watching: Gold, ETH, USD/JPY, SOL, BTC\n\n"
        "/price  - current prices\n"
        "/status - bot status\n"
        "/news   - latest market news\n"
        "/signal - scan for CRT setups\n"
        "Or ask me anything."
    )


async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prices = fetch_all_prices()
    lines = ["Current Prices:"]
    for sym, price in prices.items():
        val = f"{price:,.4f}" if price else "unavailable"
        lines.append(f"  {SYMBOLS[sym]}: {val}")
    await update.message.reply_text("\n".join(lines))


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n     = sum(1 for p in last_prices.values() if p)
    depth = len(price_history.get("XAU/USD", []))
    await update.message.reply_text(
        f"Jarvis Status\n"
        f"Active markets: {n}/{len(SYMBOLS)}\n"
        f"History depth:  {depth} candles\n"
        f"News tracked:   {len(sent_news_urls)} articles\n"
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
    prices = fetch_all_prices()
    found  = []
    for sym, price in prices.items():
        if price is None:
            continue
        price_history[sym].append(price)
        sig = detect_crt_signal(sym, price)
        if sig:
            found.append(sig)
    if found:
        for sig in found:
            await update.message.reply_text(sig)
    else:
        depth = len(price_history.get("XAU/USD", []))
        await update.message.reply_text(
            f"No CRT setups detected.\n"
            f"History: {depth}/20 candles. Markets may be ranging."
        )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prices = fetch_all_prices()
    reply  = ask_jarvis(update.message.text, prices)
    await update.message.reply_text(reply)


# ─────────────────────────────────────────────────────────────────────────────
# ERROR HANDLER
# ─────────────────────────────────────────────────────────────────────────────


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        log.warning("Conflict: old instance shutting down. Waiting 10s...")
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
    while True:
        try:
            now = datetime.utcnow().strftime("%H:%M:%S")
            log.info(f"[{now}] Scanning...")

            prices = fetch_all_prices()

            for sym, price in prices.items():
                if price is None:
                    continue

                log.info(
                    f"  [{sym}] {SYMBOLS[sym]}: {price:,.4f} "
                    f"| History: {len(price_history[sym])}"
                )

                spike = detect_spike(sym, price)
                if spike:
                    safe_send(spike)

                price_history[sym].append(price)
                if len(price_history[sym]) > 200:
                    price_history[sym] = price_history[sym][-200:]

                if loop_count % 5 == 0:
                    sig = detect_crt_signal(sym, price)
                    if sig:
                        safe_send(sig)

                last_prices[sym] = price

            if loop_count % 10 == 0:
                articles = fetch_news(3)
                for article in articles:
                    if is_market_relevant(article["title"]):
                        safe_send(
                            f"NEWS ALERT\n\n{article['title']}\n\n{article['link']}"
                        )
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

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("price",  cmd_price))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("news",   cmd_news))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)

    async def post_init(application):
        global _event_loop
        _event_loop = asyncio.get_running_loop()
        log.info("[TELEGRAM] Jarvis is ONLINE")
        log.info(f"Watching: {', '.join(SYMBOLS.values())}")

    app.post_init = post_init

    threading.Thread(target=scanner_loop, daemon=True).start()

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
