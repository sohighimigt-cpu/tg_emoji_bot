PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    username TEXT,
    source_type TEXT NOT NULL,
    source_file_id TEXT,
    source_file_path TEXT,
    original_filename TEXT,
    orientation TEXT,
    grid_code TEXT,
    target_short_name TEXT,
    trim_start REAL NOT NULL DEFAULT 0,
    trim_duration REAL,
    title TEXT,
    short_name TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'pending', 'queued', 'processing', 'ready', 'done', 'failed', 'cancelled')),
    error_message TEXT,
    pack_url TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS job_edits (
    public_id   TEXT PRIMARY KEY,
    crop_x      REAL,
    crop_y      REAL,
    crop_w      REAL,
    crop_h      REAL,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (public_id) REFERENCES jobs(public_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_user_id_created_at ON jobs(user_id, created_at);