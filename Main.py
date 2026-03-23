import os
import time
import logging
import threading
import requests
import feedparser
import anthropic
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── Environment Variables ─────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TWELVE_API_KEY   = os.environ["TWELVE_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CHAT_ID          = int(os.environ["CHAT_ID"])

# ── Anthropic Client ──────────────────────────────────────────────────────────
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Markets to Watch ──────────────────────────────────────────────────────────
SYMBOLS = {
    "XAU/USD": "Gold",
    "ETH/USD": "Ethereum",
    "USD/JPY": "USD/JPY",
    "SOL/USD": "Solana",
    "XBT/USD": "Bitcoin",
}

TWELVE_SYMBOLS = {
    "XAU/USD": "XAU/USD",
    "ETH/USD": "ETH/USD",
    "USD/JPY": "USD/JPY",
    "SOL/USD": "SOL/USD",
    "XBT/USD": "BTC/USD",   # Twelve Data uses BTC/USD
}

# ── State ─────────────────────────────────────────────────────────────────────
price_history: dict[str, list[float]] = {s: [] for s in SYMBOLS}
last_prices:   dict[str, float]       = {}
sent_news_urls: set[str]              = set()
app_ref = None   # set after build

# ─────────────────────────────────────────────────────────────────────────────
# PRICE FETCHING
# ─────────────────────────────────────────────────────────────────────────────

def fetch_price(symbol: str) -> float | None:
    twelve_sym = TWELVE_SYMBOLS.get(symbol, symbol)
    url = (
        f"https://api.twelvedata.com/price"
        f"?symbol={twelve_sym}&apikey={TWELVE_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if "price" in data:
            return float(data["price"])
        log.warning(f"No price for {symbol}: {data}")
    except Exception as e:
        log.error(f"fetch_price {symbol}: {e}")
    return None


def fetch_all_prices() -> dict[str, float | None]:
    return {sym: fetch_price(sym) for sym in SYMBOLS}

# ─────────────────────────────────────────────────────────────────────────────
# CRT SIGNAL DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_crt_signal(symbol: str, price: float) -> str | None:
    """
    Simple CRT (Candle Range Theory) High/Low sweep detection.
    Needs at least 20 data points. Returns signal string or None.
    """
    history = price_history[symbol]
    if len(history) < 20:
        return None

    recent   = history[-5:]
    lookback = history[-20:-5]

    prev_high = max(lookback)
    prev_low  = min(lookback)
    curr_high = max(recent)
    curr_low  = min(recent)

    sweep_high = curr_high > prev_high and price < prev_high
    sweep_low  = curr_low  < prev_low  and price > prev_low

    pip_size = 0.0001 if "JPY" not in symbol else 0.01
    if symbol in ("XAU/USD",):
        pip_size = 0.1
    if symbol in ("ETH/USD", "SOL/USD", "XBT/USD"):
        pip_size = 1.0

    if sweep_high:
        entry  = price
        sl     = curr_high + (50 * pip_size)
        tp     = entry - (150 * pip_size)
        return (
            f"🔻 CRT SELL SIGNAL — {SYMBOLS[symbol]}\n"
            f"High swept: {prev_high:.4f}\n"
            f"Entry: {entry:.4f}\n"
            f"SL:    {sl:.4f}\n"
            f"TP:    {tp:.4f} (150 pips)\n"
            f"Strategy: CRT High Sweep + Malaysian S/R"
        )

    if sweep_low:
        entry  = price
        sl     = curr_low  - (50 * pip_size)
        tp     = entry + (150 * pip_size)
        return (
            f"🟢 CRT BUY SIGNAL — {SYMBOLS[symbol]}\n"
            f"Low swept: {prev_low:.4f}\n"
            f"Entry: {entry:.4f}\n"
            f"SL:    {sl:.4f}\n"
            f"TP:    {tp:.4f} (150 pips)\n"
            f"Strategy: CRT Low Sweep + Malaysian S/R"
        )

    return None


def detect_spike(symbol: str, price: float) -> str | None:
    """Alert if price moves 1%+ from last recorded price."""
    last = last_prices.get(symbol)
    if last is None:
        return None
    change_pct = abs(price - last) / last * 100
    if change_pct >= 1.0:
        direction = "📈" if price > last else "📉"
        return (
            f"{direction} PRICE SPIKE — {SYMBOLS[symbol]}\n"
            f"Move: {change_pct:.2f}%\n"
            f"From: {last:.4f} → {price:.4f}"
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
                title   = entry.get("title", "")
                link    = entry.get("link", "")
                summary = entry.get("summary", "")[:200]
                if link and link not in sent_news_urls:
                    articles.append({
                        "title":   title,
                        "link":    link,
                        "summary": summary,
                    })
        except Exception as e:
            log.error(f"News fetch error ({feed_url}): {e}")
    return articles[:limit]


def is_market_relevant(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in MARKET_KEYWORDS)

# ─────────────────────────────────────────────────────────────────────────────
# AI CHAT
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Jarvis, an elite AI trading assistant specialising in:
- CRT (Candle Range Theory) High/Low sweep strategy
- Malaysian S/R (Support & Resistance) confluences
- Forex, Gold, Crypto, and indices
- Target: 100-150 pips minimum per trade

Always be concise, confident, and focused on trade setups.
When asked what to buy/trade, analyse current market conditions and give a direct recommendation with entry, SL, and TP.
Never give financial advice disclaimers — the user knows this is for informational purposes."""


def ask_jarvis(user_message: str, context_prices: dict | None = None) -> str:
    price_context = ""
    if context_prices:
        lines = [f"  {SYMBOLS.get(s, s)}: {p:.4f}" for s, p in context_prices.items() if p]
        price_context = "\nCurrent prices:\n" + "\n".join(lines)

    try:
        response = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": f"{user_message}{price_context}"}
            ],
        )
        return response.content[0].text
    except Exception as e:
        log.error(f"Anthropic error: {e}")
        return f"Jarvis error: {e}"

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Jarvis online.\n"
        "Watching: Gold, ETH, USD/JPY, SOL, BTC\n\n"
        "Commands:\n"
        "/price  — current prices\n"
        "/status — bot status\n"
        "/news   — latest market news\n"
        "/signal — scan for CRT setups\n"
        "Or just ask me anything."
    )


async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prices = fetch_all_prices()
    lines = ["Current Prices:"]
    for sym, price in prices.items():
        name = SYMBOLS[sym]
        val  = f"{price:,.4f}" if price else "unavailable"
        lines.append(f"  {name}: {val}")
    await update.message.reply_text("\n".join(lines))


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n_prices = sum(1 for p in last_prices.values() if p)
    await update.message.reply_text(
        f"Jarvis Status\n"
        f"Active markets: {n_prices}/{len(SYMBOLS)}\n"
        f"History depth:  {len(price_history.get('XAU/USD', []))} candles\n"
        f"News seen:      {len(sent_news_urls)} articles\n"
        f"Uptime: running"
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
    signals_found = []
    for sym, price in prices.items():
        if price is None:
            continue
        price_history[sym].append(price)
        sig = detect_crt_signal(sym, price)
        if sig:
            signals_found.append(sig)

    if signals_found:
        for sig in signals_found:
            await update.message.reply_text(sig)
    else:
        await update.message.reply_text(
            "No CRT setups detected right now.\n"
            "Still building price history or markets are ranging."
        )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    prices = fetch_all_prices()
    reply = ask_jarvis(text, prices)
    await update.message.reply_text(reply)

# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND SCANNER
# ─────────────────────────────────────────────────────────────────────────────

def scanner_loop(app):
    """Runs in background thread: scans prices + news every 60 seconds."""
    loop_count = 0
    while True:
        try:
            now = datetime.utcnow().strftime("%H:%M:%S")
            log.info(f"[{now}] Scanning...")

            prices = fetch_all_prices()

            for sym, price in prices.items():
                if price is None:
                    continue

                name = SYMBOLS[sym]
                score = len(price_history[sym])
                log.info(f"  [{sym}] {name}: {price:,.4f} | History: {score}")

                # Spike check
                spike = detect_spike(sym, price)
                if spike:
                    app.create_task(
                        app.bot.send_message(chat_id=CHAT_ID, text=spike)
                    )

                # Update history
                price_history[sym].append(price)
                if len(price_history[sym]) > 200:
                    price_history[sym] = price_history[sym][-200:]

                # CRT signal check (every 5 scans = ~5 mins)
                if loop_count % 5 == 0:
                    sig = detect_crt_signal(sym, price)
                    if sig:
                        app.create_task(
                            app.bot.send_message(chat_id=CHAT_ID, text=sig)
                        )

                last_prices[sym] = price

            # News check every 10 scans (~10 mins)
            if loop_count % 10 == 0:
                articles = fetch_news(3)
                for article in articles:
                    if is_market_relevant(article["title"]):
                        msg = (
                            f"📰 NEWS ALERT\n\n"
                            f"{article['title']}\n\n"
                            f"{article['link']}"
                        )
                        app.create_task(
                            app.bot.send_message(chat_id=CHAT_ID, text=msg)
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
    log.info("JARVIS STARTING...")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("price",  cmd_price))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("news",   cmd_news))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Start background scanner in a daemon thread
    scanner_thread = threading.Thread(
        target=scanner_loop, args=(app,), daemon=True
    )
    scanner_thread.start()

    log.info("[TELEGRAM] Jarvis is ONLINE")
    log.info(f"Watching: {', '.join(SYMBOLS.values())}")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
