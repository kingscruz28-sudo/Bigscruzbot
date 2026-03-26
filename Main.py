import os
import time
import asyncio
import logging
import threading
import requests
import feedparser
import anthropic
import base64
import random
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

# ── Anthropic Client ──────────────────────────────────────────────────────────
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
    # Now includes LONDON (with warning tag)
    return session in ("ASIAN", "NEW_YORK", "LONDON")

# ── State ─────────────────────────────────────────────────────────────────────
price_history:  dict[str, list[float]] = {s: [] for s in SYMBOLS}
last_prices:    dict[str, float]       = {}
sent_news_urls: set[str]               = set()
last_signal_time: dict[str, float]     = {}
last_signal_dir:  dict[str, str]       = {}
greeted_periods:  set[str]             = set()

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
# FEATURE 1 — TIME-BASED GREETINGS
# ─────────────────────────────────────────────────────────────────────────────

def get_greeting_period(utc_hour: int) -> str:
    """Return greeting period key based on UTC hour."""
    if 5 <= utc_hour < 12:
        return "morning"
    elif 12 <= utc_hour < 17:
        return "afternoon"
    elif 17 <= utc_hour < 21:
        return "evening"
    else:
        return "night"


# ── Motivational quotes — fire these at random ───────────────────────────────
MOTIVATIONAL_QUOTES = [
    "💡 \"The market rewards patience and punishes impatience.\" — sit tight, boss.",
    "🔥 \"Discipline is doing what needs to be done, even when you don't want to.\" Trade the plan.",
    "⚡ \"Amateurs want to be right. Professionals want to make money.\" Know the difference.",
    "🎯 \"One good trade is worth more than ten rushed ones.\" Quality over quantity.",
    "🧘 \"The best trade is sometimes no trade at all.\" Dead zone = rest zone.",
    "💎 \"Protect the capital first. Profits come second.\" SL is your best friend.",
    "🌊 \"Don't fight the session. Flow with it.\" Asian sweeps are your bread and butter.",
    "🏆 \"Every loss is tuition. Every win is proof the system works.\" Keep studying.",
    "🔑 \"Consistency beats luck every single time.\" Show up every session.",
    "🚀 \"Small pips compound into life-changing money.\" 150 pips a day keeps the losses away.",
    "🦁 \"The market is a lion. Respect it and it feeds you. Disrespect it and it eats you.\"",
    "⏰ \"Timing is everything. The Asian session doesn't lie.\" 02:00 UTC is your hour.",
    "📊 \"A trading plan without discipline is just a wish list.\" Execute, boss.",
    "🌙 \"While others sleep, the Asian market sets up your next move.\" Eyes open at 02:00.",
    "💪 \"Losses don't define you. How you respond to them does.\" Reset and reload.",
    "🎲 \"Trading without a stop loss is gambling. You're not a gambler, you're a trader.\"",
    "🧠 \"Your biggest enemy in trading is between your ears.\" Stay calm, stay sharp.",
    "📈 \"The CRT sweep doesn't lie. Price always revisits liquidity.\" Trust the method.",
]

# ── Names Jarvis calls you — rotates so it never feels robotic ────────────────
BOSS_NAMES = [
    "Scruz", "Bigscruz", "BigDawg", "Boss", "Scruman",
]
_last_name_used = ""

def get_name() -> str:
    """Pick a random name, never repeat the same one twice in a row."""
    global _last_name_used
    choices = [n for n in BOSS_NAMES if n != _last_name_used]
    name = random.choice(choices)
    _last_name_used = name
    return name

# ── Boss names — Jarvis knows who he's talking to ────────────────────────────
BOSS_NAMES = [
    "Scruz", "Bigscruz", "BigDawg", "Boss", "Scruman",
]

def get_boss_name() -> str:
    return random.choice(BOSS_NAMES)


