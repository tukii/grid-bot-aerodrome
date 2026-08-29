"""Opcional: alertas por Telegram.

Si `alerts.telegram_bot_token` y `alerts.telegram_chat_id` no están en
config.yaml, `send_alert` es un no-op. Nunca lanza excepciones.
"""
import logging
import urllib.request
import urllib.parse
import json

from paperbot.config import load_config

logger = logging.getLogger("paperbot.alerts")


def send_alert(message: str) -> bool:
    try:
        cfg = load_config()
        alerts = cfg.get("alerts") or {}
        token = alerts.get("telegram_bot_token", "")
        chat_id = alerts.get("telegram_chat_id", "")
        if not token or not chat_id:
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as e:
        logger.debug("telegram alert failed (non-fatal): %s", e)
        return False
