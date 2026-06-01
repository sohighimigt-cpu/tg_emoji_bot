from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def _safe_resolve(path_value: str, *, base_dir: Path) -> Path:
    path = (base_dir / path_value).resolve() if not Path(path_value).is_absolute() else Path(path_value).resolve()
    return path


@dataclass(frozen=True)
class Settings:
    bot_token: str
    bot_username: str
    api_id: int
    api_hash: str
    phone_number: str
    telethon_session_path: Path
    database_url: str 
    log_level: str
    data_dir: Path
    sessions_dir: Path
    input_dir: Path
    output_dir: Path
    temp_dir: Path
    logs_dir: Path
    max_parallel_jobs: int

@lru_cache(maxsize=1)
def load_settings() -> Settings:
    bot_token = _require_env("BOT_TOKEN")
    bot_username = _require_env("BOT_USERNAME").lstrip("@").strip()
    api_id = int(_require_env("API_ID"))
    api_hash = _require_env("API_HASH")
    phone_number = _require_env("PHONE_NUMBER")
    telethon_session_path = _safe_resolve(_require_env("TELETHON_SESSION_PATH"), base_dir=BASE_DIR)
    database_path = _safe_resolve(_require_env("DATABASE_PATH"), base_dir=BASE_DIR)
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()

    data_dir = (BASE_DIR / "data").resolve()
    sessions_dir = (data_dir / "sessions").resolve()
    input_dir = (data_dir / "input").resolve()
    output_dir = (data_dir / "output").resolve()
    temp_dir = (data_dir / "temp").resolve()
    logs_dir = (BASE_DIR / "logs").resolve()

    max_parallel_jobs = int(os.getenv("MAX_PARALLEL_JOBS", "1"))

    if max_parallel_jobs != 1:
        raise RuntimeError("For safety, MAX_PARALLEL_JOBS must stay equal to 1 in the current version.")

    return Settings(
        bot_token=bot_token,
        bot_username=bot_username,
        api_id=api_id,
        api_hash=api_hash,
        phone_number=phone_number,
        telethon_session_path=telethon_session_path,
        database_path=database_path,
        log_level=log_level,
        data_dir=data_dir,
        sessions_dir=sessions_dir,
        input_dir=input_dir,
        output_dir=output_dir,
        temp_dir=temp_dir,
        logs_dir=logs_dir,
        max_parallel_jobs=max_parallel_jobs,
    )


def ensure_runtime_dirs(settings: Settings) -> None:
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    settings.input_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.telethon_session_path.parent.mkdir(parents=True, exist_ok=True)