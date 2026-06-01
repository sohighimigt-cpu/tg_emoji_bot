from __future__ import annotations

import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Optional
from functools import lru_cache
from app.core.config import load_settings


@dataclass
class JobRecord:
    id: int
    public_id: str
    user_id: int
    chat_id: int
    username: Optional[str]
    source_type: str
    source_file_id: Optional[str]
    source_file_path: Optional[str]
    original_filename: Optional[str]
    orientation: Optional[str]
    grid_code: Optional[str]
    title: Optional[str]
    short_name: Optional[str]
    status: str
    error_message: Optional[str]
    pack_url: Optional[str]
    created_at: str
    updated_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    target_short_name: Optional[str] = None
    trim_start: float = 0.0
    trim_duration: Optional[float] = None
    target_short_name: Optional[str] = None
    trim_start: float = 0.0
    trim_duration: Optional[float] = None
    crop_x: Optional[float] = None
    crop_y: Optional[float] = None
    crop_w: Optional[float] = None
    crop_h: Optional[float] = None

_ACTIVE_JOB_STATUSES = ("draft", "queued", "processing", "ready")



@lru_cache(maxsize=1)
def _database_path() -> str:
    return load_settings().database_path


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_database_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  
    conn.execute("PRAGMA journal_mode = WAL")  
    return conn



def get_active_public_ids() -> set[str]:
    """public_id задач, чьи файлы ещё нужны (не терминальные статусы)."""
    placeholders = ",".join("?" for _ in _ACTIVE_JOB_STATUSES)
    with closing(_get_connection()) as conn:
        cursor = conn.execute(
            f"SELECT public_id FROM jobs WHERE status IN ({placeholders})",
            _ACTIVE_JOB_STATUSES,
        )
        return {row[0] for row in cursor.fetchall()}
def _row_value(row: sqlite3.Row, key: str, default=None):
    return row[key] if key in row.keys() else default

# ... внутри _row_to_job, после trim_duration:
        
def _row_to_job(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        id=row["id"],
        public_id=row["public_id"],
        user_id=row["user_id"],
        chat_id=row["chat_id"],
        username=row["username"],
        source_type=row["source_type"],
        source_file_id=row["source_file_id"],
        source_file_path=row["source_file_path"],
        original_filename=row["original_filename"],
        orientation=row["orientation"],
        grid_code=row["grid_code"],
        title=row["title"],
        short_name=row["short_name"],
        status=row["status"],
        error_message=row["error_message"],
        pack_url=row["pack_url"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        target_short_name=row["target_short_name"],
        trim_start=row["trim_start"] if row["trim_start"] is not None else 0.0,
        trim_duration=row["trim_duration"],
        crop_x=_row_value(row, "crop_x"),
        crop_y=_row_value(row, "crop_y"),
        crop_w=_row_value(row, "crop_w"),
        crop_h=_row_value(row, "crop_h"),
    )



def generate_public_id() -> str:
    return secrets.token_urlsafe(9)


def create_job(
    *,
    user_id: int,
    chat_id: int,
    username: Optional[str],
    source_type: str,
    source_file_id: Optional[str] = None,
    source_file_path: Optional[str] = None,
    original_filename: Optional[str] = None,
) -> JobRecord:
    public_id = generate_public_id()

    with closing(_get_connection()) as conn, conn:
        cursor = conn.execute(
            """
            INSERT INTO jobs (
                public_id,
                user_id,
                chat_id,
                username,
                source_type,
                source_file_id,
                source_file_path,
                original_filename,
                status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft')
            """,
            (
                public_id,
                user_id,
                chat_id,
                username,
                source_type,
                source_file_id,
                source_file_path,
                original_filename,
            ),
        )
        job_id = cursor.lastrowid

        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()

    return _row_to_job(row)


def get_job_by_public_id(public_id: str) -> Optional[JobRecord]:
    with closing(_get_connection()) as conn:
        row = conn.execute(
            """
            SELECT j.*, e.crop_x, e.crop_y, e.crop_w, e.crop_h
            FROM jobs j
            LEFT JOIN job_edits e ON e.public_id = j.public_id
            WHERE j.public_id = ?
            """,
            (public_id,),
        ).fetchone()
        return _row_to_job(row) if row else None
    


def get_job_by_public_id_for_user(public_id: str, user_id: int) -> Optional[JobRecord]:
    with closing(_get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE public_id = ? AND user_id = ?",
            (public_id, user_id),
        ).fetchone()
    return _row_to_job(row) if row else None


def get_latest_job_for_user(user_id: int) -> Optional[JobRecord]:
    with closing(_get_connection()) as conn:
        row = conn.execute(
            """
            SELECT * FROM jobs
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    return _row_to_job(row) if row else None

def list_jobs_for_user(
    user_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
    include_additions: bool = False,
) -> list[JobRecord]:
    addition_clause = "" if include_additions else "AND target_short_name IS NULL"
    with closing(_get_connection()) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE user_id = ?
              {addition_clause}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (user_id, limit, offset),
        ).fetchall()
        return [_row_to_job(row) for row in rows]


def get_active_job_for_user(user_id: int) -> Optional[JobRecord]:
    with closing(_get_connection()) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE user_id = ?
              AND status IN ('draft', 'queued', 'processing')
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    return _row_to_job(row) if row else None

def count_inflight_jobs_for_user(user_id: int) -> int:
    with closing(_get_connection()) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM jobs
            WHERE user_id = ?
              AND status IN ('queued', 'processing')
            """,
            (user_id,),
        ).fetchone()
        return int(row["c"]) if row else 0


def set_job_source(
    *,
    public_id: str,
    user_id: int,
    source_type: str,
    source_file_id: Optional[str],
    source_file_path: Optional[str],
    original_filename: Optional[str],
) -> None:
    with closing(_get_connection()) as conn, conn:
        conn.execute(
            """
            UPDATE jobs
            SET source_type = ?,
                source_file_id = ?,
                source_file_path = ?,
                original_filename = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE public_id = ?
              AND user_id = ?
            """,
            (
                source_type,
                source_file_id,
                source_file_path,
                original_filename,
                public_id,
                user_id,
            ),
        )


def update_job_selection(
    *,
    public_id: str,
    orientation: Optional[str] = None,
    grid_code: Optional[str] = None,
    title: Optional[str] = None,
    short_name: Optional[str] = None,
    target_short_name: Optional[str] = None,
    trim_start: Optional[float] = None,
    trim_duration: Optional[float] = None,
) -> None:
    with closing(_get_connection()) as conn, conn:
        conn.execute(
            """
            UPDATE jobs
            SET orientation = COALESCE(?, orientation),
                grid_code = COALESCE(?, grid_code),
                title = COALESCE(?, title),
                short_name = COALESCE(?, short_name),
                target_short_name = COALESCE(?, target_short_name),
                trim_start = COALESCE(?, trim_start),
                trim_duration = COALESCE(?, trim_duration),
                updated_at = CURRENT_TIMESTAMP
            WHERE public_id = ?
            """,
            (orientation, grid_code, title, short_name,
             target_short_name, trim_start, trim_duration, public_id),
        )

def upsert_job_crop(
    *,
    public_id: str,
    crop_x: float,
    crop_y: float,
    crop_w: float,
    crop_h: float,
) -> None:
    with closing(_get_connection()) as conn, conn:
        conn.execute(
            """
            INSERT INTO job_edits (public_id, crop_x, crop_y, crop_w, crop_h, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(public_id) DO UPDATE SET
                crop_x = excluded.crop_x,
                crop_y = excluded.crop_y,
                crop_w = excluded.crop_w,
                crop_h = excluded.crop_h,
                updated_at = CURRENT_TIMESTAMP
            """,
            (public_id, crop_x, crop_y, crop_w, crop_h),
        )

def set_job_title_and_short_name(public_id: str, title: str, short_name: str) -> None:
    with closing(_get_connection()) as conn, conn:
        conn.execute(
            """
            UPDATE jobs
            SET title = ?,
                short_name = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE public_id = ?
            """,
            (title, short_name, public_id),
        )
        
def short_name_exists(short_name: str) -> bool:
    with closing(_get_connection()) as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE short_name = ? LIMIT 1",
            (short_name,),
        ).fetchone()
        return row is not None

