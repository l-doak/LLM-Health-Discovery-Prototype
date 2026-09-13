"""
tests/test_summarize.py

Unit tests for the stage-2 summarization module. The Anthropic API call is
mocked throughout — these tests check OUR logic (parsing tool output into
a Summary, status transitions, retry/error handling), not Claude's actual
output quality. Judging output quality is what the eventual grounding-check
evaluation stage (stage 5) is for.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import anthropic
import pytest

from src import db, summarize


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point db.DB_PATH at a throwaway file for each test, so tests never
    touch the real data/steadi.db.
    """
    db_path = tmp_path / "test_steadi.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    return db_path


@pytest.fixture
def sample_source(temp_db) -> db.Source:
    source = db.Source(
        url="https://www.nhs.uk/example-page",
        domain="nhs.uk",
        title="Example NHS page",
        raw_text="This is enough example text to pass the length check. " * 3,
        status="discovered",
    )
    db.insert_source(source)
    return source


def _make_fake_response(tool_input: dict, model: str = "claude-sonnet-4-5-mock"):
    """Build a fake anthropic Message response with a single tool_use
    block, shaped like the real response.content list.
    """
    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.input = tool_input

    response = MagicMock()
    response.content = [tool_use_block]
    response.model = model
    return response


VALID_TOOL_INPUT = {
    "title": "Example NHS page",
    "key_claims": ["Claim one.", "Claim two."],
    "target_audience": "older adults",
    "credibility_notes": "Official NHS guidance page.",
}


def test_summarize_source_success(sample_source):
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_fake_response(VALID_TOOL_INPUT)

    result = summarize.summarize_source(fake_client, sample_source)

    assert result is True

    pending = db.list_sources(status="pending_review")
    assert len(pending) == 1
    assert pending[0].source_id == sample_source.source_id
    assert db.list_sources(status="discovered") == []

    stored = db.get_latest_summary(sample_source.source_id)
    assert stored is not None
    assert stored.key_claims == ["Claim one.", "Claim two."]
    assert stored.prompt_version == summarize.PROMPT_VERSION
    assert stored.model_version == "claude-sonnet-4-5-mock"

    fake_client.messages.create.assert_called_once()
    _, kwargs = fake_client.messages.create.call_args
    assert kwargs["tool_choice"] == {"type": "tool", "name": "record_summary"}


def test_summarize_source_skips_empty_raw_text(temp_db):
    empty_source = db.Source(
        url="https://www.nhs.uk/empty-page",
        domain="nhs.uk",
        title="Empty page",
        raw_text="",
        status="discovered",
    )
    db.insert_source(empty_source)

    fake_client = MagicMock()
    result = summarize.summarize_source(fake_client, empty_source)

    assert result is False
    fake_client.messages.create.assert_not_called()
    # status untouched, so it can be retried once raw_text is fixed
    remaining = db.list_sources(status="discovered")
    assert len(remaining) == 1
    assert remaining[0].source_id == empty_source.source_id
    assert db.get_latest_summary(empty_source.source_id) is None


def test_summarize_source_handles_missing_tool_call(sample_source):
    fake_client = MagicMock()
    response = MagicMock()
    response.content = []  # model didn't call the tool
    response.model = "claude-sonnet-4-5-mock"
    fake_client.messages.create.return_value = response

    result = summarize.summarize_source(fake_client, sample_source)

    assert result is False
    assert db.list_sources(status="discovered")[0].source_id == sample_source.source_id
    assert db.get_latest_summary(sample_source.source_id) is None


def test_summarize_source_retries_on_rate_limit_then_succeeds(sample_source, monkeypatch):
    monkeypatch.setattr(summarize.time, "sleep", lambda _: None)  # skip real waiting

    fake_response = MagicMock()
    fake_response.status_code = 429
    fake_response.request = MagicMock()
    rate_limit_error = anthropic.RateLimitError(
        "rate limited", response=fake_response, body=None
    )

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        rate_limit_error,
        _make_fake_response(VALID_TOOL_INPUT),
    ]

    result = summarize.summarize_source(fake_client, sample_source)

    assert result is True
    assert fake_client.messages.create.call_count == 2
    assert db.get_latest_summary(sample_source.source_id) is not None


def test_summarize_source_gives_up_after_max_retries(sample_source, monkeypatch):
    monkeypatch.setattr(summarize.time, "sleep", lambda _: None)

    fake_response = MagicMock()
    fake_response.status_code = 429
    fake_response.request = MagicMock()
    rate_limit_error = anthropic.RateLimitError(
        "rate limited", response=fake_response, body=None
    )

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = rate_limit_error

    result = summarize.summarize_source(fake_client, sample_source)

    assert result is False
    assert fake_client.messages.create.call_count == summarize.MAX_RETRIES
    assert db.list_sources(status="discovered")[0].source_id == sample_source.source_id
    assert db.get_latest_summary(sample_source.source_id) is None


def test_run_summarization_counts_successes_and_failures(temp_db):
    good_source = db.Source(
        url="https://www.nhs.uk/good-page",
        domain="nhs.uk",
        title="Good page",
        raw_text="Plenty of real content here to summarize. " * 3,
        status="discovered",
    )
    bad_source = db.Source(
        url="https://www.nhs.uk/bad-page",
        domain="nhs.uk",
        title="Bad page",
        raw_text="",
        status="discovered",
    )
    db.insert_source(good_source)
    db.insert_source(bad_source)

    fake_client = MagicMock()
    fake_client.messages.create.return_value = _make_fake_response(
        {**VALID_TOOL_INPUT, "title": "Good page"}
    )

    results = summarize.run_summarization(client=fake_client)

    assert results == {"succeeded": 1, "failed": 1}