def build_greeting(utc_hour: int) -> str:
    """Build a time-appropriate greeting with session tip."""
    period = get_greeting_period(utc_hour)
    session = get_session(utc_hour)

    name = get_boss_name()

    greetings = {
        "morning": (
            f"🌅 Morning, {name}.\n"
            "Asian session winding down — sweep review time on Gold.\n"
            "London opens soon. Sit on your hands until the dust settles.\n"
            "CRT + Malaysian S/R + CISD. You already know."
        ),
        "afternoon": (
            f"☀️ Afternoon, {name}.\n"
            "NY session is live. Secondary entries — trend confirm before you commit.\n"
            "Gold and crypto on watch. 150 minimum, no exceptions.\n"
            "Trust your levels. You built them for a reason."
        ),
        "evening": (
            f"🌆 Evening, {name}.\n"
            "NY wrapping up. Manage what's open, don't start new positions.\n"
            "Asian session in a few hours — use this time to mark up your charts.\n"
            "Your groups are watching. Lead by example."
        ),
        "night": (
            f"🌙 Late night, {name}.\n"
            "Dead zone. Market's breathing — so should you.\n"
            "Asian open is 02:00 UTC. Sydney sets the highs. You know what comes next.\n"
            "Rest is part of the edge. Sharp mind, sharp entries."
        ),
    }

    quote = random.choice(MOTIVATIONAL_QUOTES)
    msg = greetings.get(period, "Jarvis online.")
    msg += f"\n\nSession: {session}\n\n{quote}"
    return msg


def check_and_send_greeting():
    """Send greeting once per period (morning/afternoon/evening/night)."""
    utc_hour = datetime.now(timezone.utc).hour
    period   = get_greeting_period(utc_hour)

    if period not in greeted_periods:
        greeted_periods.add(period)
        # Clear old periods so next day works
        if len(greeted_periods) > 4:
            greeted_periods.clear()
            greeted_periods.add(period)
        safe_send(build_greeting(utc_hour))

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
    # Try Yahoo Finance v8 first (GC=F = Gold futures)
    for ticker in ["GC%3DF", "XAUUSD%3DX", "GLD"]:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                "?interval=1d&range=1d",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            price = float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
            # Sanity check — Gold is between 1500 and 4000 as of 2024-2025
            if 1500 < price < 4000:
                log.info(f"Gold price from Yahoo ({ticker}): {price}")
                return price
            else:
                log.warning(f"Yahoo {ticker} returned suspicious price: {price}, trying next")
        except Exception as e:
            log.warning(f"Yahoo gold ({ticker}) error: {e}")

    # Fallback: frankfurter.app (free, no key, uses ECB data — XAU in oz)
    try:
        r = requests.get(
            "https://api.frankfurter.app/latest?from=XAU&to=USD",
            timeout=10,
        )
        data = r.json()
        price = float(data["rates"]["USD"])
        if 1500 < price < 4000:
            log.info(f"Gold price from frankfurter: {price}")
            return price
    except Exception as e:
        log.warning(f"Frankfurter gold error: {e}")

    # Last resort: metals-live free API
    try:
        r = requests.get(
            "https://api.metals.live/v1/spot/gold",
            timeout=10,
        )
        data = r.json()
        price = float(data[0]["price"])
        if 1500 < price < 4000:
            log.info(f"Gold price from metals.live: {price}")
            return price
    except Exception as e:
        log.error(f"All gold sources failed. Last error: {e}")

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
# FEATURE 2 — NEWS → PAIR ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

