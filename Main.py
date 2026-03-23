import requests
import time
import threading
import json
import os
import feedparser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

# ============================================================
# BIGSCRUZ JARVIS — Trading Assistant Bot
# CRT Signals + Price Alerts + News + Chat via Claude AI
# ============================================================

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
CHAT_ID          = os.environ["CHAT_ID"]
TWELVE_API_KEY   = os.environ["TWELVE_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

ASSETS = {
    "XAU/USD": {"name": "Gold",      "pip": 0.10,  "source": "twelve"},
    "ETH/USD": {"name": "Ethereum",  "pip": 0.10,  "source": "twelve"},
    "USD/JPY": {"name": "USD/JPY",   "pip": 0.01,  "source": "twelve"},
    "SOL/USD": {"name": "Solana",    "pip": 0.10,  "source": "twelve"},
    "XBTUSD":  {"name": "Bitcoin",   "pip": 1.0,   "source": "kraken"},
}

SWEEP_BUFFER      = 0.0005
SR_LOOKBACK       = 50
CHECK_INTERVAL    = 120
SIGNAL_COOLDOWN   = 300
SPIKE_THRESHOLD   = 0.01   # 1% price move = spike alert
API_CALL_DELAY    = 8

last_signal_times = {s: 0 for s in ASSETS}
last_prices       = {}
last_update_id    = 0
conversation_history = []

# ============================================================
# HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "running"}).encode())
    def log_message(self, format, *args):
        pass

def start_health_server(port=5000):
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# ============================================================
# TELEGRAM — SEND & RECEIVE
# ============================================================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
        print(f"[TELEGRAM] {message[:60]}...")
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"timeout": 10, "offset": offset}
    try:
        r = requests.get(url, params=params, timeout=15)
        return r.json().get("result", [])
    except:
        return []

# ============================================================
# CLAUDE AI CHAT
# ============================================================

SYSTEM_PROMPT = """You are Jarvis, a personal AI trading assistant for a forex and crypto trader named Scruz.
He trades: Gold (XAU/USD), Silver, Oil, ETH, USD/JPY, US100, Solana, Bitcoin.
His strategy is CRT (Candle Range Theory) + Malaysian Support & Resistance on 1m entries.
His minimum target is 100-150 pips per trade. He leverage trades.

You help him with:
- Market analysis and price levels
- Explaining CRT setups
- News impact on his assets
- General trading questions
- Answering anything he asks

Be direct, concise, like a smart trading assistant. No fluff."""

def ask_claude(user_message):
    global conversation_history
    conversation_history.append({"role": "user", "content": user_message})
    if len(conversation_history) > 20:
        conversation_history = conversation_history[-20:]
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 500,
                "system": SYSTEM_PROMPT,
                "messages": conversation_history
            },
            timeout=20
        )
        data = r.json()
        reply = data["content"][0]["text"]
        conversation_history.append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        return f"Error reaching AI: {e}"

# ============================================================
# HANDLE INCOMING MESSAGES
# ============================================================

def handle_message(text):
    text = text.strip().lower()

    # Commands
    if text in ["/start", "hello", "hi", "hey"]:
        return ("Jarvis online. I'm watching Gold, ETH, USD/JPY, SOL and BTC for you.\n\n"
                "Commands:\n/price - current prices\n/status - bot status\n/news - latest market news\n"
                "Or just ask me anything.")

    elif text == "/price" or text == "prices":
        lines = ["Current Prices:"]
        for symbol, info in ASSETS.items():
            price = last_prices.get(symbol)
            if price:
                lines.append(f"  {info['name']}: {price:,.4f}")
            else:
                lines.append(f"  {info['name']}: fetching...")
        return "\n".join(lines)

    elif text == "/status":
        return (f"Jarvis Status: ACTIVE\n"
                f"Monitoring: {len(ASSETS)} assets\n"
                f"Scan interval: every 2 minutes\n"
                f"Strategy: CRT + Malaysian S/R\n"
                f"Target: 100-150 pips")

    elif text == "/news":
        return get_market_news()

    else:
        # Send to Claude
        return ask_claude(text)

# ============================================================
# TELEGRAM POLLING THREAD
# ============================================================

