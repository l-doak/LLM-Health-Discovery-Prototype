# STEADI prototype — health content discovery & drafting pipeline

[![tests](https://github.com/l-doak/LLM-Health-Discovery-Prototype/actions/workflows/tests.yml/badge.svg)](https://github.com/l-doak/LLM-Health-Discovery-Prototype/actions/workflows/tests.yml)

A small prototype of a pipeline that discovers approved health-information
sources, summarizes them for human review, turns approved sources into
structured app-content drafts, evaluates quality, and logs every step for
auditability. Built as a portfolio piece to explore the problem space of an
LLM-assisted content pipeline; it is not production software, but is built
and documented as if it could become the basis of one.

*Independent portfolio prototype — not affiliated with or endorsed by
Smplicare.*

See `docs/design-decisions.md` for why each choice was made and
`docs/limitations.md` for the honest gaps versus a production system.

## Pipeline and status

```
[topic] → 1. Discovery → 2. Summarization → 3. Human review
        → 4. Draft generation → 5. Evaluation      (6. Audit log spans all stages)
```

| # | Stage | Module | Status |
|---|---|---|---|
| 1 | Discovery: Tavily search restricted to an allowlist, page fetch, de-duplication | `src/discovery.py` | Built, tested |
| 2 | Structured summarization: Claude forced tool-use, retries, input cap | `src/summarize.py` | Built, tested |
| 3 | Human review: approve / reject (CLI first) | `src/review.py` | **Next** |
| 4 | Draft generation + metadata/tags against a fixed schema | `src/draft.py` | Not started |
| 5 | Evaluation: grounding check, readability, schema validation | `src/evaluate.py` | Not started |
| 6 | Audit log: structured JSON events | `src/audit.py` | Built; wired into stages 1–2 |

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# then edit .env and add your real ANTHROPIC_API_KEY and TAVILY_API_KEY
```

Note: `requirements.txt` already lists packages for later stages
(`sentence-transformers`, `chromadb`, `flask`, `textstat`). Nothing in
stages 1–2 uses them yet.

## Running the pipeline

**Stage 1 — discovery**

```bash
python -m src.discovery "fall prevention exercises for older adults"
```

1. Searches Tavily for the topic, restricted to the domains in
   `data/allowlist.json`.
2. Re-checks each result against the allowlist (safety net) and
   normalizes/de-duplicates URLs.
3. Fetches and cleans each page's text. Non-HTML responses (e.g. PDFs)
   are skipped and logged rather than mis-parsed.
4. Stores new sources in `data/steadi.db` (SQLite) with status
   `discovered`.
5. Logs every decision to `data/audit_log.jsonl`.

Run it again with the same topic and already-stored URLs are skipped
(`skipped_already_stored` in the audit log) instead of re-fetched.

**Stage 2 — summarization**

```bash
python -m src.summarize
```

Summarizes every source with status `discovered` using Claude, then moves
it to `pending_review`. Failures are logged and the source stays
`discovered`, so re-running retries it. Every attempt is stored as its own
row in the `summaries` table.

## Running tests

```bash
pytest
```

No network calls or API keys are needed: the Claude API is mocked, and
each test uses a throwaway database and a throwaway audit log, so running
the tests never touches `data/steadi.db` or `data/audit_log.jsonl`.

## Project layout

```
.
├── README.md
├── requirements.txt
├── .env.example              # copy to .env with real API keys
├── .github/workflows/
│   └── tests.yml             # runs pytest on every push and pull request
├── docs/
│   ├── design-decisions.md   # why each choice was made
│   ├── limitations.md        # gaps vs. a production system
│   └── schema.md             # data model reference (Source, Summary, ContentDraft)
├── src/
│   ├── audit.py              # shared structured-logging helper
│   ├── db.py                 # Pydantic models + SQLite schema/helpers
│   ├── discovery.py          # stage 1: search + filter + fetch + store
│   └── summarize.py          # stage 2: structured summaries via Claude
├── data/
│   ├── allowlist.json        # approved source domains
│   ├── steadi.db             # created on first run (gitignored)
│   └── audit_log.jsonl       # created on first run (gitignored)
└── tests/
    ├── conftest.py           # isolates the audit log from the test suite
    ├── test_db.py
    ├── test_discovery.py
    ├── test_discovery_content_type.py
    └── test_summarize.py
```

## Next steps

1. **Stage 3 — human review** (`src/review.py`, CLI first): list sources at
   `pending_review` with their latest summary, record approve/reject, log
   each decision. Adds `reviewed_by` / `reviewed_at` to `Source`.
2. **Decide the controlled vocabularies** for `tags` and
   `target_audience` (see the open question in `docs/schema.md`).
3. **Stage 4 — draft generation**, then **stage 5 — evaluation** (grounding
   check first). The grounding check is also what will later enable
   evaluation-driven retries, turning the pipeline into a planning loop.
