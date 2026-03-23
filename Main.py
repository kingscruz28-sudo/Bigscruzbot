import requests
import time
from datetime import datetime
import os
import threading
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

# ============================================================
# BIGSCRUZ CRT + MALAYSIAN S/R SIGNAL BOT
# Telegram: @BigscruzBot
# Strategy: CRT sweep + Malaysian Support & Resistance
# ============================================================

TELEGRAM_TOKEN = "8334034705:AAH_DCZsBKxOsnh_EXLR6JW3bPZIi_QIHWE"
CHAT_ID = "887594990"

# --- CONFIG ---
SYMBOL = "XBTUSD"          # BTC on Kraken
TIMEFRAME_1H = 60           # 1 hour candles for CRT range
TIMEFRAME_1M = 1            # 1 min candles for entry trigger
SR_LOOKBACK = 50            # candles to find S/R levels
SWEEP_BUFFER = 0.0005       # 0.05% buffer for sweep detection
CHECK_INTERVAL = 60         # check every 60 seconds

last_signal_time = 0
SIGNAL_COOLDOWN = 300       # 5 min cooldown between signals

# ============================================================
# HEALTH CHECK SERVER
# ============================================================

bot_status = {
    "status": "starting",
    "btc_price": None,
    "support_levels": 0,
    "resistance_levels": 0,
    "last_scan": None,
    "last_signal": None,
}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(bot_status).encode())

    def log_message(self, format, *args):
        pass  # suppress default request logging


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
        print(f"[TELEGRAM SENT] {message[:60]}...")
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

# ============================================================
# FETCH CANDLES FROM KRAKEN
# ============================================================


def get_candles(interval_minutes):
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": SYMBOL, "interval": interval_minutes}
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("error"):
            print(f"[KRAKEN ERROR] {data['error']}")
            return []
        result = data["result"]
        key = [k for k in result if k != "last"][0]
        candles = result[key]
        # Each candle: [time, open, high, low, close, vwap, volume, count]
        return candles
    except Exception as e:
        print(f"[FETCH ERROR] {e}")
        return []

# ============================================================
# MALAYSIAN S/R — find key highs/lows from recent candles
# ============================================================


def get_sr_levels(candles_1h):
    if len(candles_1h) < SR_LOOKBACK:
        return [], []

    recent = candles_1h[-SR_LOOKBACK:]
    highs = [float(c[2]) for c in recent]
    lows  = [float(c[3]) for c in recent]

    resistance_levels = []
    support_levels    = []

    # Find swing highs (local maxima)
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            resistance_levels.append(highs[i])

    # Find swing lows (local minima)
    for i in range(2, len(lows) - 2):
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
           lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            support_levels.append(lows[i])

    return support_levels, resistance_levels

# ============================================================
# CHECK IF PRICE IS NEAR AN S/R LEVEL
# ============================================================


def near_sr(price, levels, tolerance_pct=0.003):
    for lvl in levels:
        if abs(price - lvl) / lvl < tolerance_pct:
            return True, lvl
    return False, None

# ============================================================
# CRT DETECTION
# Uses 1h candle for CRTH/CRTL, watches 1m for sweep + close back
# ============================================================


def check_crt_signal(candles_1h, candles_1m, support_levels, resistance_levels):
    if len(candles_1h) < 2 or len(candles_1m) < 3:
        return None

    # Previous 1h candle = CRT reference candle
    prev_1h = candles_1h[-2]
    crth = float(prev_1h[2])  # high
    crtl = float(prev_1h[3])  # low

    # Current 1m candles
    curr_1m  = candles_1m[-1]
    prev_1m  = candles_1m[-2]

    curr_high  = float(curr_1m[2])
    curr_low   = float(curr_1m[3])
    curr_close = float(curr_1m[4])

    # --- LONG SIGNAL ---
    # Wick sweeps below CRTL then closes back above it
    swept_low         = curr_low < crtl * (1 - SWEEP_BUFFER)
    closed_back_above = curr_close > crtl
    near_support, support_hit = near_sr(crtl, support_levels)

    if swept_low and closed_back_above and near_support:
        return {
            "direction": "LONG",
            "entry": curr_close,
            "crth": crth,
            "crtl": crtl,
            "sr_level": support_hit,
            "sweep": "LOW SWEPT",
            "emoji": "LONG",
        }

    # --- SHORT SIGNAL ---
    # Wick sweeps above CRTH then closes back below it
    swept_high        = curr_high > crth * (1 + SWEEP_BUFFER)
    closed_back_below = curr_close < crth
    near_resistance, resistance_hit = near_sr(crth, resistance_levels)

    if swept_high and closed_back_below and near_resistance:
        return {
            "direction": "SHORT",
            "entry": curr_close,
            "crth": crth,
            "crtl": crtl,
            "sr_level": resistance_hit,
            "sweep": "HIGH SWEPT",
            "emoji": "SHORT",
        }

    return None