def mark_job_ready(public_id: str) -> None:
    with closing(_get_connection()) as conn, conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'queued',
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE public_id = ?
              AND status = 'draft'
            """,
            (public_id,),
        )


def mark_job_ready_for_user(public_id: str, user_id: int) -> None:
    with closing(_get_connection()) as conn, conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'queued',
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE public_id = ?
              AND user_id = ?
              AND status = 'draft'
            """,
            (public_id, user_id),
        )


def claim_next_queued_job() -> Optional[JobRecord]:
    with closing(_get_connection()) as conn, conn:
        row = conn.execute(
            """
            SELECT id
            FROM jobs
            WHERE status = 'queued'
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()

        if row is None:
            return None

        updated = conn.execute(
            """
            UPDATE jobs
            SET status = 'processing',
                started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP,
                error_message = NULL
            WHERE id = ?
              AND status = 'queued'
            """,
            (row["id"],),
        )

        if updated.rowcount != 1:
            return None

        claimed_row = conn.execute(
            """
            SELECT j.*, e.crop_x, e.crop_y, e.crop_w, e.crop_h
            FROM jobs j
            LEFT JOIN job_edits e ON e.public_id = j.public_id
            WHERE j.id = ?
            """,
            (row["id"],),
        ).fetchone()

    return _row_to_job(claimed_row)


def mark_job_done(public_id: str, pack_url: Optional[str] = None) -> None:
    with closing(_get_connection()) as conn, conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'done',
                pack_url = COALESCE(?, pack_url),
                updated_at = CURRENT_TIMESTAMP,
                finished_at = CURRENT_TIMESTAMP
            WHERE public_id = ?
              AND status = 'processing'
            """,
            (pack_url, public_id),
        )


def mark_job_failed(public_id: str, error_message: str) -> None:
    with closing(_get_connection()) as conn, conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'failed',
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP,
                finished_at = CURRENT_TIMESTAMP
            WHERE public_id = ?
              AND status = 'processing'
            """,
            ((error_message or "unknown error")[:1000], public_id),
        )


def cancel_job(public_id: str, error_message: str = "cancelled_by_user") -> None:
    with closing(_get_connection()) as conn, conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'cancelled',
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP,
                finished_at = CURRENT_TIMESTAMP
            WHERE public_id = ?
              AND status IN ('draft', 'queued', 'processing')
            """,
            ((error_message or "cancelled_by_user")[:1000], public_id),
        )
        
def delete_job_for_user(public_id: str, user_id: int) -> bool:
    with closing(_get_connection()) as conn, conn:
        cursor = conn.execute(
            "DELETE FROM jobs WHERE public_id = ? AND user_id = ?",
            (public_id, user_id),
        )
        return cursor.rowcount > 0