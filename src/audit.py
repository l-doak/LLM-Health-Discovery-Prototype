"""
audit.py
========

Shared logging helper used by every stage of the pipeline (discovery,
summarization, review, draft generation, evaluation).

WHY THIS EXISTS
----------------
The job spec (and the project brief) explicitly calls out "auditability
across the pipeline" as a requirement. That means: for every decision the
system makes, we should be able to answer "what happened, using what
inputs, which model/prompt version, and what was the outcome?" after the
fact — not just print something to the console and lose it.

DESIGN DECISION: plain JSON Lines (.jsonl) file, not a logging framework.
- Each log entry is one JSON object on its own line, appended to a file.
- This is deliberately low-tech: no structlog, no external log service.
  It's easy to read (each line is valid JSON), easy to grep, and easy to
  load into pandas/SQLite later for analysis ("show me every stage-2
  event where the model flagged low confidence").
- A production system would likely send these to a proper log pipeline
  (e.g. CloudWatch, a message queue) rather than a local file — that's a
  simplification worth noting in docs/limitations.md.

Every other module should import `log_event` from here rather than using
`print()` or the standard `logging` module directly, so that all audit
history ends up in one consistent place and format.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Where audit events get written. Kept as a module-level constant so every
# caller writes to the same file without having to pass a path around.
# The `data/` directory already exists (it holds allowlist.json), so we
# keep the audit log alongside it rather than inventing a new top-level
# folder.
AUDIT_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "audit_log.jsonl"


def log_event(
    stage: str,
    inputs: dict[str, Any],
    output: dict[str, Any] | None = None,
    decision: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
) -> dict[str, Any]:
    """Append one structured audit event to the audit log.

    Args:
        stage: Which pipeline stage produced this event, e.g. "discovery",
            "summarize", "review", "draft", "evaluate". Use the same short
            names consistently so the log can be filtered by stage later.
        inputs: The inputs that led to this event (e.g. the search query,
            the source URL, the source text that was summarized). Keep
            this small — store references (URLs, IDs) rather than huge
            blobs of text where possible, so the log file stays readable.
        output: Whatever the stage produced (a summary, a draft, a
            validation result). Optional because some events are pure
            decisions with no generated output (e.g. a human rejecting a
            source).
        decision: A short human- or system-made decision label, e.g.
            "approved", "rejected", "flagged_low_confidence", "stored".
        model: The model identifier used, if an LLM call was involved
            (e.g. "claude-sonnet-4-5"). None for non-LLM stages like raw
            discovery/fetching.
        prompt_version: A label for which version of the prompt template
            was used (e.g. "summarize_v1"). This matters because if you
            change a prompt later, you want to be able to tell which past
            outputs were generated under the old vs new version.

    Returns:
        The full event dict that was written, in case the caller wants to
        also use it (e.g. to get the generated timestamp).
    """
    event: dict[str, Any] = {
        # UTC, ISO 8601 — unambiguous regardless of where this runs.
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "inputs": inputs,
        "output": output,
        "decision": decision,
        "model": model,
        "prompt_version": prompt_version,
    }

    # Make sure the parent directory exists (it will, in practice, since
    # data/allowlist.json is committed to the repo — but this keeps the
    # function safe to call even in a fresh checkout before that's true).
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    # 'a' = append. We never rewrite past history — the audit log is
    # meant to be an immutable record of what happened, in order.
    with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    return event


def read_events(stage: str | None = None) -> list[dict[str, Any]]:
    """Read back audit events, optionally filtered to a single stage.

    This is a convenience for debugging and for the eventual evaluation
    stage (e.g. "show me every draft-generation event so I can spot-check
    grounding"). Not optimised for large logs — for a prototype, reading
    the whole file into memory each time is fine.
    """
    if not AUDIT_LOG_PATH.exists():
        return []

    events = []
    with AUDIT_LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if stage is None or event.get("stage") == stage:
                events.append(event)
    return events
