"""Configurazione: variabili d'ambiente (.env) + config/settings.yaml."""
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _bool(v: str | None, default=False) -> bool:
    return default if v is None or v == "" else v.strip().lower() in ("1", "true", "yes", "si", "sì")


@dataclass
class Env:
    alpaca_key: str = os.getenv("ALPACA_API_KEY", "")
    alpaca_secret: str = os.getenv("ALPACA_SECRET_KEY", "")
    alpaca_paper: bool = _bool(os.getenv("ALPACA_PAPER"), True)
    anthropic_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
    tg_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat: str = os.getenv("TELEGRAM_CHAT_ID", "")
    tz: str = os.getenv("TZ", "Europe/Rome")
    dry_run: bool = _bool(os.getenv("DRY_RUN"), False)
    db_path: str = os.getenv("DB_PATH", str(ROOT / "data" / "tradebot.db"))


ENV = Env()


def load_settings(path: str | None = None) -> dict:
    p = Path(path or os.getenv("SETTINGS_PATH", ROOT / "config" / "settings.yaml"))
    with open(p) as f:
        return yaml.safe_load(f)


def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format=f"%(asctime)s [{name}] %(levelname)s %(message)s",
    )
    return logging.getLogger(name)
