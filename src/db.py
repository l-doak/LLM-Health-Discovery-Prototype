"""
db.py
=====

SQLite storage layer + Pydantic data models for the pipeline.

WHY PYDANTIC MODELS + SQLITE TOGETHER
--------------------------------------
The project brief says: "when we change [the schema], update docs/schema.md
and any Pydantic models together in the same change." The idea is that the
Pydantic model IS the schema — docs/schema.md describes it in prose, and the
SQLite table mirrors its fields. If you add a field, you touch three things
in one commit: the Pydantic model here, the CREATE TABLE statement here, and
docs/schema.md. Keeping them in the same file makes it hard to forget one.

WHY SQLITE
----------
Zero infrastructure (it's a single file on disk), but still gives real
relational-DB experience — foreign keys, queries, an actual schema — which
the job listing calls out as desirable. A production system would likely
use Postgres for concurrent writes, but for a single-user prototype SQLite
is the right-sized tool.

WHAT'S IN THIS FILE
--------------------
1. `Source`      — a candidate source found during discovery.
2. `ContentDraft`— a generated piece of app content (built in a later
                    stage, but modelled now so the schema is settled
                    early — see docs/schema.md).
3. `init_db()`   — creates both tables if they don't exist yet.
4. CRUD-ish helper functions for `Source`, since that's what the
   discovery stage needs today. Draft-related helpers will be added when
   we build draft.py, so this file doesn't get ahead of itself with
   untested code for a stage we haven't built yet.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# The SQLite database file. Sits in data/ alongside allowlist.json and the
# audit log — everything the pipeline reads/writes lives under data/.
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "steadi.db"

# --- Controlled vocabularies -------------------------------------------
# Pydantic's Literal type enforces these at the Python level (an invalid
# value raises a validation error before it ever reaches the database).
# This is what the project brief means by "schema discipline" — you
# cannot accidentally store status="aproved" (typo) or an unlisted
# category.

SourceStatus = Literal["discovered", "pending_review", "approved", "rejected"]

ContentCategory = Literal[
    "mobility", "medication", "home-safety", "nutrition", "general-frailty"
]
ContentStatus = Literal["pending_review", "approved", "rejected", "published"]


# --- Pydantic models -----------------------------------------------------


class Source(BaseModel):
    """A candidate health-information source found by the discovery stage.

    `status` tracks where this source is in the pipeline:
      discovered      -> just found and stored by discovery.py, not yet summarized
      pending_review  -> a structured summary exists, waiting on a human decision
      approved        -> a human approved it; eligible for draft generation
      rejected        -> a human rejected it; pipeline stops here for this source
    """

    source_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    url: str
    domain: str
    title: str
    raw_text: str
    status: SourceStatus = "discovered"
    discovered_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ContentDraft(BaseModel):
    """A generated app-content draft, matching docs/schema.md.

    Not used yet (draft.py doesn't exist until the draft-generation stage),
    but modelled here now so the schema is decided deliberately up front
    rather than improvised later. See docs/schema.md for the field-by-field
    rationale.
    """

    content_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    body: str
    source_url: str
    source_credibility_notes: str
    category: ContentCategory
    tags: list[str]
    reading_level: str
    reviewed_by: str | None = None
    status: ContentStatus = "pending_review"
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    model_version: str
    prompt_version: str


# --- Schema setup ---------------------------------------------------------


def get_connection() -> sqlite3.Connection:
    """Open a connection to the SQLite database.

    `row_factory = sqlite3.Row` lets us access columns by name (row["url"])
    instead of by position (row[1]) — much less error-prone as the schema
    grows.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the `sources` and `content_drafts` tables if they don't exist.

    Safe to call every time the app starts — `CREATE TABLE IF NOT EXISTS`
    is a no-op once the tables are already there.
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sources (
                source_id      TEXT PRIMARY KEY,
                url            TEXT NOT NULL UNIQUE,
                domain         TEXT NOT NULL,
                title          TEXT NOT NULL,
                raw_text       TEXT NOT NULL,
                status         TEXT NOT NULL,
                discovered_at  TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS content_drafts (
                content_id                TEXT PRIMARY KEY,
                title                     TEXT NOT NULL,
                body                      TEXT NOT NULL,
                source_url                TEXT NOT NULL,
                source_credibility_notes  TEXT,
                category                  TEXT NOT NULL,
                tags                      TEXT NOT NULL,  -- JSON-encoded list
                reading_level             TEXT,
                reviewed_by               TEXT,
                status                    TEXT NOT NULL,
                generated_at              TEXT NOT NULL,
                model_version             TEXT NOT NULL,
                prompt_version            TEXT NOT NULL,
                FOREIGN KEY (source_url) REFERENCES sources (url)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


# --- Source CRUD helpers --------------------------------------------------
#
# These are intentionally thin wrappers around plain SQL rather than a
# heavier ORM (e.g. SQLAlchemy). For a prototype with two small tables,
# raw SQL is easier to read and debug than ORM abstractions — worth
# revisiting if the schema grows a lot more relationships.


def insert_source(source: Source) -> None:
    """Insert a new source. Raises sqlite3.IntegrityError if the URL
    already exists (URLs are UNIQUE) — discovery.py should catch that to
    silently skip duplicates rather than crash the whole run.
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO sources
                (source_id, url, domain, title, raw_text, status, discovered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.source_id,
                source.url,
                source.domain,
                source.title,
                source.raw_text,
                source.status,
                source.discovered_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def source_exists(url: str) -> bool:
    """Check whether a URL is already stored, so discovery.py can skip
    re-fetching/re-storing sources it has already found in a past run.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM sources WHERE url = ?", (url,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def list_sources(status: SourceStatus | None = None) -> list[Source]:
    """List stored sources, optionally filtered by status."""
    conn = get_connection()
    try:
        if status is None:
            rows = conn.execute("SELECT * FROM sources").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sources WHERE status = ?", (status,)
            ).fetchall()
        return [Source(**dict(row)) for row in rows]
    finally:
        conn.close()


def update_source_status(source_id: str, status: SourceStatus) -> None:
    """Move a source to a new pipeline status (e.g. after summarization
    sets it to 'pending_review', or a human review sets it to 'approved').
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE sources SET status = ? WHERE source_id = ?",
            (status, source_id),
        )
        conn.commit()
    finally:
        conn.close()
