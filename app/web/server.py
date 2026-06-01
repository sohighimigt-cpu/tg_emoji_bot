from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.db.repository import short_name_exists
from app.domain.pack_naming import build_unique_short_name
from app.core.config import load_settings
from app.db.repository import (
    create_job,
    get_job_by_public_id_for_user,
    mark_job_ready_for_user,
    set_job_source,
    list_jobs_for_user,
    count_inflight_jobs_for_user,
    update_job_selection,
    upsert_job_crop,
    delete_job_for_user,
)
from app.domain.pack_naming import build_short_name
from app.domain.pack_options import (
    GRID_OPTIONS_BY_ORIENTATION,
    ORIENTATION_OPTIONS,
    is_allowed_grid,
    is_allowed_orientation,
)
from app.services.storage import build_job_input_path, remove_job_dirs
from app.web.api.auth import (
    MiniAppAuthError,
    build_user_display_name,
    validate_telegram_init_data,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
MINIAPP_INDEX = TEMPLATES_DIR / "miniapp" / "index.html"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_INFLIGHT_JOBS_PER_USER = int(os.getenv("MAX_INFLIGHT_JOBS_PER_USER", "1"))
UPLOAD_CHUNK_SIZE = 1024 * 1024 
ALLOWED_UPLOAD_EXTENSIONS: dict[str, set[str]] = {
    "photo": {".jpg", ".jpeg", ".png", ".webp"},
    "animation": {".gif"},
    "video": {".mp4", ".webm", ".mov"},
}
_EXT_TO_SOURCE_TYPE = {
    ext: stype
    for stype, exts in ALLOWED_UPLOAD_EXTENSIONS.items()
    for ext in exts
}
ALLOWED_EXTENSIONS = set(_EXT_TO_SOURCE_TYPE)

ALLOWED_MIME_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "video/mp4", "video/webm", "video/quicktime",
}


class JobResponse(BaseModel):
    public_id: str
    status: str
    title: str | None
    short_name: str | None
    orientation: str | None
    grid_code: str | None
    pack_url: str | None
    error_message: str | None

class JobHistoryItem(BaseModel):
    public_id: str
    status: str
    title: str | None
    short_name: str | None
    orientation: str | None
    grid_code: str | None
    pack_url: str | None
    created_at: str

class MiniAppAuthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    init_data: str = Field(min_length=1, max_length=8192)


class JobSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=64)
    orientation: str = Field(min_length=1, max_length=32)
    grid_code: str = Field(min_length=1, max_length=32)
    crop_x: float | None = Field(default=None, ge=0, le=1)
    crop_y: float | None = Field(default=None, ge=0, le=1)
    crop_w: float | None = Field(default=None, gt=0, le=1)
    crop_h: float | None = Field(default=None, gt=0, le=1)    

    @model_validator(mode="after")
    def validate_crop(self):
        parts = (self.crop_x, self.crop_y, self.crop_w, self.crop_h)
        provided = [p is not None for p in parts]
        if any(provided) and not all(provided):
            raise ValueError("crop requires all of crop_x, crop_y, crop_w, crop_h")
        if all(provided):
            if self.crop_x + self.crop_w > 1.0 + 1e-6:
                raise ValueError("crop x+w out of bounds")
            if self.crop_y + self.crop_h > 1.0 + 1e-6:
                raise ValueError("crop y+h out of bounds")
        return self
    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("title must not be empty")
        return cleaned

    @field_validator("orientation")
    @classmethod
    def validate_orientation(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not is_allowed_orientation(cleaned):
            raise ValueError("unsupported orientation")
        return cleaned

    @field_validator("grid_code")
    @classmethod
    def validate_grid_code(cls, value: str) -> str:
        return value.strip().lower()


class UpdateJobRequest(JobSelectionRequest):
    pass


class CreateJobRequest(JobSelectionRequest):
    add_to_short_name: str | None = Field(default=None, max_length=64)

    @field_validator("add_to_short_name")
    @classmethod
    def validate_add_to_short_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()

    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is not configured")

    if not MINIAPP_INDEX.exists():
        raise RuntimeError(f"Mini App index not found: {MINIAPP_INDEX}")

    app.state.settings = settings
    yield


app = FastAPI(
    title="telegram_emoji_bot mini app",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=STATIC_DIR, html=False), name="static")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)

    csp = (
        "default-src 'self'; "
        "script-src 'self' https://telegram.org; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https://t.me https://*.telegram.org https://*.telegram-cdn.org; "
        "media-src 'self' blob: data:; "
        "connect-src 'self'; "
        "font-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors https://web.telegram.org; "
        "upgrade-insecure-requests"
    )

    response.headers["Content-Security-Policy"] = csp
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"

    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    return response


