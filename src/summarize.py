"""
summarize.py
============

Stage 2 of the pipeline: structured summarization.

Reads every Source with status="discovered", asks Claude to produce a
structured summary (title, key claims, target audience, credibility
notes), stores the result via db.insert_summary, and flips the source's
status to "pending_review" so stage 3 (human review) can pick it up.

WHY FORCED TOOL-USE FOR STRUCTURED OUTPUT
------------------------------------------
Rather than asking the model to "reply in JSON" and parsing its prose
response (fragile — models sometimes wrap JSON in markdown fences, add a
preamble, etc.), we define a tool schema that matches the Summary model
and force the model to call it via `tool_choice`. The API then guarantees
a well-formed arguments object matching our schema, which we still pass
through the Summary Pydantic model as a final validation safety net.

WHY PER-SOURCE ERROR HANDLING
-------------------------------
One bad source (blocked page, malformed API response, a transient rate
limit) shouldn't take down a whole batch run. Each source is summarized
independently; failures are logged via audit.log_event and the source is
left at status="discovered" so it's retried on a future run.

RETRIES VS THE AGENT-ENGINEERING ROADMAP
------------------------------------------
This module retries a failed API call a small, fixed number of times with
a short backoff — but only for transient failures (rate limits, connection
errors). It does NOT retry because the *content* of a summary looks bad
(e.g. too few claims, a claim that doesn't look grounded) — that's a
smarter, evaluation-driven retry loop, which is explicitly future scope
(see agent-engineering-roadmap.md) once stage 5's grounding check exists
to judge quality in the first place.
"""

from __future__ import annotations

import time
from typing import Any

import anthropic
from dotenv import load_dotenv

from . import audit, db

load_dotenv()

# Which Claude model to call. Kept as a single constant so it's easy to
# change in one place; check docs.claude.com for currently available
# model names before relying on this string.
MODEL = "claude-sonnet-4-5"

# Bump this by hand whenever SUMMARY_PROMPT_TEMPLATE's wording changes in
# a way that could meaningfully change model output. The audit log records
# this label (not the full prompt text) against every summarization event.
# Simplification: this means reconstructing the *exact* prompt used for a
# past event means checking out the matching commit rather than reading
# the audit log alone — a production system would more likely snapshot
# the literal prompt text per call. See docs/limitations.md.
PROMPT_VERSION = "summarize_v1"

# How many times to retry a single source on a transient API error
# (rate limit / connection issue) before giving up on it for this run.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

# A source's raw_text shorter than this is treated as unusable rather than
# sent to the API at all — cheap to check locally, saves an API call on
# pages that fetched empty/near-empty (e.g. JS-rendered content BS4 can't see).
MIN_RAW_TEXT_CHARS = 50

SUMMARY_PROMPT_TEMPLATE = """You are helping a clinical content team triage \
health-information sources for older adults. Read the following page text \
and produce a structured summary.

Be faithful to the source: every claim you list must be something the \
source text actually says, not general medical knowledge. If the page \
doesn't clearly state a target audience, infer the most reasonable one \
from context (e.g. "older adults at risk of falls") rather than leaving \
it vague.

Page title: {title}
Page URL: {url}

Page text:
{raw_text}
"""

# Tool schema Claude must fill in. Field descriptions double as the only
# "prompting" those fields get, so they're written deliberately.
RECORD_SUMMARY_TOOL = {
    "name": "record_summary",
    "description": "Record a structured summary of the health-information page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": (
                    "A clear, human-readable title for this source (may "
                    "match the page title, or be tightened if the page "
                    "title is vague)."
                ),
            },
            "key_claims": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "3-6 concise factual claims the source text actually "
                    "makes, in plain language. Each must be traceable to "
                    "specific wording in the source text."
                ),
            },
            "target_audience": {
                "type": "string",
                "description": (
                    "Who this content is written for or most relevant to, "
                    "e.g. 'older adults living independently' or "
                    "'caregivers of people with dementia'."
                ),
            },
            "credibility_notes": {
                "type": "string",
                "description": (
                    "A short note on why this source is (or isn't) "
                    "credible, e.g. publisher, evidence of clinical "
                    "review, date of last update if stated."
                ),
            },
        },
        "required": ["title", "key_claims", "target_audience", "credibility_notes"],
    },
}