# Keyword → which pairs are affected + expected direction + pip estimate
NEWS_PAIR_MAP = {
    # Gold triggers
    "gold":         [("XAU/USD", "BUY",  "100-200 pips — safe haven demand")],
    "inflation":    [("XAU/USD", "BUY",  "80-150 pips — inflation hedge"), ("USD/JPY", "BUY", "30-60 pips")],
    "fed":          [("XAU/USD", "SELL", "80-150 pips — rate hike pressure"), ("USD/JPY", "BUY", "40-80 pips")],
    "fomc":         [("XAU/USD", "SELL", "100-200 pips — USD strength"), ("USD/JPY", "BUY", "50-100 pips")],
    "interest rate":[("XAU/USD", "SELL", "100-200 pips"), ("USD/JPY", "BUY", "50-100 pips")],
    "rate cut":     [("XAU/USD", "BUY",  "150-250 pips — dovish boost"), ("USD/JPY", "SELL", "50-80 pips")],
    "war":          [("XAU/USD", "BUY",  "150-300 pips — geopolitical spike"), ("BTC/USD", "BUY", "500-1000 pips")],
    "iran":         [("XAU/USD", "BUY",  "100-200 pips — geopolitical risk")],
    "sanctions":    [("XAU/USD", "BUY",  "80-150 pips"), ("BTC/USD", "BUY", "300-600 pips — sanction bypass")],
    "recession":    [("XAU/USD", "BUY",  "200-400 pips — risk-off"), ("BTC/USD", "SELL", "500-1000 pips")],
    # Crypto triggers
    "bitcoin":      [("BTC/USD", "BUY",  "200-500 pips — sentiment driven")],
    "btc":          [("BTC/USD", "BUY",  "200-500 pips")],
    "ethereum":     [("ETH/USD", "BUY",  "100-300 pips")],
    "eth":          [("ETH/USD", "BUY",  "100-300 pips")],
    "solana":       [("SOL/USD", "BUY",  "50-150 pips")],
    "crypto":       [("BTC/USD", "BUY",  "200-500 pips"), ("ETH/USD", "BUY", "100-200 pips")],
    "sec":          [("BTC/USD", "SELL", "300-600 pips — regulatory fear"), ("ETH/USD", "SELL", "150-300 pips")],
    "etf":          [("BTC/USD", "BUY",  "300-800 pips — institutional inflow")],
    # Forex triggers
    "dollar":       [("USD/JPY", "BUY",  "30-80 pips — USD strength"), ("XAU/USD", "SELL", "50-100 pips")],
    "yen":          [("USD/JPY", "SELL", "30-80 pips — yen safe haven")],
    "boj":          [("USD/JPY", "SELL", "50-120 pips — BoJ intervention risk")],
    "tariff":       [("XAU/USD", "BUY",  "80-150 pips — trade war hedge"), ("USD/JPY", "SELL", "30-60 pips")],
    "trump":        [("XAU/USD", "BUY",  "100-200 pips — uncertainty premium"), ("BTC/USD", "BUY", "300-500 pips")],
    "oil":          [("XAU/USD", "BUY",  "50-100 pips — inflation risk")],
}


def analyse_news_pairs(title: str) -> str:
    """Given a news headline, return which pairs to watch and pip estimate."""
    title_lower = title.lower()
    hits = {}  # pair → list of analysis strings

    for keyword, impacts in NEWS_PAIR_MAP.items():
        if keyword in title_lower:
            for pair, direction, estimate in impacts:
                key = f"{pair}_{direction}"
                if key not in hits:
                    hits[key] = (pair, direction, estimate)

    if not hits:
        return ""

    lines = ["📊 Pair Impact:"]
    for pair, direction, estimate in hits.values():
        arrow = "⬆️" if direction == "BUY" else "⬇️"
        lines.append(f"  {arrow} {SYMBOLS.get(pair, pair)} ({pair}) — {direction} {estimate}")

    return "\n".join(lines)


def format_news_alert(title: str, link: str) -> str:
    """Format a news alert with pair analysis appended."""
    analysis = analyse_news_pairs(title)
    msg = f"📰 NEWS\n\n{title}\n{link}"
    if analysis:
        msg += f"\n\n{analysis}"
    return msg

# ─────────────────────────────────────────────────────────────────────────────
# AI — Groq primary, Anthropic fallback
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are Jarvis — a sharp, loyal AI built specifically for Bigscruz (also called Scruz, Scruman, BigDawg, or Boss). "
    "He is a professional technical analyst with 10+ years of experience, 4 prop firm certificates, "
    "runs two trading groups (82 on WhatsApp, 48 on Telegram), and is an active single father of two. "
    "He is building toward financial freedom — recession-proof income through trading and the systems he creates.\n\n"
    "CRITICAL — HOW TO HANDLE MESSAGES:\n"
    "Every single message the user sends is about trading or charts unless it is clearly a greeting or personal chat. "
    "If he sends ANY text that could relate to a market, pair, price, or setup — treat it as a chart/trade question. "
    "He does not need hand-holding. He knows CRT, Malaysian S/R, CISD, sessions, sweeps. "
    "Skip the basics. Talk to him like a peer — experienced trader to experienced trader.\n\n"
    "Trading method: CRT High/Low sweeps, Malaysian S/R, CISD, session trading.\n"
    "Sessions: Asian 02-06 UTC = prime, London = caution, NY 13-18 UTC = secondary.\n\n"
    "STRICT RULES:\n"
    "1. Answer the question asked. SOL question = SOL answer. BTC question = BTC answer.\n"
    "2. TP MUST be 150 pips minimum from entry. Never less. Ever.\n"
    "3. SL max 40-60 pips. RR minimum 1:3.\n"
    "4. Always state session and tradeable or not.\n"
    "5. Max 6 lines. Exact Entry, SL, TP numbers.\n"
    "6. Dead zone = say so in one line, then give the setup anyway so he is prepared for the open.\n"
    "7. Speak like a peer. No nursery language. No over-explaining. He already knows."
)