def poll_telegram():
    global last_update_id
    print("[TELEGRAM] Polling for messages...")
    while True:
        try:
            updates = get_updates(offset=last_update_id + 1)
            for update in updates:
                last_update_id = update["update_id"]
                message = update.get("message", {})
                text = message.get("text", "")
                if text:
                    print(f"[MESSAGE] {text}")
                    reply = handle_message(text)
                    send_telegram(reply)
        except Exception as e:
            print(f"[POLL ERROR] {e}")
        time.sleep(2)

# ============================================================
# MARKET NEWS (RSS)
# ============================================================

NEWS_FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://www.forexlive.com/feed/news",
]
seen_news = set()

def get_market_news():
    headlines = []
    for feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:3]:
                headlines.append(f"- {entry.title}")
        except:
            pass
    if headlines:
        return "Latest Market News:\n" + "\n".join(headlines[:6])
    return "No news fetched right now."

def check_news():
    keywords = ["gold", "bitcoin", "fed", "inflation", "cpi", "nfp", "rate", "oil", "yen", "ethereum", "solana"]
    for feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:5]:
                title = entry.title.lower()
                link  = entry.get("link", "")
                if link in seen_news:
                    continue
                if any(k in title for k in keywords):
                    seen_news.add(link)
                    send_telegram(f"NEWS ALERT\n\n{entry.title}\n\n{link}")
        except Exception as e:
            print(f"[NEWS ERROR] {e}")

# ============================================================
# PRICE DATA
# ============================================================

def get_candles_twelve(symbol, interval="1h", outputsize=60):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": outputsize, "apikey": TWELVE_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("status") == "error":
            return []
        values = data.get("values", [])
        return [[v["datetime"], v["open"], v["high"], v["low"], v["close"]] for v in reversed(values)]
    except:
        return []

def get_candles_kraken(symbol, interval_minutes):
    try:
        r = requests.get("https://api.kraken.com/0/public/OHLC",
                         params={"pair": symbol, "interval": interval_minutes}, timeout=15)
        data = r.json()
        if data.get("error"):
            return []
        result = data["result"]
        key = [k for k in result if k != "last"][0]
        return result[key]
    except:
        return []

def get_candles(symbol, info, interval="1h"):
    if info["source"] == "kraken":
        return get_candles_kraken(symbol, 60 if interval == "1h" else 1)
    return get_candles_twelve(symbol, interval)

# ============================================================
# S/R LEVELS
# ============================================================

def get_sr_levels(candles):
    if len(candles) < SR_LOOKBACK:
        return [], []
    recent = candles[-SR_LOOKBACK:]
    highs = [float(c[2]) for c in recent]
    lows  = [float(c[3]) for c in recent]
    res, sup = [], []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            res.append(highs[i])
    for i in range(2, len(lows) - 2):
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            sup.append(lows[i])
    return sup, res

def near_sr(price, levels, tolerance_pct=0.003):
    for lvl in levels:
        if abs(price - lvl) / lvl < tolerance_pct:
            return True, lvl
    return False, None

# ============================================================
# CRT SIGNAL CHECK
# ============================================================

def check_crt(candles_1h, candles_1m, sup, res, info):
    if len(candles_1h) < 2 or len(candles_1m) < 3:
        return None
    crth = float(candles_1h[-2][2])
    crtl = float(candles_1h[-2][3])
    c    = candles_1m[-1]
    hi, lo, cl = float(c[2]), float(c[3]), float(c[4])
    pip = info["pip"]

    if lo < crtl * (1 - SWEEP_BUFFER) and cl > crtl:
        ns, sh = near_sr(crtl, sup)
        if ns:
            tp = cl + (100 * pip)
            sl = crtl - (50 * pip)
            return {"dir": "LONG", "entry": cl, "tp": tp, "sl": sl, "crth": crth, "crtl": crtl, "sr": sh, "sweep": "LOW SWEPT"}

    if hi > crth * (1 + SWEEP_BUFFER) and cl < crth:
        nr, rh = near_sr(crth, res)
        if nr:
            tp = cl - (100 * pip)
            sl = crth + (50 * pip)
            return {"dir": "SHORT", "entry": cl, "tp": tp, "sl": sl, "crth": crth, "crtl": crtl, "sr": rh, "sweep": "HIGH SWEPT"}
    return None