def _call_claude_for_summary(
    client: anthropic.Anthropic, source: db.Source
) -> tuple[dict[str, Any], str]:
    """Call Claude once, forced to use the record_summary tool.

    Returns (tool_input, model_version). Raises anthropic's own exception
    types on API/transport errors, or ValueError if the model somehow
    responds without calling the tool (shouldn't happen with tool_choice
    set, but we check rather than assume).
    """
    prompt = SUMMARY_PROMPT_TEMPLATE.format(
        title=source.title, url=source.url, raw_text=source.raw_text
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        tools=[RECORD_SUMMARY_TOOL],
        tool_choice={"type": "tool", "name": "record_summary"},
        messages=[{"role": "user", "content": prompt}],
    )

    tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
    if not tool_use_blocks:
        raise ValueError(
            f"Model did not call record_summary for source {source.source_id}"
        )

    return tool_use_blocks[0].input, response.model


def summarize_source(client: anthropic.Anthropic, source: db.Source) -> bool:
    """Summarize a single source: call Claude, store the result, flip
    status, log the event.

    Returns True on success, False on failure. Failures are logged, not
    raised, so the caller (run_summarization) can continue the batch.
    """
    if not source.raw_text or len(source.raw_text.strip()) < MIN_RAW_TEXT_CHARS:
        audit.log_event(
            stage="summarize",
            inputs={"source_id": source.source_id, "url": source.url},
            output={"error": "raw_text missing or too short to summarize"},
            decision="failed",
            model=None,
            prompt_version=PROMPT_VERSION,
        )
        print(f"Skipping {source.url}: raw_text missing or too short")
        return False

    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            tool_input, model_version = _call_claude_for_summary(client, source)

            summary = db.Summary(
                source_id=source.source_id,
                title=tool_input["title"],
                key_claims=tool_input["key_claims"],
                target_audience=tool_input["target_audience"],
                credibility_notes=tool_input["credibility_notes"],
                model_version=model_version,
                prompt_version=PROMPT_VERSION,
            )
            db.insert_summary(summary)
            db.update_source_status(source.source_id, "pending_review")

            audit.log_event(
                stage="summarize",
                inputs={"source_id": source.source_id, "url": source.url},
                output=summary.model_dump(),
                decision="stored",
                model=model_version,
                prompt_version=PROMPT_VERSION,
            )
            return True

        except (anthropic.RateLimitError, anthropic.APIConnectionError) as e:
            # Transient — worth retrying with backoff.
            last_error = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            break

        except Exception as e:
            # Malformed tool output, Pydantic validation error, or any
            # other non-transient failure — retrying won't help.
            last_error = e
            break

    audit.log_event(
        stage="summarize",
        inputs={"source_id": source.source_id, "url": source.url},
        output={"error": str(last_error)},
        decision="failed",
        model=None,
        prompt_version=PROMPT_VERSION,
    )
    print(f"Failed to summarize {source.url}: {last_error}")
    return False


def run_summarization(client: anthropic.Anthropic | None = None) -> dict[str, int]:
    """Summarize every source currently at status="discovered".

    Returns a small {"succeeded": n, "failed": n} count, mainly so a CLI
    entry point or test can report/assert on the outcome.
    """
    client = client or anthropic.Anthropic()
    sources = db.list_sources(status="discovered")

    succeeded = 0
    failed = 0
    for source in sources:
        if summarize_source(client, source):
            succeeded += 1
        else:
            failed += 1

    return {"succeeded": succeeded, "failed": failed}


if __name__ == "__main__":
    db.init_db()
    results = run_summarization()
    print(f"Summarization run complete: {results}")
