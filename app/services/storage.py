from __future__ import annotations

import re
from pathlib import Path
from typing import Optional
import shutil

SAFE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp",   
    ".gif",                              
    ".mp4", ".webm", ".mov",
}


def sanitize_filename(name: str) -> str:
    name = name.strip().replace("\x00", "")
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name or "file"


def ensure_safe_extension(filename: str, fallback_ext: str = ".bin") -> str:
    ext = Path(filename).suffix.lower()
    if ext in SAFE_EXTENSIONS:
        return ext
    return fallback_ext


def build_job_input_path(base_input_dir: Path, public_id: str, original_name: Optional[str], fallback_ext: str) -> Path:
    job_dir = (base_input_dir / public_id).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)

    safe_name = sanitize_filename(original_name or f"source{fallback_ext}")
    ext = ensure_safe_extension(safe_name, fallback_ext=fallback_ext)

    final_name = f"source{ext}"
    final_path = (job_dir / final_name).resolve()

    if final_path.parent != job_dir:
        raise RuntimeError("Unsafe file path detected.")

    return final_path

def remove_job_input_dir(base_input_dir: Path, public_id: str) -> None:
    job_dir = (base_input_dir / public_id).resolve()

    if not job_dir.exists():
        return

    expected_root = base_input_dir.resolve()
    if job_dir.parent != expected_root:
        raise RuntimeError("Unsafe cleanup path detected.")

    shutil.rmtree(job_dir)
    
def remove_job_output_dir(base_output_dir: Path, public_id: str) -> None:
    job_dir = (base_output_dir / public_id).resolve()
    if not job_dir.exists():
        return
    expected_root = base_output_dir.resolve()
    if job_dir.parent != expected_root:
        raise RuntimeError("Unsafe cleanup path detected.")
    shutil.rmtree(job_dir)


def remove_job_dirs(input_dir: Path, output_dir: Path, public_id: str) -> None:
    """Best-effort: сносит и input, и output каталог задачи. Ошибки глушим."""
    for remover, base in (
        (remove_job_input_dir, input_dir),
        (remove_job_output_dir, output_dir),
    ):
        try:
            remover(base, public_id)
        except Exception:
            pass


def sweep_orphan_job_dirs(base_dir: Path, keep_ids: set[str]) -> int:
    """Удаляет подкаталоги base_dir, public_id которых нет в keep_ids.
    Возвращает число удалённых каталогов."""
    base = base_dir.resolve()
    if not base.exists():
        return 0
    removed = 0
    for child in base.iterdir():
        if not child.is_dir() or child.name in keep_ids:
            continue
        if child.parent != base:
            continue
        try:
            shutil.rmtree(child)
            removed += 1
        except Exception:
            pass
    return removed