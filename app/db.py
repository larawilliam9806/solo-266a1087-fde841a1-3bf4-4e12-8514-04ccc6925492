"""SQLite schema and connection helpers.

The service keeps a small amount of relational state, so plain ``sqlite3`` with
hand-written SQL is a better fit than an ORM.  Each request opens its own
short-lived connection; writes run inside ``BEGIN IMMEDIATE`` so version
numbers are assigned without races even with several uvicorn workers.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = "/data/subtitles.db"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 原片: the source films the documentary was cut from.
CREATE TABLE IF NOT EXISTS source_reels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 成片: the finished film that gets broadcast.
CREATE TABLE IF NOT EXISTS films (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 已审台词, versioned per source reel.
CREATE TABLE IF NOT EXISTS subtitle_documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_reel_id  INTEGER NOT NULL REFERENCES source_reels(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (source_reel_id, version)
);

CREATE TABLE IF NOT EXISTS subtitle_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES subtitle_documents(id) ON DELETE CASCADE,
    number      INTEGER NOT NULL,
    start_ms    INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms      INTEGER NOT NULL CHECK (end_ms > start_ms),
    text        TEXT    NOT NULL,
    UNIQUE (document_id, number)
);

-- 剪辑表, versioned per finished film.
CREATE TABLE IF NOT EXISTS edit_tables (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    film_id     INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (film_id, version)
);

CREATE TABLE IF NOT EXISTS edit_segments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    edit_table_id    INTEGER NOT NULL REFERENCES edit_tables(id) ON DELETE CASCADE,
    position         INTEGER NOT NULL,
    source_reel_id   INTEGER NOT NULL REFERENCES source_reels(id),
    source_start_ms  INTEGER NOT NULL CHECK (source_start_ms >= 0),
    source_end_ms    INTEGER NOT NULL CHECK (source_end_ms > source_start_ms),
    output_start_ms  INTEGER NOT NULL CHECK (output_start_ms >= 0),
    UNIQUE (edit_table_id, position)
);

-- 一次交付: a film's edit table plus the exact source-subtitle versions it was
-- prepared against.  While open it may be re-pointed at newer versions.
CREATE TABLE IF NOT EXISTS deliveries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    film_id       INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
    edit_table_id INTEGER NOT NULL REFERENCES edit_tables(id),
    state         TEXT    NOT NULL DEFAULT 'open'
                  CHECK (state IN ('open', 'published', 'rejected')),
    reject_reason TEXT,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    published_at  TEXT
);

CREATE TABLE IF NOT EXISTS delivery_pins (
    delivery_id     INTEGER NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
    source_reel_id  INTEGER NOT NULL REFERENCES source_reels(id),
    subtitle_version INTEGER NOT NULL,
    PRIMARY KEY (delivery_id, source_reel_id)
);

CREATE INDEX IF NOT EXISTS ix_deliveries_film ON deliveries(film_id, state);
CREATE INDEX IF NOT EXISTS ix_edit_segments_table ON edit_segments(edit_table_id);
CREATE INDEX IF NOT EXISTS ix_subtitle_lines_doc ON subtitle_lines(document_id);

-- Successful deliveries, frozen for the broadcast side.
CREATE TABLE IF NOT EXISTS publications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    film_id         INTEGER NOT NULL REFERENCES films(id) ON DELETE CASCADE,
    delivery_id     INTEGER NOT NULL REFERENCES deliveries(id),
    output_version  INTEGER NOT NULL,
    edit_version    INTEGER NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (film_id, output_version)
);

CREATE TABLE IF NOT EXISTS publication_sources (
    publication_id   INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    source_reel_id   INTEGER NOT NULL,
    source_reel_name TEXT    NOT NULL,
    subtitle_version INTEGER NOT NULL,
    PRIMARY KEY (publication_id, source_reel_id)
);

-- 字幕: one row per retained line occurrence, in finished-film timecode order.
CREATE TABLE IF NOT EXISTS publication_cues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id  INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    ordinal         INTEGER NOT NULL,
    source_reel_id  INTEGER NOT NULL,
    source_reel_name TEXT   NOT NULL,
    line_number     INTEGER NOT NULL,
    text            TEXT    NOT NULL,
    start_ms        INTEGER NOT NULL,
    end_ms          INTEGER NOT NULL
);

-- 待复核半句: each place a reviewed line was only partly retained.
CREATE TABLE IF NOT EXISTS publication_reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id  INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    ordinal         INTEGER NOT NULL,
    source_reel_id  INTEGER NOT NULL,
    source_reel_name TEXT   NOT NULL,
    line_number     INTEGER NOT NULL,
    text            TEXT    NOT NULL,
    keep_start_ms   INTEGER NOT NULL,
    keep_end_ms     INTEGER NOT NULL
);
"""


def db_path() -> Path:
    return Path(os.environ.get("SUBTITLE_DB_PATH", DEFAULT_DB_PATH))


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path) if path is not None else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: Path | str | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def check_writable(path: Path | str | None = None) -> None:
    """Fail fast at startup instead of on the first request."""

    with write_conn(path) as conn:
        conn.execute("SELECT 1")


@contextmanager
def read_conn(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_conn(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