# ============================================================
# FORMAT SIGNAL MESSAGE
# ============================================================


def format_signal(signal):
    now = datetime.utcnow().strftime("%H:%M UTC")
    emoji = "LONG" if signal["direction"] == "LONG" else "SHORT"
    direction_label = "LONG" if signal["direction"] == "LONG" else "SHORT"
    return (
        f"{emoji} BIGSCRUZ CRT SIGNAL\n\n"
        f"Direction: {direction_label}\n"
        f"Entry Price: ${signal['entry']:,.2f}\n\n"
        f"CRT Range:\n"
        f"CRTH: ${signal['crth']:,.2f}\n"
        f"CRTL: ${signal['crtl']:,.2f}\n\n"
        f"Malaysian S/R Hit: ${signal['sr_level']:,.2f}\n"
        f"Sweep: {signal['sweep']}\n\n"
        f"Time: {now}\n"
        f"Asset: BTC/USD\n\n"
        f"-- @BigscruzBot"
    )

# ============================================================
# MAIN LOOP
# ============================================================


def main():
    global last_signal_time

    print("BIGSCRUZ CRT BOT STARTING...")

    # Start health check server in background thread on port 5000
    health_thread = threading.Thread(target=start_health_server, args=(5000,), daemon=True)
    health_thread.start()

    send_telegram(
        "BigscruzBot is LIVE\n\nWatching BTC for CRT sweeps at Malaysian S/R levels.\nStrategy: CRT High/Low sweep + S/R confluence on 1m entries."
    )

    bot_status["status"] = "running"

    while True:
        try:
            now = datetime.utcnow()
            print(f"\n[{now.strftime('%H:%M:%S')}] Scanning BTC...")

            candles_1h = get_candles(60)
            candles_1m = get_candles(1)

            if not candles_1h or not candles_1m:
                print("No candle data. Retrying...")
                time.sleep(CHECK_INTERVAL)
                continue

            support_levels, resistance_levels = get_sr_levels(candles_1h)
            current_price = float(candles_1m[-1][4])

            print(f"  BTC Price: ${current_price:,.2f}")
            print(f"  S/R levels -- Support: {len(support_levels)} | Resistance: {len(resistance_levels)}")

            bot_status["btc_price"] = current_price
            bot_status["support_levels"] = len(support_levels)
            bot_status["resistance_levels"] = len(resistance_levels)
            bot_status["last_scan"] = now.strftime("%H:%M:%S UTC")

            # Check cooldown
            if time.time() - last_signal_time < SIGNAL_COOLDOWN:
                remaining = int(SIGNAL_COOLDOWN - (time.time() - last_signal_time))
                print(f"  Cooldown: {remaining}s remaining")
                time.sleep(CHECK_INTERVAL)
                continue

            signal = check_crt_signal(candles_1h, candles_1m, support_levels, resistance_levels)

            if signal:
                msg = format_signal(signal)
                print(f"\n  SIGNAL: {signal['direction']} at ${signal['entry']:,.2f}")
                send_telegram(msg)
                last_signal_time = time.time()
                bot_status["last_signal"] = f"{signal['direction']} at ${signal['entry']:,.2f} ({now.strftime('%H:%M UTC')})"
            else:
                print("  No CRT setup yet. Waiting for sweep...")

        except Exception as e:
            print(f"[ERROR] {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