def ask_groq(user_message: str, price_ctx: str) -> str:
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

    if GROQ_API_KEY:
        try:
            return ask_groq(user_message, price_ctx)
        except Exception as e:
            log.warning(f"Groq failed, trying Anthropic: {e}")

    if ANTHROPIC_API_KEY:
        try:
            return ask_anthropic(user_message, price_ctx)
        except Exception as e:
            log.error(f"Anthropic also failed: {e}")

    return "AI unavailable. Add GROQ_API_KEY to Railway variables (free at console.groq.com)"

# ─────────────────────────────────────────────────────────────────────────────
# FEATURE 4 — CHART IMAGE SCAN (Anthropic Vision)
# ─────────────────────────────────────────────────────────────────────────────

CHART_SCAN_PROMPT = (
    "You are Jarvis, an elite trading analyst. Analyse this chart image using:\n"
    "- CRT (Candle Range Theory): identify recent High/Low sweeps\n"
    "- Malaysian S/R: key support and resistance zones\n"
    "- CISD: Change in State of Delivery signals\n"
    "- Session context: Asian sweeps → NY entries\n\n"
    "Provide:\n"
    "1. What pattern you see (sweep high, sweep low, consolidation, trend)\n"
    "2. BUY or SELL bias with reason\n"
    "3. Suggested Entry, SL, TP (in pips or price)\n"
    "4. Confidence level (Low/Medium/High)\n\n"
    "Be concise. Max 200 words."
)


