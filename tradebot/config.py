"""Configurazione: variabili d'ambiente (.env) + config/settings.yaml."""
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")  # sul PC legge .env; in Docker le variabili arrivano già da compose


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
    db_path: str = os.getenv("DB_PATH") or str(ROOT / "data" / "tradebot.db")


ENV = Env()


def load_settings(path: str | None = None) -> dict:
    p = Path(path or os.getenv("SETTINGS_PATH", ROOT / "config" / "settings.yaml"))
    with open(p) as f:
        return yaml.safe_load(f)


def setup_logging(name: str) -> logging.Logger:
    import sys
    from logging.handlers import RotatingFileHandler
    for stream in (sys.stdout, sys.stderr):  # console Windows: niente crash su emoji/accenti
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logdir = Path(ENV.db_path).parent / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    fmt = f"%(asctime)s [{name}] %(levelname)s %(message)s"
    fh = RotatingFileHandler(logdir / f"{name}.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format=fmt,
                        handlers=[logging.StreamHandler(sys.stdout), fh], force=True)
    return logging.getLogger(name)


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(base.get(k, {}), v) if isinstance(v, dict) else v
    return out
