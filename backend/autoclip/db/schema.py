"""SQLite schema and migrations.

Migrations are driven by SQLite's ``user_version`` pragma: each entry in
:data:`MIGRATIONS` moves the database forward one version. Applying them is
idempotent, so :func:`migrate` runs safely on every startup.

We use raw ``sqlite3`` rather than an ORM. The schema is six small tables read
by one local process, so an ORM would add a dependency and a layer of
indirection without buying anything.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

_V1 = """
CREATE TABLE sources (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL CHECK (type IN ('youtube', 'upload')),
    url         TEXT,
    filename    TEXT,
    path        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    channel     TEXT,
    duration_s  REAL NOT NULL DEFAULT 0,
    width       INTEGER,
    height      INTEGER,
    fps         REAL,
    has_audio   INTEGER NOT NULL DEFAULT 1,
    has_video   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE jobs (
    id            TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    status        TEXT NOT NULL CHECK (status IN ('queued','running','failed','done','cancelled')),
    current_stage TEXT NOT NULL DEFAULT '',
    progress      REAL NOT NULL DEFAULT 0,
    error         TEXT,
    provider      TEXT NOT NULL DEFAULT '',
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);
CREATE INDEX idx_jobs_source ON jobs(source_id);
CREATE INDEX idx_jobs_status ON jobs(status);

CREATE TABLE transcripts (
    job_id          TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    json_path       TEXT NOT NULL,
    language        TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT '',
    has_diarization INTEGER NOT NULL DEFAULT 0,
    word_count      INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT 'whisper',
    created_at      TEXT NOT NULL
);

CREATE TABLE clips (
    id           TEXT PRIMARY KEY,
    job_id       TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    rank         INTEGER NOT NULL DEFAULT 0,
    start_s      REAL NOT NULL,
    end_s        REAL NOT NULL,
    start_word   INTEGER NOT NULL DEFAULT 0,
    end_word     INTEGER NOT NULL DEFAULT 0,
    title        TEXT NOT NULL DEFAULT '',
    hook         TEXT NOT NULL DEFAULT '',
    score        INTEGER NOT NULL DEFAULT 0,
    reason       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'candidate'
                 CHECK (status IN ('candidate','kept','discarded','exported')),
    user_trimmed INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX idx_clips_job ON clips(job_id, rank);

CREATE TABLE clip_edits (
    clip_id           TEXT PRIMARY KEY REFERENCES clips(id) ON DELETE CASCADE,
    edited_words_json TEXT,
    caption_style     TEXT NOT NULL DEFAULT 'bold_pop',
    ratio             TEXT NOT NULL DEFAULT '9:16',
    updated_at        TEXT NOT NULL
);

CREATE TABLE exports (
    id         TEXT PRIMARY KEY,
    clip_id    TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    ratio      TEXT NOT NULL DEFAULT '9:16',
    style      TEXT NOT NULL DEFAULT 'bold_pop',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_exports_clip ON exports(clip_id);
"""


def _migration_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(_V1)


def _migration_v2(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        ALTER TABLE clip_edits ADD COLUMN crop_nudge REAL NOT NULL DEFAULT 0;
        ALTER TABLE clip_edits ADD COLUMN centre_crop INTEGER NOT NULL DEFAULT 0;
        """
    )


def _migration_v3(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        ALTER TABLE clip_edits ADD COLUMN pip_enabled INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE clip_edits ADD COLUMN pip_corner TEXT NOT NULL DEFAULT 'auto';
        ALTER TABLE clip_edits ADD COLUMN pip_size REAL NOT NULL DEFAULT 0.35;
        """
    )


def _migration_v4(conn: sqlite3.Connection) -> None:
    # Nullable and additive: a clip with no stored styling resolves exactly as it
    # did before, so an existing job needs no data migration. ``style_overrides``
    # keeps the ``_json`` suffix the neighbouring column uses.
    conn.executescript(
        """
        ALTER TABLE clip_edits ADD COLUMN style_overrides_json TEXT;
        ALTER TABLE clip_edits ADD COLUMN caption_margin_ratio REAL;
        ALTER TABLE clip_edits ADD COLUMN caption_x_ratio REAL;
        """
    )


def _migration_v5(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE presets (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL UNIQUE,
            base_style      TEXT NOT NULL DEFAULT 'bold_pop',
            style_overrides TEXT,
            layout          TEXT,
            is_default      INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL
        );
        -- At most one default, enforced where it cannot be forgotten: the
        -- partial index makes a second one fail rather than silently win.
        CREATE UNIQUE INDEX idx_presets_default ON presets(is_default) WHERE is_default = 1;
        """
    )


#: Ordered migrations. Index + 1 is the resulting ``user_version``.
#: Append only — never edit a migration that has shipped.
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migration_v1,
    _migration_v2,
    _migration_v3,
    _migration_v4,
    _migration_v5,
]

SCHEMA_VERSION = len(MIGRATIONS)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any outstanding migrations. Returns the resulting schema version."""
    current: int = conn.execute("PRAGMA user_version").fetchone()[0]

    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than this AutoClip "
            f"build supports ({SCHEMA_VERSION}). Upgrade AutoClip."
        )

    for version in range(current, SCHEMA_VERSION):
        migration = MIGRATIONS[version]
        with conn:
            migration(conn)
            # PRAGMA doesn't accept bound parameters; version is loop-controlled.
            conn.execute(f"PRAGMA user_version = {version + 1}")

    return SCHEMA_VERSION
