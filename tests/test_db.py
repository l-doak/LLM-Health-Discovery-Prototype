"""
Tests for the SQLite storage layer in db.py.

Uses pytest's `tmp_path` fixture + `monkeypatch` to point `db.DB_PATH` at a
throwaway file for each test, so tests never touch (or depend on) the real
data/steadi.db used by the actual pipeline. Each test starts from a fresh,
empty database.
"""

import pytest

from src import db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Point db.DB_PATH at a temporary file for the duration of each test."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_steadi.db")
    db.init_db()


def make_source(url: str = "https://www.nhs.uk/conditions/falls/") -> db.Source:
    return db.Source(
        url=url,
        domain="nhs.uk",
        title="Falls - NHS",
        raw_text="Some example page text about fall prevention.",
    )


def test_insert_and_source_exists():
    source = make_source()
    assert db.source_exists(source.url) is False

    db.insert_source(source)

    assert db.source_exists(source.url) is True


def test_insert_duplicate_url_raises():
    source = make_source()
    db.insert_source(source)

    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        db.insert_source(make_source(url=source.url))


def test_list_sources_filters_by_status():
    approved = make_source(url="https://www.nhs.uk/a")
    approved.status = "approved"
    discovered = make_source(url="https://www.nhs.uk/b")

    db.insert_source(approved)
    db.insert_source(discovered)

    approved_only = db.list_sources(status="approved")
    assert len(approved_only) == 1
    assert approved_only[0].url == approved.url

    all_sources = db.list_sources()
    assert len(all_sources) == 2


def test_update_source_status():
    source = make_source()
    db.insert_source(source)

    db.update_source_status(source.source_id, "approved")

    [updated] = db.list_sources()
    assert updated.status == "approved"


def test_source_model_rejects_invalid_status():
    # Pydantic validates Literal fields at construction time, not on
    # mutation — so the invalid value must be passed to __init__ here
    # (model_copy(update=...) does NOT re-validate) to trigger the error.
    with pytest.raises(Exception):
        db.Source(
            url="https://www.nhs.uk/x",
            domain="nhs.uk",
            title="X",
            raw_text="text",
            status="not_a_real_status",
        )