async def scan_chart_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Send chart image to Anthropic Claude Vision for analysis."""
    if not ai_client:
        return (
            "⚠️ Chart scan needs ANTHROPIC_API_KEY.\n"
            "Add it to Railway variables and top up $5 at console.anthropic.com"
        )

    try:
        image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
        resp = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": image_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": CHART_SCAN_PROMPT,
                        },
                    ],
                }
            ],
        )
        return f"🔍 Chart Scan:\n\n{resp.content[0].text}"
    except Exception as e:
        log.error(f"Chart scan error: {e}")
        return f"Chart scan failed: {e}"

# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL DETECTION (updated — London now included with ⚠️ warning)
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

    # London warning tag
    london_warn = "\n⚠️ LONDON SESSION — High chop risk. Reduce size. Confirm twice." if session == "LONDON" else ""

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
                f"🔴 SELL SIGNAL — {name} [{session}]\n"
                f"High swept: {prev_high:.4f} ({sweep_high:.0f} pips)\n"
                f"Entry: {entry:.4f}  SL: {sl:.4f}  TP: {tp:.4f}\n"
                f"Target: 150 pips | CRT + Malaysian S/R"
                f"{london_warn}"
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
                f"🟢 BUY SIGNAL — {name} [{session}]\n"
                f"Low swept: {prev_low:.4f} ({sweep_low:.0f} pips)\n"
                f"Entry: {entry:.4f}  SL: {sl:.4f}  TP: {tp:.4f}\n"
                f"Target: 150 pips | CRT + Malaysian S/R"
                f"{london_warn}"
            )

    return None


def detect_spike(symbol: str, price: float) -> str | None:
    last = last_prices.get(symbol)
    if last is None:
        return None
    pct = abs(price - last) / last * 100
    if pct >= 1.0:
        arrow = "⬆️ UP" if price > last else "⬇️ DOWN"
        return (
            f"⚡ SPIKE {arrow} — {SYMBOLS[symbol]}\n"
            f"{pct:.2f}% move  |  {last:.4f} → {price:.4f}"
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
    greeting = build_greeting(utc_hour)
    menu = (
        f"{greeting}\n\n"
        f"{'─' * 22}\n"
        f"🤖 JARVIS COMMANDS\n"
        f"{'─' * 22}\n"
        f"💲 /price — Live prices (Gold, BTC, ETH, SOL, JPY)\n"
        f"📊 /signal — Manual CRT signal scan\n"
        f"📰 /news — News + pair analysis\n"
        f"🕐 /session — Session status\n"
        f"📈 /status — Bot health check\n"
        f"🧠 /chat <question> — Ask Jarvis anything\n"
        f"📸 Send a chart image for auto-scan\n"
        f"{'─' * 22}\n"
        f"_Trading pattern: CRT + Malaysian S/R + CISD_\n"
        f"_Asian session = prime. London = caution. NY = secondary._"
    )
    await update.message.reply_text(menu)


async def cmd_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    utc_hour = datetime.now(timezone.utc).hour
    session  = get_session(utc_hour)
    advice = {
        "ASIAN":     "✅ ACTIVE — Asian session. Watch for Sydney high sweeps. Gold focus. Clean entries.",
        "ASIAN_END": "⚠️ TRANSITION — Asian ending. Be cautious.",
        "LONDON":    "⚠️ CAUTION — London session. Signals active but chop risk is HIGH. Reduce position size. Confirm twice before entering.",
        "NEW_YORK":  "✅ ACTIVE — NY session. Secondary entries if trend confirms.",
        "SYDNEY":    "👀 WATCH ONLY — Sydney setting up highs for Asian sweep.",
        "DEAD":      "💤 DEAD ZONE — No trading. Rest.",
    }
    await update.message.reply_text(
        f"Session: {session}\n"
        f"UTC: {utc_hour:02d}:00\n\n"
        f"{advice.get(session, 'Unknown')}"
    )


async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prices = fetch_all_prices()
    lines  = ["💲 Current Prices:"]
    for sym, price in prices.items():
        val = f"{price:,.4f}" if price else "unavailable"
        lines.append(f"  {SYMBOLS[sym]} ({sym}): {val}")
    await update.message.reply_text("\n".join(lines))


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    utc_hour = datetime.now(timezone.utc).hour
    session  = get_session(utc_hour)
    n        = sum(1 for p in last_prices.values() if p)
    depth    = len(price_history.get("XAU/USD", []))
    ai_status = "Groq ✅" if GROQ_API_KEY else ("Anthropic ✅" if ANTHROPIC_API_KEY else "❌ NONE")
    vision_status = "✅ Active" if ANTHROPIC_API_KEY else "⚠️ Add ANTHROPIC_API_KEY"
    await update.message.reply_text(
        f"📈 Jarvis Status\n"
        f"Session: {session}\n"
        f"AI Chat: {ai_status}\n"
        f"Chart Vision: {vision_status}\n"
        f"Markets live: {n}/{len(SYMBOLS)}\n"
        f"History: {depth} candles\n"
        f"Status: 🟢 RUNNING"
    )


async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    articles = fetch_news(5)
    if not articles:
        await update.message.reply_text("No new market news right now.")
        return
    for a in articles:
        msg = format_news_alert(a["title"], a["link"])
        await update.message.reply_text(msg)


async def cmd_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    utc_hour = datetime.now(timezone.utc).hour
    session  = get_session(utc_hour)

    if not is_signal_allowed(session):
        await update.message.reply_text(
            f"Session: {session}\n"
            f"No signals during this session.\n"
            f"Active windows:\n"
            f"  Asian 02:00-06:00 UTC\n"
            f"  London 07:00-13:00 UTC (⚠️ with warning)\n"
            f"  NY 13:00-18:00 UTC"
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
        london_note = "\n⚠️ London session — signals on but chop risk high." if session == "LONDON" else ""
        await update.message.reply_text(
            f"Session: {session}\n"
            f"No CRT setups right now.\n"
            f"History: {depth}/20 candles needed."
            f"{london_note}"
        )


async def cmd_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Explicit /chat command."""
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        # No question given — return a quick market briefing
        prices   = fetch_all_prices()
        utc_hour = datetime.now(timezone.utc).hour
        session  = get_session(utc_hour)
        reply    = ask_jarvis(
            "Give me a quick market briefing: current session, is it a good time to trade, "
            "and what is Gold doing right now?",
            prices,
        )
        await update.message.reply_text(reply)
        return
    prices = fetch_all_prices()
    reply  = ask_jarvis(text, prices)
    await update.message.reply_text(reply)


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Feature 4: Auto chart scan when user sends a photo."""
    await update.message.reply_text("🔍 Scanning chart... one moment.")
    try:
        photo   = update.message.photo[-1]  # Highest resolution
        file    = await ctx.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()
        result  = await scan_chart_image(bytes(img_bytes), "image/jpeg")
        await update.message.reply_text(result)
    except Exception as e:
        log.error(f"Photo handler error: {e}")
        await update.message.reply_text(f"Chart scan error: {e}")


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle file sends (uncompressed photos sent as documents)."""
    doc = update.message.document
    if not doc or not doc.mime_type or not doc.mime_type.startswith("image/"):
        await handle_message(update, ctx)
        return
    await update.message.reply_text("🔍 Scanning chart (file)... one moment.")
    try:
        file      = await ctx.bot.get_file(doc.file_id)
        img_bytes = await file.download_as_bytearray()
        result    = await scan_chart_image(bytes(img_bytes), doc.mime_type)
        await update.message.reply_text(result)
    except Exception as e:
        log.error(f"Document handler error: {e}")
        await update.message.reply_text(f"Chart scan error: {e}")


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

    loop_count   = 0
    prev_session = None

    while True:
        try:
            utc_hour = datetime.now(timezone.utc).hour
            session  = get_session(utc_hour)
            now      = datetime.utcnow().strftime("%H:%M:%S")

            log.info(f"[{now}] Session: {session}")

            # ── Feature 1: Greetings ─────────────────────────────────────────
            check_and_send_greeting()

            # ── Session change notification ──────────────────────────────────
            if session != prev_session and prev_session is not None:
                n = get_name()
                asian_msgs = [
                    f"🌏 ASIAN SESSION OPEN, {n}.\nThis is prime time. Watch for Sydney high sweeps on Gold.\nClean entries only — CRT + Malaysian S/R. Let's get it.",
                    f"🌏 ASIAN SESSION LIVE, {n}.\nSydney set the highs. Now we watch for the sweep.\nGold is the focus. 150 pips minimum. Be patient.",
                ]
                ny_msgs = [
                    f"🗽 NEW YORK SESSION OPEN, {n}.\nSecondary entries only — confirm the trend before you commit.\nDon't chase. Let the setup come to you.",
                    f"🗽 NY SESSION IS LIVE, {n}.\nVolatility picks up here. Confirm CISD before entering.\nGold and crypto are on watch.",
                ]
                messages = {
                    "ASIAN":    random.choice(asian_msgs),
                    "NEW_YORK": random.choice(ny_msgs),
                    "LONDON":   f"🇬🇧 LONDON SESSION, {n}.\n⚠️ Caution mode. Signals active but chop risk is HIGH.\nReduce size. Confirm twice. Manage existing trades carefully.",
                    "DEAD":     f"💤 Markets closing, {n}. Rest up.\nAsian session opens 02:00 UTC — be ready.",
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

            # ── Feature 2: News with pair analysis ──────────────────────────
            if loop_count % 10 == 0:
                articles = fetch_news(3)
                for article in articles:
                    if is_market_relevant(article["title"]):
                        msg = format_news_alert(article["title"], article["link"])
                        safe_send(msg)
                        sent_news_urls.add(article["link"])

            # ── Random motivational drop every ~90 mins ──────────────────
            if loop_count % 90 == 0 and loop_count > 0:
                quote = random.choice(MOTIVATIONAL_QUOTES)
                name = get_boss_name()
                safe_send(f"🧠 Jarvis drop for {name}:\n\n{quote}")

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

    # Commands
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("price",   cmd_price))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("news",    cmd_news))
    app.add_handler(CommandHandler("signal",  cmd_signal))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("chat",    cmd_chat))

    # Feature 4: Chart image scan — photos + file sends
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Text fallback
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
