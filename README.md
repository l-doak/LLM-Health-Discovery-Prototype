# prototype — health content discovery & drafting pipeline

A small prototype of a pipeline that discovers approved health-information
sources, summarizes them for human review, turns approved sources into
structured app-content drafts, evaluates quality, and logs every step for
auditability. Built as a portfolio piece — see `docs/design-decisions.md`
and `docs/limitations.md` for the reasoning and honest gaps.

## Status

**Stage 1 (discovery) is built and working.** Stages 2-6 (summarization,
review, draft generation, evaluation, and full audit coverage of those
stages) are not built yet — see `docs/limitations.md`.

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# then edit .env and add your real ANTHROPIC_API_KEY and TAVILY_API_KEY
```

## Running discovery

```bash
python -m src.discovery "fall prevention exercises for older adults"
```

This will:

1. Search Tavily for the given topic.
2. Keep only results whose domain is in `data/allowlist.json`.
3. Fetch and clean each kept page's text.
4. Store new sources in `data/steadi.db` (SQLite).
5. Log every decision (kept, filtered out, fetch failed, stored) to
   `data/audit_log.jsonl`.

Run it again with the same topic and you'll see already-stored URLs get
skipped (`skipped_already_stored` in the audit log) rather than
re-fetched.

## Running tests

```bash
pytest
```

`tests/test_discovery.py` covers the pure URL/allowlist logic (no network
calls, no API key needed). `tests/test_db.py` covers the storage layer
against a temporary throwaway database.

## Project layout

```
steadi-prototype/
├── README.md
├── requirements.txt
├── .env.example              # copy to .env with real API keys
├── docs/
│   ├── design-decisions.md   # why each non-obvious choice was made
│   ├── limitations.md        # honest gaps vs. a production system
│   └── schema.md             # data model reference
├── src/
│   ├── audit.py              # shared structured-logging helper
│   ├── db.py                 # Pydantic models + SQLite schema/helpers
│   └── discovery.py          # stage 1: search + filter + fetch + store
├── data/
│   ├── allowlist.json        # approved source domains
│   ├── steadi.db             # created on first run (gitignored)
│   └── audit_log.jsonl       # created on first run (gitignored)
└── tests/
    ├── test_discovery.py
    └── test_db.py
```

## Next steps

See `steadi-portfolio-plan.md` (project knowledge) for the full roadmap.
Immediately next: stage 2, structured summarization of stored sources
using the Claude API, moving each source's status from `discovered` to
`pending_review`.
