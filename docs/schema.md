# Data schema

This documents the three data models used by the pipeline. The source of
truth is the Pydantic models in `src/db.py` — this file explains *why*
each field exists in prose. If you change a field, update both this file
and `src/db.py` in the same commit (see the project instructions).

Pipeline flow across the models: a `Source` is discovered → gets one or
more `Summary` rows → a human approves or rejects the `Source` → an
approved `Source` yields a `ContentDraft`.

## `Source` (stage 1: discovery)

A candidate health-information page found by the discovery stage, before
any summarization or human review has happened to it.

| Field | Type | Why it's here |
|---|---|---|
| `source_id` | UUID string | Stable identifier that doesn't change even if the URL is later normalized/edited. |
| `url` | string | The page's URL. Also the natural de-duplication key — see `db.source_exists`. |
| `domain` | string | Extracted separately from `url` so we can query/report "how many sources per domain" without re-parsing URLs later. |
| `title` | string | From the search result; used for human-readable review UIs and logs. |
| `raw_text` | string | The fetched, HTML-stripped page text. This is the input to summarization — kept verbatim (not summarized) so later stages can re-derive grounding checks against the original wording. |
| `status` | enum | Tracks pipeline position: `discovered` → `pending_review` (summary generated) → `approved` / `rejected` (human decision). |
| `discovered_at` | ISO 8601 timestamp | When discovery found it — separate from the audit log so you can query "sources found this week" directly from the DB without scanning JSONL. |

**Deferred:** `reviewed_by` / `reviewed_at` are not on `Source` yet. They
are needed as soon as stage 3 (`review.py`) records an approve/reject
decision, and will be added then, together with the matching `db.py`
change and a migration for existing databases.

## `Summary` (stage 2: structured summarization)

A structured, LLM-produced summary of one `Source`, stored for human
review. Lives in its own `summaries` table rather than as extra columns on
`sources`: this keeps `sources` a stage-1-only concern, and lets a source
be re-summarized (a failed attempt retried, a prompt version bumped)
**without overwriting the previous attempt** — each run inserts a new row,
so the table is a full history. `db.get_latest_summary` returns the most
recent row, which is the one stage 3 should show a reviewer;
`db.list_summaries` returns the full history for audit/debugging.

| Field | Type | Why it's here |
|---|---|---|
| `summary_id` | UUID string | Stable identifier for this one summarization attempt. |
| `source_id` | UUID string | Foreign key to `Source.source_id`. Many summaries may point at one source. |
| `title` | string | A clear human-readable title; may tighten a vague page title. |
| `key_claims` | list of strings | 3–6 factual claims the model says the source makes. Stored JSON-encoded in SQLite, decoded back to a list by `db.py`. This is what the stage-5 grounding check will test against `Source.raw_text`. |
| `target_audience` | string (free text) | Who the content is written for. **Not yet a controlled vocabulary** — same open item as `ContentDraft.tags`; see the open question below. |
| `credibility_notes` | string | Short note on why the source is or isn't credible (publisher, evidence of clinical review, last-updated date if stated). |
| `model_version` | string | Taken from the API response (`response.model`), not the constant we asked for, so the audit record reflects what actually ran. |
| `prompt_version` | string | Label of the prompt template used (e.g. `summarize_v1`). A label, not a snapshot — see `docs/limitations.md`. |
| `generated_at` | ISO 8601 timestamp | When this summary was produced; `get_latest_summary` orders by it. |

There is deliberately **no `status` field** on `Summary`: the human
decision is recorded on `Source.status` (`approved` / `rejected`), so there
is one place that says where a source stands.

Truncation note: if a page's `raw_text` exceeds `MAX_RAW_TEXT_CHARS`, only
the first part is sent to the model. The stored `Source.raw_text` is never
truncated, and the audit event for that summary records `truncated: true`.

## `ContentDraft` (stage 4: draft generation)

Modelled now, built later. This is the schema the job description's
"metadata/tags via an agreed schema" requirement points at directly.

| Field | Type | Why it's here |
|---|---|---|
| `content_id` | UUID string | Stable identifier for the generated content item. |
| `title` | string | Patient-facing title. |
| `body` | string | The drafted content itself, in patient-friendly language. |
| `source_url` | string | Foreign key back to `Source.url` — every draft must be traceable to exactly one source page. |
| `source_credibility_notes` | string | Short human/LLM-written note on why this source is trustworthy (e.g. "NHS official guidance page, last reviewed 2024"). |
| `category` | enum: `mobility \| medication \| home-safety \| nutrition \| general-frailty` | A **controlled vocabulary**, not free text — this is the "schema discipline" the project brief calls for. Enforced by Pydantic's `Literal` type, so an invalid category raises a validation error before it ever reaches the database. |
| `tags` | list of strings | Also intended to be a controlled vocabulary. **Not yet finalized** — the current model accepts any strings; before draft.py is built we should define the actual allowed tag list (see open question below). |
| `reading_level` | string | e.g. a Flesch-Kincaid grade level, computed at evaluation time. |
| `reviewed_by` | string or null | Who (or what process) approved this draft; null until reviewed. |
| `status` | enum: `pending_review \| approved \| rejected \| published` | Mirrors `Source.status` but for the content item itself — a source being `approved` and a draft generated from it being `approved` are two separate human decisions. |
| `generated_at` | ISO 8601 timestamp | When the draft was generated. |
| `model_version` | string | Which Claude model produced this draft — needed for audit/reproducibility. |
| `prompt_version` | string | Which prompt template version produced this draft — same reason. |

### Open question (flagged for docs/limitations.md)

`tags` (on `ContentDraft`) and `target_audience` (on `Summary`) are both
free text at the moment. `category` is already locked down with a
`Literal`; these two are not, because the controlled vocabularies haven't
been defined yet. Before building `draft.py`, decide a fixed list for each
(e.g. tags such as `["balance", "strength", "medication-review",
"home-hazards", ...]`) and tighten the fields the same way. The two lists
likely overlap, so decide them together — ideally by looking at real
summaries produced in stage 2. Leaving them loose for now is a
deliberate, temporary simplification — not an oversight.
