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
2. `Summary`      — a structured summary of a Source, produced by the
                    summarization stage. A Source can have more than one
                    Summary row over time (e.g. a failed attempt retried
                    with a newer prompt version) — each summarization run
                    inserts a new row rather than overwriting, so the
                    summaries table is a full history, not just current
                    state. `get_latest_summary` returns the one that
                    matters for the pipeline (the most recent attempt).
3. `ContentDraft`- a generated piece of app content (built in a later
                    stage, but modelled now so the schema is settled
                    early — see docs/schema.md).
4. `init_db()`   — creates all tables if they don't exist yet.
5. CRUD-ish helper functions for `Source` and `Summary`. Draft-related
   helpers will be added when we build draft.py, so this file doesn't get
   ahead of itself with untested code for a stage we haven't built yet.
"""

from __future__ import annotations

import json
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


class Summary(BaseModel):
    """A structured summary of a Source, produced by the summarization
    stage (src/summarize.py).

    Deliberately a separate table from `sources` rather than extra
    columns on it, for two reasons: (1) it keeps `sources` a stage-1-only
    concern, mirroring how `content_drafts` is already its own table
    rather than being bolted onto `sources`; and (2) it lets a source be
    re-summarized (a failed attempt retried, or a prompt update re-run)
    without destroying the previous attempt — every summarization run is
    a new row, not an overwrite. `target_audience` is free text for now,
    same "not yet a controlled vocabulary" caveat as `ContentDraft.tags`
    — see docs/limitations.md.

    There's deliberately no `status` field here: the human review
    decision (stage 3) is recorded on `Source.status`
    (`approved`/`rejected`), not on the Summary itself.
    """

    summary_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_id: str
    title: str
    key_claims: list[str]
    target_audience: str
    credibility_notes: str
    model_version: str
    prompt_version: str
    generated_at: str = Field(
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
    """Create all tables if they don't exist yet.

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
            CREATE TABLE IF NOT EXISTS summaries (
                summary_id         TEXT PRIMARY KEY,
                source_id          TEXT NOT NULL,
                title              TEXT NOT NULL,
                key_claims         TEXT NOT NULL,  -- JSON-encoded list
                target_audience    TEXT NOT NULL,
                credibility_notes  TEXT NOT NULL,
                model_version      TEXT NOT NULL,
                prompt_version     TEXT NOT NULL,
                generated_at       TEXT NOT NULL,
                FOREIGN KEY (source_id) REFERENCES sources (source_id)
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


def delete_source(source_id: str) -> None:
    """Delete a source and any summaries associated with it.

    This is NOT part of normal pipeline flow — the pipeline itself only
    ever changes a source's status, never deletes rows, so the audit
    trail stays intact for anything that actually went through the
    pipeline. This exists for removing genuinely bad data instead: e.g. a
    source whose raw_text turned out to be a mis-fetched PDF rather than
    real page content (see discovery.py's NotHTMLContentError, added
    after hitting exactly this). SQLite's foreign key from summaries to
    sources isn't set up with ON DELETE CASCADE, so summaries are deleted
    explicitly first to avoid leaving orphaned rows.
    """
    conn = get_connection()
    try:
        conn.execute("DELETE FROM summaries WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))
        conn.commit()
    finally:
        conn.close()


# --- Summary CRUD helpers --------------------------------------------------


def insert_summary(summary: Summary) -> None:
    """Insert a new summary row. Does NOT touch Source.status — callers
    (summarize.py) are responsible for calling update_source_status
    separately, so a summary can be stored and inspected even if the
    status transition step were ever to fail independently.
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO summaries
                (summary_id, source_id, title, key_claims, target_audience,
                 credibility_notes, model_version, prompt_version, generated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                summary.summary_id,
                summary.source_id,
                summary.title,
                json.dumps(summary.key_claims),
                summary.target_audience,
                summary.credibility_notes,
                summary.model_version,
                summary.prompt_version,
                summary.generated_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_summary(source_id: str) -> Summary | None:
    """Return the most recent summary for a source (by generated_at), or
    None if it hasn't been successfully summarized yet. This is what the
    human review stage should read — not list_summaries — since only the
    latest attempt is the one awaiting a decision.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT * FROM summaries
            WHERE source_id = ?
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            (source_id,),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["key_claims"] = json.loads(data["key_claims"])
        return Summary(**data)
    finally:
        conn.close()


def list_summaries(source_id: str | None = None) -> list[Summary]:
    """List all summary attempts, optionally filtered to one source.
    Mostly useful for debugging and audit review (e.g. "show me every
    attempt at summarizing this source"), not the main pipeline path.
    """
    conn = get_connection()
    try:
        if source_id is None:
            rows = conn.execute("SELECT * FROM summaries").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM summaries WHERE source_id = ?", (source_id,)
            ).fetchall()
        results = []
        for row in rows:
            data = dict(row)
            data["key_claims"] = json.loads(data["key_claims"])
            results.append(Summary(**data))
        return results
    finally:
        conn.close()
