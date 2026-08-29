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
        token = chat_id = ""
        # Fuente 1 (privilegiada): ~/trading/.env (no versionado) — fix BAJA:
        # el token NO debe vivir en config.yaml (archivo versionable del repo).
        try:
            from paperbot.config import ENV_PATH
            if ENV_PATH.exists():
                for line in ENV_PATH.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("TELEGRAM_BOT_TOKEN=") and not token:
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    elif line.startswith("TELEGRAM_CHAT_ID=") and not chat_id:
                        chat_id = line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
        # Fuente 2 (fallback): ~/trading/.env — mismo patrón que el supervisor
        try:
            from pathlib import Path as _Path
            _env = _Path(__import__("os").path.expanduser("~/trading/.env"))
            if _env.exists():
                for line in _env.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("TELEGRAM_BOT_TOKEN=") and not token:
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    elif line.startswith("TELEGRAM_CHAT_ID=") and not chat_id:
                        chat_id = line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
        # Fuente 3 (último recurso): config.yaml (se deja vacío; fallback legacy)
        cfg = load_config()
        alerts = cfg.get("alerts") or {}
        if not token:
            token = alerts.get("telegram_bot_token", "")
        if not chat_id:
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