def format_signal(sig, symbol, info):
    now = datetime.utcnow().strftime("%H:%M UTC")
    emoji = "LONG" if sig["dir"] == "LONG" else "SHORT"
    rr = round(abs(sig["tp"] - sig["entry"]) / max(abs(sig["entry"] - sig["sl"]), 0.0001), 1)
    return (f"{emoji} SIGNAL - {info['name']} ({symbol})\n\n"
            f"Entry:  {sig['entry']:,.4f}\n"
            f"TP:     {sig['tp']:,.4f}\n"
            f"SL:     {sig['sl']:,.4f}\n"
            f"R:R     1:{rr}\n\n"
            f"CRT High: {sig['crth']:,.4f}\n"
            f"CRT Low:  {sig['crtl']:,.4f}\n"
            f"S/R Level: {sig['sr']:,.4f}\n"
            f"Sweep: {sig['sweep']}\n\n"
            f"Time: {now}\n-- @BigscruzBot")

# ============================================================
# SCAN ASSET
# ============================================================

def scan_asset(symbol, info):
    if time.time() - last_signal_times[symbol] < SIGNAL_COOLDOWN:
        return

    c1h = get_candles(symbol, info, "1h")
    time.sleep(2)
    c1m = get_candles(symbol, info, "1min")

    if not c1h or not c1m:
        print(f"  [{symbol}] No data")
        return

    price = float(c1m[-1][4])
    last_prices[symbol] = price

    # Price spike check
    prev = last_prices.get(symbol + "_prev")
    if prev and abs(price - prev) / prev >= SPIKE_THRESHOLD:
        pct = ((price - prev) / prev) * 100
        direction = "UP" if price > prev else "DOWN"
        send_telegram(f"PRICE SPIKE - {info['name']}\n\n{direction} {abs(pct):.2f}%\n"
                     f"Was: {prev:,.4f}\nNow: {price:,.4f}")
    last_prices[symbol + "_prev"] = price

    sup, res = get_sr_levels(c1h)
    print(f"  [{symbol}] {info['name']}: {price:,.4f} | S:{len(sup)} R:{len(res)}")

    sig = check_crt(c1h, c1m, sup, res, info)
    if sig:
        msg = format_signal(sig, symbol, info)
        send_telegram(msg)
        last_signal_times[symbol] = time.time()

# ============================================================
# ECONOMIC EVENTS (simple scheduler)
# ============================================================

EVENTS = [
    {"day": 0, "hour": 13, "min": 30, "name": "NFP / US Jobs Data"},
    {"day": 1, "hour": 13, "min": 30, "name": "CPI Inflation Data"},
    {"day": 2, "hour": 18, "min": 0,  "name": "Fed Interest Rate Decision"},
    {"day": 3, "hour": 12, "min": 0,  "name": "ECB Rate Decision"},
]
alerted_events = set()

def check_economic_events():
    now = datetime.utcnow()
    for event in EVENTS:
        key = f"{event['name']}-{now.date()}"
        if key in alerted_events:
            continue
        # Alert 15 mins before
        diff_mins = (event["hour"] * 60 + event["min"]) - (now.hour * 60 + now.minute)
        if now.weekday() == event["day"] and 0 <= diff_mins <= 15:
            send_telegram(f"ECONOMIC EVENT in ~{diff_mins} min\n\n{event['name']}\n\nBe careful — high volatility expected!")
            alerted_events.add(key)

# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("JARVIS STARTING...")

    threading.Thread(target=start_health_server, args=(5000,), daemon=True).start()
    threading.Thread(target=poll_telegram, daemon=True).start()

    send_telegram(
        "Jarvis is ONLINE\n\n"
        "Watching: Gold, ETH, USD/JPY, Solana, Bitcoin\n\n"
        "I will alert you for:\n"
        "- CRT trade setups (100+ pips)\n"
        "- Price spikes (1%+ moves)\n"
        "- Market news\n"
        "- Economic events\n\n"
        "You can also just message me anything."
    )

    scan_count = 0
    while True:
        try:
            now = datetime.utcnow()
            print(f"\n[{now.strftime('%H:%M:%S')}] Scanning...")

            for symbol, info in ASSETS.items():
                scan_asset(symbol, info)
                time.sleep(API_CALL_DELAY)

            check_economic_events()

            if scan_count % 5 == 0:  # check news every 5 scans
                check_news()

            scan_count += 1

        except Exception as e:
            print(f"[ERROR] {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