@app.exception_handler(MiniAppAuthError)
async def miniapp_auth_error_handler(_: Request, exc: MiniAppAuthError):
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"ok": False, "error": str(exc)},
    )


def _extract_verified_user(authorization: str | None, bot_token: str):
    prefix = "TelegramInitData "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Telegram init data authorization",
        )

    init_data_raw = authorization[len(prefix):].strip()
    if not init_data_raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty Telegram init data authorization",
        )

    try:
        return validate_telegram_init_data(
            init_data_raw,
            bot_token,
            max_age_seconds=3600,
        )
    except MiniAppAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc


def _job_response(job) -> JobResponse:
    return JobResponse(
        public_id=job.public_id,
        status=job.status,
        title=job.title,
        short_name=job.short_name,
        orientation=job.orientation,
        grid_code=job.grid_code,
        pack_url=job.pack_url,
        error_message=job.error_message,
    )


@app.get("/healthz")
async def healthcheck():
    return {"ok": True}

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    icon_path = STATIC_DIR / "favicon.ico"
    if not icon_path.exists():
        return Response(status_code=204)
    return FileResponse(icon_path, media_type="image/x-icon")

@app.get("/miniapp")
async def miniapp_index():
    return FileResponse(
        MINIAPP_INDEX,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/miniapp/auth")
async def miniapp_auth(payload: MiniAppAuthRequest, request: Request):
    settings = request.app.state.settings

    verified = validate_telegram_init_data(
        payload.init_data,
        settings.bot_token,
        max_age_seconds=3600,
    )

    return {
        "ok": True,
        "user": {
            "id": verified.user.id,
            "username": verified.user.username,
            "first_name": verified.user.first_name,
            "last_name": verified.user.last_name,
            "language_code": verified.user.language_code,
            "is_premium": verified.user.is_premium,
            "display_name": build_user_display_name(verified.user),
        },
        "auth_date": verified.auth_date,
        "query_id": verified.query_id,
        "orientation_options": ORIENTATION_OPTIONS,
        "grid_options_by_orientation": GRID_OPTIONS_BY_ORIENTATION,
    }


@app.post("/api/miniapp/jobs", response_model=JobResponse)
async def create_miniapp_job(
    payload: CreateJobRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    settings = request.app.state.settings
    verified = _extract_verified_user(authorization, settings.bot_token)

    if not is_allowed_grid(payload.orientation, payload.grid_code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid grid for selected orientation",
        )

    job = create_job(
        user_id=verified.user.id,
        chat_id=verified.user.id,
        username=verified.user.username,
        source_type="pending",
    )

    if payload.add_to_short_name:
        short_name = payload.add_to_short_name
    else:
        short_name = build_unique_short_name(
            payload.title, settings.bot_username, exists=short_name_exists
        )

    update_job_selection(
        public_id=job.public_id,
        orientation=payload.orientation,
        grid_code=payload.grid_code,
        title=payload.title,
        short_name=short_name,
        target_short_name=payload.add_to_short_name,
    )
    
    if payload.crop_x is not None:
        upsert_job_crop(
            public_id=job.public_id,   # в create_miniapp_job: public_id=job.public_id
            crop_x=payload.crop_x,
            crop_y=payload.crop_y,
            crop_w=payload.crop_w,
            crop_h=payload.crop_h,
    )

    updated = get_job_by_public_id_for_user(job.public_id, verified.user.id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Job not found after creation")

    return _job_response(updated)

@app.get("/api/miniapp/history")
async def miniapp_history(
    request: Request,
    authorization: str | None = Header(default=None),
):
    settings = request.app.state.settings
    verified = _extract_verified_user(authorization, settings.bot_token)
    jobs = list_jobs_for_user(verified.user.id, limit=50)
    return {
        "items": [
            JobHistoryItem(
                public_id=j.public_id,
                status=j.status,
                title=j.title,
                short_name=j.short_name,
                orientation=j.orientation,
                grid_code=j.grid_code,
                pack_url=j.pack_url,
                created_at=j.created_at,
            )
            for j in jobs
        ]
    }

@app.post("/api/miniapp/jobs/{public_id}/upload", response_model=JobResponse)
async def upload_job_source(
    public_id: str,
    request: Request,
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    settings = request.app.state.settings
    verified = _extract_verified_user(authorization, settings.bot_token)

    job = get_job_by_public_id_for_user(public_id, verified.user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Max {MAX_UPLOAD_MB} MB.",
                )
        except ValueError:
            pass  # битый заголовок — поймает потоковый guard ниже

    original_filename = file.filename or "upload.bin"

    # --- whitelist: расширение это жёсткий барьер ---
    suffix = (Path(original_filename).suffix or "").lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail="Unsupported file extension. Allowed: "
            + ", ".join(sorted(ALLOWED_EXTENSIONS)),
        )

    # --- MIME: вторичная проверка; пустой/octet-stream допускаем, т.к. расширение уже проверено ---
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type and content_type not in ALLOWED_MIME_TYPES | {"application/octet-stream"}:
        raise HTTPException(status_code=415, detail="Unsupported content type.")

    # source_type выводим из проверенного расширения, а не из слов клиента
    source_type = _EXT_TO_SOURCE_TYPE[suffix]

    destination = build_job_input_path(
        settings.input_dir,
        public_id,
        original_filename,
        suffix,
    )

    # Жёсткий лимит размера на лету, независимо от заголовков
    written = 0
    try:
        with destination.open("wb") as buffer:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large. Max {MAX_UPLOAD_MB} MB.",
                    )
                buffer.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    set_job_source(
        public_id=public_id,
        user_id=verified.user.id, 
        source_type=source_type,
        source_file_id=None,
        source_file_path=str(destination),
        original_filename=original_filename,
    )

    updated = get_job_by_public_id_for_user(public_id, verified.user.id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Job not found after upload")

    return _job_response(updated)


@app.patch("/api/miniapp/jobs/{public_id}", response_model=JobResponse)
async def update_miniapp_job(
    public_id: str,
    payload: UpdateJobRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    settings = request.app.state.settings
    verified = _extract_verified_user(authorization, settings.bot_token)

    job = get_job_by_public_id_for_user(public_id, verified.user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if not is_allowed_grid(payload.orientation, payload.grid_code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid grid for selected orientation",
        )

    short_name = build_unique_short_name(
        payload.title, settings.bot_username, exists=short_name_exists
    )

    update_job_selection(
        public_id=public_id,
        orientation=payload.orientation,
        grid_code=payload.grid_code,
        title=payload.title,
        short_name=short_name,
    )
    
    if payload.crop_x is not None:
        upsert_job_crop(
            public_id=public_id,   # в create_miniapp_job: public_id=job.public_id
            crop_x=payload.crop_x,
            crop_y=payload.crop_y,
            crop_w=payload.crop_w,
            crop_h=payload.crop_h,
    )

    updated = get_job_by_public_id_for_user(public_id, verified.user.id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Job not found after update")

    return _job_response(updated)


@app.post("/api/miniapp/jobs/{public_id}/start", response_model=JobResponse)
async def start_miniapp_job(
    public_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    settings = request.app.state.settings
    verified = _extract_verified_user(authorization, settings.bot_token)

    job = get_job_by_public_id_for_user(public_id, verified.user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if not job.source_file_path:
        raise HTTPException(status_code=400, detail="Source file is missing")
    if not job.orientation:
        raise HTTPException(status_code=400, detail="Orientation is missing")
    if not job.grid_code:
        raise HTTPException(status_code=400, detail="Grid is missing")
    if not job.title:
        raise HTTPException(status_code=400, detail="Title is missing")
    if not job.short_name:
        raise HTTPException(status_code=400, detail="Short name is missing")
    if count_inflight_jobs_for_user(verified.user.id) >= MAX_INFLIGHT_JOBS_PER_USER:
        raise HTTPException(
            status_code=429,
            detail="У вас уже есть задача в обработке. Дождитесь её завершения.",
        )

    mark_job_ready_for_user(public_id, verified.user.id)

    updated = get_job_by_public_id_for_user(public_id, verified.user.id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Job not found after start")

    return _job_response(updated)


@app.get("/api/miniapp/jobs/{public_id}", response_model=JobResponse)
async def get_miniapp_job(
    public_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    settings = request.app.state.settings
    verified = _extract_verified_user(authorization, settings.bot_token)

    job = get_job_by_public_id_for_user(public_id, verified.user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return _job_response(job)

@app.delete("/api/miniapp/jobs/{public_id}")
async def delete_miniapp_job(
    public_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    settings = request.app.state.settings
    verified = _extract_verified_user(authorization, settings.bot_token)

    job = get_job_by_public_id_for_user(public_id, verified.user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status in {"queued", "processing"}:
        raise HTTPException(
            status_code=409,
            detail="Нельзя удалить задачу, пока она обрабатывается.",
        )

    deleted = delete_job_for_user(public_id, verified.user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Задача не найдена.")

    # подчищаем файлы задачи (воркер мог не успеть, либо задача не обрабатывалась)
    settings = load_settings()
    remove_job_dirs(settings.input_dir, settings.output_dir, public_id)

    return {"ok": True, "public_id": public_id}