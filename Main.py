import requests
import time
from datetime import datetime
import os
import threading
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

# ============================================================
# BIGSCRUZ CRT + MALAYSIAN S/R SIGNAL BOT
# Assets: XAUUSD, XAGUSD, OIL, ETH, USDJPY, US100, SOL, BTC
# Strategy: CRT sweep + Malaysian S/R | Target: 100-150 pips
# ============================================================

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
TWELVE_API_KEY = os.environ["TWELVE_API_KEY"]

# --- ASSETS CONFIG ---
# pip_value = how many price units = 1 pip for each asset
ASSETS = {
    "XAUUSD":  {"name": "Gold",       "pip": 0.10,  "target_pips": 100, "type": "forex"},
    "XAGUSD":  {"name": "Silver",     "pip": 0.01,  "target_pips": 100, "type": "forex"},
    "WTI/USD": {"name": "Oil (WTI)",  "pip": 0.01,  "target_pips": 100, "type": "forex"},
    "ETH/USD": {"name": "Ethereum",   "pip": 0.10,  "target_pips": 100, "type": "crypto"},
    "USD/JPY": {"name": "USD/JPY",    "pip": 0.01,  "target_pips": 100, "type": "forex"},
    "US100":   {"name": "US100/NAS",  "pip": 1.0,   "target_pips": 100, "type": "index"},
    "SOL/USD": {"name": "Solana",     "pip": 0.10,  "target_pips": 100, "type": "crypto"},
    "XBT/USD": {"name": "Bitcoin",    "pip": 1.0,   "target_pips": 100, "type": "crypto"},
}

SWEEP_BUFFER = 0.0005
SR_LOOKBACK = 50
CHECK_INTERVAL = 60
SIGNAL_COOLDOWN = 300

last_signal_times = {symbol: 0 for symbol in ASSETS}

bot_status = {
    "status": "starting",
    "last_scan": None,
    "signals_sent": 0,
    "assets_monitored": list(ASSETS.keys()),
}

# ============================================================
# HEALTH CHECK SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(bot_status).encode())

    def log_message(self, format, *args):
        pass

def start_health_server(port=5000):
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"[HEALTH] Server listening on port {port}")
    server.serve_forever()

# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"[TELEGRAM SENT] {message[:80]}...")
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

# ============================================================
# FETCH CANDLES — Twelve Data for forex/indices, Kraken for crypto
# ============================================================

def get_candles_twelve(symbol, interval="1h", outputsize=60):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_API_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("status") == "error":
            print(f"[TWELVE ERROR] {symbol}: {data.get('message')}")
            return []
        values = data.get("values", [])
        # Convert to [time, open, high, low, close] format
        candles = [[v["datetime"], v["open"], v["high"], v["low"], v["close"]] for v in reversed(values)]
        return candles
    except Exception as e:
        print(f"[FETCH ERROR] {symbol}: {e}")
        return []

def get_candles_kraken(symbol, interval_minutes):
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": symbol, "interval": interval_minutes}
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("error"):
            return []
        result = data["result"]
        key = [k for k in result if k != "last"][0]
        return result[key]
    except Exception as e:
        print(f"[KRAKEN ERROR] {symbol}: {e}")
        return []

def get_candles(symbol, asset_info, interval="1h"):
    if asset_info["type"] == "crypto" and symbol == "XBT/USD":
        if interval == "1h":
            return get_candles_kraken("XBTUSD", 60)
        else:
            return get_candles_kraken("XBTUSD", 1)
    else:
        return get_candles_twelve(symbol, interval)

# ============================================================
# MALAYSIAN S/R
# ============================================================

def get_sr_levels(candles_1h):
    if len(candles_1h) < SR_LOOKBACK:
        return [], []
    recent = candles_1h[-SR_LOOKBACK:]
    highs = [float(c[2]) for c in recent]
    lows  = [float(c[3]) for c in recent]
    resistance_levels = []
    support_levels = []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            resistance_levels.append(highs[i])
    for i in range(2, len(lows) - 2):
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
           lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            support_levels.append(lows[i])
    return support_levels, resistance_levels

def near_sr(price, levels, tolerance_pct=0.003):
    for lvl in levels:
        if abs(price - lvl) / lvl < tolerance_pct:
            return True, lvl
    return False, None

# ============================================================
# CRT DETECTION
# ============================================================

