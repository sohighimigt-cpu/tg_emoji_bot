from __future__ import annotations

import os
import json
import math
import shutil
import subprocess

from dataclasses import dataclass
from pathlib import Path
from app.db.repository import JobRecord
from app.core.logging_config import setup_logging
from concurrent.futures import ThreadPoolExecutor
logger = setup_logging()
MAX_DURATION_SECONDS = 3.0
EMOJI_SIZE = 100
FFMPEG_TIMEOUT_SECONDS = int(os.getenv("FFMPEG_TIMEOUT_SECONDS", "120"))
VIDEO_MAX_FILE_SIZE_BYTES = 64 * 1024

VP9_CPU_USED = int(os.getenv("VP9_CPU_USED", "4"))
VP9_DEADLINE = os.getenv("VP9_DEADLINE", "good")  
VP9_THREADS = int(os.getenv("VP9_THREADS", "2"))


TILE_WORKERS = int(os.getenv("TILE_WORKERS", str(max(1, min(4, os.cpu_count() or 1)))))


VIDEO_ENCODE_LADDER = [
    (15, 50),
    (15, 56),
    (12, 58),
    (12, 60),
    (10, 63),
]

STATIC_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".gif"}

class ConversionError(RuntimeError):
    """Ошибка обработки с безопасным для пользователя текстом (args[0])."""

@dataclass
class TileResult:
    index: int
    row: int
    col: int
    path: Path
    size_bytes: int
    fps: int | None
    crf: int | None
    ok: bool


@dataclass
class ConversionResult:
    public_id: str
    output_dir: Path
    normalized_source_path: Path
    tile_dir: Path
    rows: int
    cols: int
    duration_seconds: float | None
    tile_results: list[TileResult]
    output_kind: str  # "static" | "video"

    @property
    def successful_tiles(self) -> list[TileResult]:
        return [x for x in self.tile_results if x.ok]

    @property
    def failed_tiles(self) -> list[TileResult]:
        return [x for x in self.tile_results if not x.ok]


def ensure_ffmpeg_available() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"{tool} is not available in PATH")


def _run(cmd: list[str], title: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg step timed out after %ss: %s", FFMPEG_TIMEOUT_SECONDS, title)
        raise ConversionError(
            "Обработка заняла слишком много времени и была прервана. "
            "Попробуйте файл покороче или меньшую сетку."
        )
    if result.returncode != 0:
        stderr = result.stderr.strip() or "Command failed"
        logger.error("ffmpeg step failed: %s | %s", title, stderr)
        raise ConversionError("Не удалось обработать файл. Проверьте формат и попробуйте ещё раз.")
    return result


def probe_media_info(source_path: Path) -> dict:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(source_path),
        ],
        title="ffprobe media info",
    )
    return json.loads(result.stdout)


def _parse_timecode_to_seconds(value: str) -> float | None:
    # "00:00:02.500000000" -> 2.5
    try:
        parts = value.split(":")
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        return float(value)
    except (ValueError, TypeError):
        return None


def _duration_from_info(info: dict) -> float | None:
    # 1) длительность контейнера
    raw = info.get("format", {}).get("duration")
    if raw not in (None, "N/A"):
        try:
            return float(raw)
        except (ValueError, TypeError):
            pass

    video_streams = [
        s for s in info.get("streams", []) if s.get("codec_type") == "video"
    ]

    # 2) длительность на уровне видеопотока
    for s in video_streams:
        raw = s.get("duration")
        if raw not in (None, "N/A"):
            try:
                return float(raw)
            except (ValueError, TypeError):
                pass

    # 3) теги потока DURATION (mkv/webm)
    for s in video_streams:
        for key, val in (s.get("tags") or {}).items():
            if key.lower() == "duration":
                parsed = _parse_timecode_to_seconds(val)
                if parsed:
                    return parsed

    # 4) расчёт по числу кадров и частоте
    for s in video_streams:
        nb_frames = s.get("nb_frames")
        rate = s.get("avg_frame_rate") or s.get("r_frame_rate")
        if nb_frames not in (None, "N/A") and rate not in (None, "N/A", "0/0"):
            try:
                num, den = rate.split("/")
                fps = float(num) / float(den) if float(den) else 0.0
                if fps > 0:
                    return int(nb_frames) / fps
            except (ValueError, TypeError, ZeroDivisionError):
                pass

    return None


