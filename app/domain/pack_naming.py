from __future__ import annotations

import re
import secrets
import unicodedata
from typing import Callable
CYRILLIC_MAP = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def transliterate_ru(text: str) -> str:
    return "".join(CYRILLIC_MAP.get(ch, ch) for ch in text.lower())


def slugify_title(value: str) -> str:
    value = value.strip().lower()
    value = transliterate_ru(value)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value


def normalize_short_name_base(title: str) -> str:
    base = slugify_title(title)
    if not base:
        base = f"emoji_{secrets.token_hex(3)}"
    if not base[0].isalpha():
        base = f"e_{base}"
    base = re.sub(r"_+", "_", base).strip("_")
    return base


def build_short_name(title: str, bot_username: str) -> str:
    base = normalize_short_name_base(title)
    suffix = f"_by_{bot_username.lower()}"
    max_base_len = 64 - len(suffix)
    base = base[:max_base_len].strip("_")
    if not base:
        base = f"emoji_{secrets.token_hex(3)}"
        base = base[:max_base_len].strip("_")
    short_name = f"{base}{suffix}"
    short_name = re.sub(r"_+", "_", short_name)
    return short_name[:64].rstrip("_")

def build_short_name_with_token(title: str, bot_username: str, token: str) -> str:
    """Like build_short_name, but injects a uniqueness token before the
    _by_<bot> suffix so the suffix always stays at the end."""
    base = normalize_short_name_base(title)
    suffix = f"_by_{bot_username.lower()}"
    token_part = f"_{token}" if token else ""
    max_base_len = 64 - len(suffix) - len(token_part)
    if max_base_len < 1:
        max_base_len = 1
    base = base[:max_base_len].strip("_")
    if not base:
        base = "e"
    short_name = f"{base}{token_part}{suffix}"
    short_name = re.sub(r"_+", "_", short_name)
    return short_name[:64].rstrip("_")

def _short_name_with_token(title: str, bot_username: str, token: str) -> str:
    """Базовое имя + несколько символов перед _by_ суффиксом.
    Используется ТОЛЬКО при коллизии short_name в БД."""
    base = normalize_short_name_base(title)
    suffix = f"_by_{bot_username.lower()}"
    token_part = f"_{token}" if token else ""
    max_base_len = 64 - len(suffix) - len(token_part)
    if max_base_len < 1:
        max_base_len = 1
    base = base[:max_base_len].strip("_")
    if not base:
        base = "e"
    short_name = f"{base}{token_part}{suffix}"
    short_name = re.sub(r"_+", "_", short_name)
    return short_name[:64].rstrip("_")


def build_unique_short_name(
    title: str,
    bot_username: str,
    exists: Callable[[str], bool],
    max_attempts: int = 12,
) -> str:
    candidate = build_short_name(title, bot_username)
    if not exists(candidate):
        return candidate
    for _ in range(max_attempts):
        candidate = _short_name_with_token(title, bot_username, secrets.token_hex(2))
        if not exists(candidate):
            return candidate
    # Крайне маловероятно: фолбэк на более длинный токен.
    return _short_name_with_token(title, bot_username, secrets.token_hex(6))