def check_crt_signal(candles_1h, candles_1m, support_levels, resistance_levels, asset_info):
    if len(candles_1h) < 2 or len(candles_1m) < 3:
        return None

    prev_1h = candles_1h[-2]
    crth = float(prev_1h[2])
    crtl = float(prev_1h[3])

    curr_1m = candles_1m[-1]
    curr_high  = float(curr_1m[2])
    curr_low   = float(curr_1m[3])
    curr_close = float(curr_1m[4])

    pip = asset_info["pip"]
    target_pips = asset_info["target_pips"]

    # LONG
    swept_low = curr_low < crtl * (1 - SWEEP_BUFFER)
    closed_back_above = curr_close > crtl
    near_support, support_hit = near_sr(crtl, support_levels)
    if swept_low and closed_back_above and near_support:
        tp = curr_close + (target_pips * pip)
        sl = crtl - (50 * pip)
        rr = round((tp - curr_close) / (curr_close - sl), 1)
        return {
            "direction": "LONG",
            "entry": curr_close,
            "crth": crth, "crtl": crtl,
            "sr_level": support_hit,
            "sweep": "LOW SWEPT",
            "tp": tp, "sl": sl, "rr": rr,
        }

    # SHORT
    swept_high = curr_high > crth * (1 + SWEEP_BUFFER)
    closed_back_below = curr_close < crth
    near_resistance, resistance_hit = near_sr(crth, resistance_levels)
    if swept_high and closed_back_below and near_resistance:
        tp = curr_close - (target_pips * pip)
        sl = crth + (50 * pip)
        rr = round((curr_close - tp) / (sl - curr_close), 1)
        return {
            "direction": "SHORT",
            "entry": curr_close,
            "crth": crth, "crtl": crtl,
            "sr_level": resistance_hit,
            "sweep": "HIGH SWEPT",
            "tp": tp, "sl": sl, "rr": rr,
        }

    return None

# ============================================================
# FORMAT SIGNAL
# ============================================================

def format_signal(signal, symbol, asset_info):
    now = datetime.utcnow().strftime("%H:%M UTC")
    direction = signal["direction"]
    emoji = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
    return (
        f"{emoji} — {asset_info['name']} ({symbol})\n\n"
        f"Entry:  {signal['entry']:,.4f}\n"
        f"TP:     {signal['tp']:,.4f}  (+{100} pips min)\n"
        f"SL:     {signal['sl']:,.4f}\n"
        f"R:R     1:{signal['rr']}\n\n"
        f"CRT Range:\n"
        f"  High: {signal['crth']:,.4f}\n"
        f"  Low:  {signal['crtl']:,.4f}\n\n"
        f"S/R Level: {signal['sr_level']:,.4f}\n"
        f"Sweep:     {signal['sweep']}\n\n"
        f"Time: {now}\n"
        f"— @BigscruzBot"
    )

# ============================================================
# SCAN ONE ASSET
# ============================================================

def scan_asset(symbol, asset_info):
    global last_signal_times

    if time.time() - last_signal_times[symbol] < SIGNAL_COOLDOWN:
        return

    candles_1h = get_candles(symbol, asset_info, "1h")
    candles_1m = get_candles(symbol, asset_info, "1min")

    if not candles_1h or not candles_1m:
        print(f"  [{symbol}] No data")
        return

    support_levels, resistance_levels = get_sr_levels(candles_1h)
    current_price = float(candles_1m[-1][4])

    print(f"  [{symbol}] {asset_info['name']}: {current_price:,.4f} | S:{len(support_levels)} R:{len(resistance_levels)}")

    signal = check_crt_signal(candles_1h, candles_1m, support_levels, resistance_levels, asset_info)

    if signal:
        msg = format_signal(signal, symbol, asset_info)
        print(f"\n  🚨 SIGNAL: {symbol} {signal['direction']} @ {signal['entry']:,.4f}")
        send_telegram(msg)
        last_signal_times[symbol] = time.time()
        bot_status["signals_sent"] += 1

# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("BIGSCRUZ CRT BOT STARTING — MULTI ASSET...")

    health_thread = threading.Thread(target=start_health_server, args=(5000,), daemon=True)
    health_thread.start()

    assets_list = ", ".join([v["name"] for v in ASSETS.values()])
    send_telegram(
        f"🤖 BigscruzBot MULTI-ASSET is LIVE\n\n"
        f"Watching: {assets_list}\n\n"
        f"Strategy: CRT High/Low sweep + Malaysian S/R\n"
        f"Target: 100-150 pips minimum"
    )

    bot_status["status"] = "running"

    while True:
        try:
            now = datetime.utcnow()
            print(f"\n[{now.strftime('%H:%M:%S')}] Scanning all assets...")
            bot_status["last_scan"] = now.strftime("%H:%M:%S UTC")

            for symbol, asset_info in ASSETS.items():
                scan_asset(symbol, asset_info)
                time.sleep(2)  # avoid rate limits

        except Exception as e:
            print(f"[ERROR] {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
