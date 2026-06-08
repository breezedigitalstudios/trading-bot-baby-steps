"""
telegram_alert.py — fire-and-forget Telegram notifications.
Fails silently on any error so an alert failure never crashes a trading script.
"""

import os
import json
import urllib.request
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_alert(message: str) -> bool:
    """Send a Telegram message. Returns True on success, False on any failure."""
    if not _TOKEN or not _CHAT_ID:
        return False
    try:
        url  = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
        data = json.dumps({
            "chat_id":    _CHAT_ID,
            "text":       message,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False
