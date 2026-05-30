from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from app.db.repository import JobRecord
from app.core.logging_config import setup_logging
logger = setup_logging()
MAX_DURATION_SECONDS = 3.0
EMOJI_SIZE = 100

VIDEO_MAX_FILE_SIZE_BYTES = 64 * 1024
VIDEO_FPS_STEPS = [20, 15, 12, 10]
VIDEO_CRF_STEPS = [44, 48, 52, 56, 60, 62]

GRID_MAP: dict[str, tuple[int, int]] = {
    "3x3": (3, 3),
    "4x4": (4, 4),
    "5x5": (5, 5),
    "6x6": (6, 6),
    "7x7": (7, 7),
    "8x8": (8, 8),
}

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
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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


def probe_duration_seconds(source_path: Path) -> float:
    info = probe_media_info(source_path)
    raw_duration = info.get("format", {}).get("duration")
    if raw_duration is None:
        raise RuntimeError("Could not detect source duration")
    return float(raw_duration)


def get_grid_size(grid_code: str) -> tuple[int, int]:
    grid = GRID_MAP.get(grid_code)
    if not grid:
        raise RuntimeError(f"Unsupported grid_code: {grid_code}")
    return grid


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


def normalize_static_source(job: JobRecord, output_dir: Path, cols: int, rows: int) -> Path:
    if not job.source_file_path:
        raise RuntimeError("source_file_path is empty")

    source_path = Path(job.source_file_path).resolve()
    if not source_path.exists():
        raise RuntimeError(f"Source file not found: {source_path}")

    normalized_path = output_dir / "normalized_grid_source.png"
    canvas_w = cols * EMOJI_SIZE
    canvas_h = rows * EMOJI_SIZE

    vf = (
        f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:color=black@0,"
        f"format=rgba"
    )

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

    vf = (
        f"fps=30,"
        f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:color=black@0,"
        f"format=rgba"
    )

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


def build_static_tiles(normalized_path: Path, tile_dir: Path, cols: int, rows: int) -> list[TileResult]:
    results: list[TileResult] = []
    index = 1

    for row in range(rows):
        for col in range(cols):
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

            size = out_path.stat().st_size
            results.append(
                TileResult(
                    index=index,
                    row=row,
                    col=col,
                    path=out_path,
                    size_bytes=size,
                    fps=None,
                    crf=None,
                    ok=True,
                )
            )
            index += 1

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
            "best",
            "-cpu-used",
            "0",
            "-row-mt",
            "1",
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
    best_fps = VIDEO_FPS_STEPS[-1]
    best_crf = VIDEO_CRF_STEPS[-1]

    for fps in VIDEO_FPS_STEPS:
        for crf in VIDEO_CRF_STEPS:
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
    results: list[TileResult] = []
    index = 1

    for row in range(rows):
        for col in range(cols):
            tile_result = build_video_tile_with_limit(
                normalized_path=normalized_path,
                tile_dir=tile_dir,
                index=index,
                row=row,
                col=col,
            )
            results.append(tile_result)
            index += 1

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