def probe_duration_seconds(source_path: Path) -> float:
    info = probe_media_info(source_path)
    duration = _duration_from_info(info)
    if duration is None or duration <= 0:
        # Исходник зацикливается (-stream_loop -1), так что разумный
        # дефолт всё равно даёт корректный пак.
        logger.warning(
            "Не удалось определить длительность; фолбэк на %.1fs",
            MAX_DURATION_SECONDS,
        )
        return MAX_DURATION_SECONDS
    return duration


def parse_grid_code(grid_code: str) -> tuple[int, int] | None:

    if not grid_code or not isinstance(grid_code, str):
        return None

    parts = grid_code.lower().split("x")
    if len(parts) != 2:
        return None

    try:
        cols = int(parts[0])
        rows = int(parts[1])
    except ValueError:
        return None

    if not (1 <= cols <= 10 and 1 <= rows <= 10):
        return None

    return cols, rows


def get_grid_size(grid_code: str) -> tuple[int, int]:
    parsed = parse_grid_code(grid_code)
    if parsed is None:
        raise RuntimeError(f"Unsupported grid_code: {grid_code}")
    return parsed


def ensure_job_output_dir(base_output_dir: Path, public_id: str) -> Path:
    job_dir = (base_output_dir / public_id).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def detect_source_kind(job: JobRecord) -> str:
    if not job.source_file_path:
        raise RuntimeError("source_file_path is empty")

    source_path = Path(job.source_file_path)
    ext = source_path.suffix.lower()

    if job.source_type == "photo":
        return "static"

    if job.source_type in {"video", "animation"}:
        return "video"

    if job.source_type == "document":
        if ext in STATIC_EXTENSIONS:
            return "static"
        if ext in VIDEO_EXTENSIONS:
            return "video"

    raise RuntimeError(f"Unsupported source type or extension: {job.source_type} / {ext}")

def _has_crop(job: JobRecord) -> bool:
    return (
        job.crop_x is not None
        and job.crop_y is not None
        and job.crop_w is not None
        and job.crop_h is not None
        and job.crop_w > 0
        and job.crop_h > 0
    )


def _geometry_vf(job: JobRecord, canvas_w: int, canvas_h: int) -> str:
    """Часть фильтра ffmpeg: кроп (если задан) + приведение к холсту сетки."""
    if _has_crop(job):
        cx = min(max(job.crop_x, 0.0), 1.0)
        cy = min(max(job.crop_y, 0.0), 1.0)
        cw = min(max(job.crop_w, 0.0), 1.0 - cx)
        ch = min(max(job.crop_h, 0.0), 1.0 - cy)
        # рамка уже в пропорции сетки -> масштабируем точно, без pad
        return (
            f"crop=iw*{cw:.6f}:ih*{ch:.6f}:iw*{cx:.6f}:ih*{cy:.6f},"
            f"scale={canvas_w}:{canvas_h}:flags=lanczos,"
            f"format=rgba"
        )
    # без кропа — прежнее поведение: вписать + прозрачный pad
    return (
        f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:color=black@0,"
        f"format=rgba"
    )

def normalize_static_source(job: JobRecord, output_dir: Path, cols: int, rows: int) -> Path:
    if not job.source_file_path:
        raise RuntimeError("source_file_path is empty")

    source_path = Path(job.source_file_path).resolve()
    if not source_path.exists():
        raise RuntimeError(f"Source file not found: {source_path}")

    normalized_path = output_dir / "normalized_grid_source.png"
    canvas_w = cols * EMOJI_SIZE
    canvas_h = rows * EMOJI_SIZE

    vf = _geometry_vf(job, canvas_w, canvas_h)

    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-vf",
            vf,
            "-frames:v",
            "1",
            str(normalized_path),
        ],
        title="normalize static source to transparent grid canvas",
    )

    return normalized_path


