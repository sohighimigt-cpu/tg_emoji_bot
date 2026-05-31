from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from app.core.config import ensure_runtime_dirs, load_settings


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()

        settings = load_settings()
        secrets_to_hide = [
            settings.bot_token,
            settings.api_hash,
            settings.phone_number,
        ]

        for secret in secrets_to_hide:
            if secret:
                message = message.replace(secret, "***REDACTED***")

        record.msg = message
        record.args = ()
        return True


def setup_logging() -> logging.Logger:
    settings = load_settings()
    ensure_runtime_dirs(settings)

    logger = logging.getLogger("emoji_bot")
    logger.setLevel(getattr(logging, settings.log_level, logging.INFO))

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        settings.logs_dir / "bot.log",
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(SecretFilter())

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(SecretFilter())

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    return logger