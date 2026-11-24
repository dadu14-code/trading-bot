"""Invio messaggi e lettura comandi via Telegram Bot API (HTTP semplice)."""
import logging

import requests

from .config import ENV

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


def _call(method: str, **params):
    if not ENV.tg_token:
        log.info("[telegram disattivato] %s %s", method, params.get("text", "")[:200])
        return None
    r = requests.post(API.format(token=ENV.tg_token, method=method), json=params, timeout=40)
    r.raise_for_status()
    return r.json()


def send(text: str):
    # Telegram limita i messaggi a 4096 caratteri
    for i in range(0, len(text), 4000):
        try:
            _call("sendMessage", chat_id=ENV.tg_chat, text=text[i:i + 4000], parse_mode="HTML",
                  disable_web_page_preview=True)
        except requests.RequestException as e:
            log.error("Telegram send fallito: %s", e)
            raise


def get_updates(offset: int | None, timeout: int = 25) -> list[dict]:
    try:
        r = _call("getUpdates", offset=offset, timeout=timeout)
        return r["result"] if r else []
    except requests.RequestException as e:
        log.warning("Telegram getUpdates: %s", e)
        return []