def normalize_video_source(job: JobRecord, output_dir: Path, cols: int, rows: int) -> tuple[Path, float]:
    if not job.source_file_path:
        raise RuntimeError("source_file_path is empty")

    source_path = Path(job.source_file_path).resolve()
    if not source_path.exists():
        raise RuntimeError(f"Source file not found: {source_path}")

    duration = min(probe_duration_seconds(source_path), MAX_DURATION_SECONDS)
    normalized_path = output_dir / "normalized_grid_source.mov"

    canvas_w = cols * EMOJI_SIZE
    canvas_h = rows * EMOJI_SIZE

    vf = f"fps=30,{_geometry_vf(job, canvas_w, canvas_h)}"

    _run(
        [
            "ffmpeg",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(source_path),
            "-t",
            f"{duration:.3f}",
            "-an",
            "-vf",
            vf,
            "-c:v",
            "qtrle",
            "-pix_fmt",
            "argb",
            str(normalized_path),
        ],
        title="normalize video source to transparent grid canvas",
    )

    return normalized_path, duration


def _build_static_tile(
    normalized_path: Path,
    tile_dir: Path,
    index: int,
    row: int,
    col: int,
) -> TileResult:
    crop_x = col * EMOJI_SIZE
    crop_y = row * EMOJI_SIZE
    out_path = tile_dir / f"{index:02d}_r{row+1}c{col+1}.png"
    vf = f"crop={EMOJI_SIZE}:{EMOJI_SIZE}:{crop_x}:{crop_y},format=rgba"

    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(normalized_path),
            "-vf",
            vf,
            "-frames:v",
            "1",
            str(out_path),
        ],
        title=f"build static tile r{row+1} c{col+1}",
    )

    return TileResult(
        index=index,
        row=row,
        col=col,
        path=out_path,
        size_bytes=out_path.stat().st_size,
        fps=None,
        crf=None,
        ok=True,
    )

def build_static_tiles(normalized_path: Path, tile_dir: Path, cols: int, rows: int) -> list[TileResult]:
    tasks: list[tuple[int, int, int]] = []
    index = 1
    for row in range(rows):
        for col in range(cols):
            tasks.append((index, row, col))
            index += 1

    results: list[TileResult] = []

    if TILE_WORKERS <= 1 or len(tasks) <= 1:
        for idx, row, col in tasks:
            results.append(_build_static_tile(normalized_path, tile_dir, idx, row, col))
    else:
        with ThreadPoolExecutor(max_workers=TILE_WORKERS) as pool:
            futures = [
                pool.submit(_build_static_tile, normalized_path, tile_dir, idx, row, col)
                for idx, row, col in tasks
            ]
            for future in futures:
                results.append(future.result())

    results.sort(key=lambda r: r.index)
    return results


def encode_video_tile(
    normalized_path: Path,
    out_path: Path,
    col: int,
    row: int,
    fps: int,
    crf: int,
) -> int:
    crop_x = col * EMOJI_SIZE
    crop_y = row * EMOJI_SIZE

    vf = (
        f"fps={fps},"
        f"crop={EMOJI_SIZE}:{EMOJI_SIZE}:{crop_x}:{crop_y},"
        f"format=yuva420p"
    )

    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(normalized_path),
            "-an",
            "-vf",
            vf,
            "-r",
            str(fps),
            "-c:v",
            "libvpx-vp9",
            "-pix_fmt",
            "yuva420p",
            "-b:v",
            "0",
            "-crf",
            str(crf),
            "-deadline",
            VP9_DEADLINE,
            "-cpu-used",
            str(VP9_CPU_USED),
            "-row-mt",
            "1",
            "-threads",
            str(VP9_THREADS),
            "-frame-parallel",
            "0",
            "-auto-alt-ref",
            "0",
            "-lag-in-frames",
            "0",
            str(out_path),
        ],
        title=f"encode video tile r{row+1} c{col+1}",
    )

    return out_path.stat().st_size


