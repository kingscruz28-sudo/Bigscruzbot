import os

# Main.py reads these via os.environ[...] at import time, so they must exist
# before pytest collects any test module that imports Main.
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("CHAT_ID", "12345")
os.environ.setdefault("ER_API_KEY", "test-er-key")

import pytest

import Main


@pytest.fixture(autouse=True)
def reset_bot_state():
    """Main.py keeps signal/session state in module-level globals. Reset them
    before every test so tests don't leak state into each other."""
    for sym in Main.SYMBOLS:
        Main.price_history[sym] = []
    Main.last_prices.clear()
    Main.sent_news_urls.clear()
    Main.last_signal_time.clear()
    Main.last_signal_dir.clear()
    Main.greeted_periods.clear()
    yield
