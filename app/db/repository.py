from __future__ import annotations

import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Optional

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


def _get_connection() -> sqlite3.Connection:
    settings = load_settings()
    conn = sqlite3.connect(settings.database_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


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
            "SELECT * FROM jobs WHERE public_id = ?",
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

def list_jobs_for_user(user_id: int, *, limit: int = 50, offset: int = 0) -> list[JobRecord]:
    with closing(_get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE user_id = ?
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
            """,
            (
                source_type,
                source_file_id,
                source_file_path,
                original_filename,
                public_id,
            ),
        )


def update_job_selection(
    *,
    public_id: str,
    orientation: Optional[str] = None,
    grid_code: Optional[str] = None,
    title: Optional[str] = None,
    short_name: Optional[str] = None,
) -> None:
    with closing(_get_connection()) as conn, conn:
        conn.execute(
            """
            UPDATE jobs
            SET orientation = COALESCE(?, orientation),
                grid_code = COALESCE(?, grid_code),
                title = COALESCE(?, title),
                short_name = COALESCE(?, short_name),
                updated_at = CURRENT_TIMESTAMP
            WHERE public_id = ?
            """,
            (orientation, grid_code, title, short_name, public_id),
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
            "SELECT * FROM jobs WHERE id = ?",
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