def build_video_tile_with_limit(
    normalized_path: Path,
    tile_dir: Path,
    index: int,
    row: int,
    col: int,
) -> TileResult:
    out_path = tile_dir / f"{index:02d}_r{row+1}c{col+1}.webm"
    best_size = math.inf
    best_fps = VIDEO_ENCODE_LADDER[-1][0]
    best_crf = VIDEO_ENCODE_LADDER[-1][1]

    for fps, crf in VIDEO_ENCODE_LADDER:
        size = encode_video_tile(
            normalized_path=normalized_path,
            out_path=out_path,
            col=col,
            row=row,
            fps=fps,
            crf=crf,
        )

        if size < best_size:
            best_size = size
            best_fps = fps
            best_crf = crf

        if size <= VIDEO_MAX_FILE_SIZE_BYTES:
            return TileResult(
                index=index,
                row=row,
                col=col,
                path=out_path,
                size_bytes=size,
                fps=fps,
                crf=crf,
                ok=True,
            )

    return TileResult(
        index=index,
        row=row,
        col=col,
        path=out_path,
        size_bytes=int(best_size),
        fps=best_fps,
        crf=best_crf,
        ok=False,
    )


def build_video_tiles(normalized_path: Path, tile_dir: Path, cols: int, rows: int) -> list[TileResult]:
    tasks: list[tuple[int, int, int]] = []
    index = 1
    for row in range(rows):
        for col in range(cols):
            tasks.append((index, row, col))
            index += 1

    results: list[TileResult] = []

    if TILE_WORKERS <= 1 or len(tasks) <= 1:
        for idx, row, col in tasks:
            results.append(
                build_video_tile_with_limit(
                    normalized_path=normalized_path,
                    tile_dir=tile_dir,
                    index=idx,
                    row=row,
                    col=col,
                )
            )
    else:
        with ThreadPoolExecutor(max_workers=TILE_WORKERS) as pool:
            futures = [
                pool.submit(
                    build_video_tile_with_limit,
                    normalized_path=normalized_path,
                    tile_dir=tile_dir,
                    index=idx,
                    row=row,
                    col=col,
                )
                for idx, row, col in tasks
            ]
            for future in futures:
                results.append(future.result())

    results.sort(key=lambda r: r.index)
    return results


def convert_job_to_tiles(job: JobRecord, base_output_dir: Path) -> ConversionResult:
    ensure_ffmpeg_available()

    if not job.public_id:
        raise RuntimeError("public_id is empty")

    if not job.grid_code:
        raise RuntimeError("grid_code is empty")

    cols, rows = get_grid_size(job.grid_code)
    output_dir = ensure_job_output_dir(base_output_dir, job.public_id)
    tile_dir = output_dir / "tiles"
    tile_dir.mkdir(parents=True, exist_ok=True)

    source_kind = detect_source_kind(job)

    if source_kind == "static":
        normalized_path = normalize_static_source(job, output_dir, cols, rows)
        tile_results = build_static_tiles(normalized_path, tile_dir, cols, rows)

        return ConversionResult(
            public_id=job.public_id,
            output_dir=output_dir,
            normalized_source_path=normalized_path,
            tile_dir=tile_dir,
            rows=rows,
            cols=cols,
            duration_seconds=None,
            tile_results=tile_results,
            output_kind="static",
        )

    normalized_path, duration = normalize_video_source(job, output_dir, cols, rows)
    tile_results = build_video_tiles(normalized_path, tile_dir, cols, rows)

    failed = [x for x in tile_results if not x.ok]
    if failed:
        details = ", ".join(
            f"{item.path.name}={item.size_bytes // 1024}KB@fps{item.fps}/crf{item.crf}"
            for item in failed
        )
        raise ConversionError(f"Некоторые тайлы превышают лимит 64KB: {details}")

    return ConversionResult(
        public_id=job.public_id,
        output_dir=output_dir,
        normalized_source_path=normalized_path,
        tile_dir=tile_dir,
        rows=rows,
        cols=cols,
        duration_seconds=duration,
        tile_results=tile_results,
        output_kind="video",
    )