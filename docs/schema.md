# Data schema

This documents the two data models used by the pipeline. The source of
truth is the Pydantic models in `src/db.py` — this file explains *why*
each field exists in prose. If you change a field, update both this file
and `src/db.py` in the same commit (see the project instructions).

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

`tags` is currently untyped (`list[str]`) rather than a `Literal` union
like `category`, because we haven't defined the controlled tag vocabulary
yet. Before building `draft.py`, decide on a fixed tag list (e.g.
`["balance", "strength", "medication-review", "home-hazards", ...]`) and
tighten this field the same way `category` is tightened. Leaving it loose
for now is a deliberate, temporary simplification — not an oversight.
