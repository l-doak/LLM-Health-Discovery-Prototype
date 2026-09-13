# Design decisions

A running log of non-obvious choices and why they were made. Added to as
the project grows — treat this as a diary, not a spec.

## Storage: SQLite, not Postgres

Zero infrastructure to set up, but still gives real relational-DB
experience (schema, queries, foreign keys). A production version of this
system handling concurrent writes from multiple users would want
Postgres; for a single-user prototype that's unneeded complexity.

## Search: Tavily, not Bing Search API

Tavily's free tier needs only an API key (no Azure account/subscription
setup), and it's purpose-built for this kind of "search for information
to feed an LLM" use case rather than being a general web search API
retrofitted for it.

## Audit logging: plain JSON Lines file, not a logging framework

Each audit event is one JSON object per line, appended to
`data/audit_log.jsonl`. This is easy to read, grep, and later load into
pandas/SQLite for analysis, without pulling in a dependency like
`structlog` or standing up a real log pipeline. A production system would
likely ship these events to a proper log/observability service — see
`docs/limitations.md`.

## Domain filtering happens before fetching, not after

Discovery checks a candidate URL's domain against `data/allowlist.json`
*before* making any HTTP request to fetch its content. This avoids
wasted network calls to sources we're going to discard anyway, and means
disallowed URLs are never even downloaded.

## Page text extraction: BeautifulSoup, not a headless browser

Most target health-org pages are static server-rendered HTML, so a
lightweight `requests` + BeautifulSoup fetch is enough, and is far faster
and simpler than driving a full browser (e.g. Playwright) per page. The
known trade-off: any page that renders its main content via JavaScript
will yield poor/empty extracted text with this approach.

## Pydantic models are the schema source of truth

`src/db.py` defines `Source` and `ContentDraft` as Pydantic models, and
the SQLite `CREATE TABLE` statements are written to mirror them directly
in the same file. `docs/schema.md` explains the *why* for each field in
prose. Keeping model, table, and doc together (updated in the same
commit whenever one changes) is meant to prevent them drifting apart, per
the project's schema-discipline requirement.

## Controlled vocabularies via Pydantic `Literal`, not free text

`ContentDraft.category` is a fixed set of five values enforced by
Python's type system, not a free-text string — an invalid category
raises a validation error immediately rather than silently getting
written to the database. `tags` is not yet locked down this way (see the
open question in `docs/schema.md`) — that's the next schema decision to
make, before `draft.py` is built.



## Session 1: getting discovery actually working end-to-end

Built the full skeleton (audit.py, db.py, discovery.py, docs, tests) and
got stage 1 (discovery) running for real against Tavily. Three real bugs
came up when testing against live data, worth recording because each one
reflects something a real integration would also hit:

**Bug: non-ASCII character in an HTTP header.** The `User-Agent` string
originally contained an em dash (`—`). HTTP headers are restricted to
Latin-1/ASCII, so `requests` raised `UnicodeEncodeError` the first time a
real fetch was attempted. Fixed by using a plain hyphen. Lesson: this
class of bug is invisible until a real network call exercises the code
path — the unit tests didn't catch it because they don't fetch real
pages.

**Design change: restrict the Tavily search itself, don't just filter
afterward.** The initial approach asked Tavily for its top general
results and then discarded almost all of them against
`data/allowlist.json` — for a real query ("fall prevention exercises for
older adults") this returned **zero** stored sources, because none of
Tavily's top general-web picks (commercial senior-care blogs, YouTube,
etc.) happened to be on the allowlist. Fixed by passing the allowlist to
Tavily's `include_domains` parameter, so the search is restricted to
trusted domains from the start. The original post-fetch `is_allowed()`
check was kept in place as a safety net, not removed — defense in depth
in case Tavily's own domain matching is ever looser than expected.

**Bug: near-duplicate sources from URL variants.** Once real results
came back, the same article was often returned multiple times under
cosmetically different URLs (`http` vs `https`, with/without `www.`, a
`#fragment` appended, a trailing slash). Since de-duplication checked for
an *exact* URL match, each variant was stored as a separate row — wasted
storage now, and would have meant redundant LLM calls once summarization
is built. Fixed with a `normalize_url()` step (forces `https`, strips
`www.`, strips trailing slash, strips fragments) applied before both the
duplicate check and storage. Deliberately does **not** strip query
strings, since some carry real meaning and some don't, and there's no
generic way to tell those apart — see `docs/limitations.md`.

**Config decision: trimmed `data/allowlist.json`** from an initial
~10-domain list down to seven core sources (NHS, WHO, Age UK, NIA, NICE,
NLM, Mayo Clinic) to keep the discovery scope focused on
frailty/mobility-relevant, clearly authoritative sources rather than
casting a wide net early.



=============================================================================================================


## Session 2 — stage 2 (structured summarization) design decisions

**Separate `summaries` table, not new columns on `sources`.** Keeps
`sources` a stage-1-only concern (mirrors how `content_drafts` is already
its own table rather than being bolted onto `sources`), and lets a source
be re-summarized — a failed attempt retried, or a prompt version bump
re-run — without destroying the previous attempt. Every summarization run
inserts a new row rather than overwriting. `db.get_latest_summary` reads
back the one row that matters for the pipeline (the most recent attempt);
the full history stays queryable via `db.list_summaries` for audit
purposes and for the "evaluation-driven retries" idea in
`agent-engineering-roadmap.md`.

**Structured output via forced tool-use, not prompted JSON.** `summarize.py`
defines a `record_summary` tool schema matching the `Summary` fields and
calls the API with `tool_choice={"type": "tool", "name": "record_summary"}`.
This guarantees a well-formed arguments object instead of relying on the
model to emit clean JSON in prose (which models sometimes wrap in markdown
fences or precede with a sentence of preamble). The tool's `input` is still
passed through the `Summary` Pydantic model as a second validation layer.

**`prompt_version` convention: a hand-bumped module constant, not a hash
or a full prompt snapshot.** `summarize.py` has `PROMPT_VERSION =
"summarize_v1"`, bumped manually whenever the prompt template's wording
changes meaningfully. The audit log stores this label — not the literal
prompt text — against every summarization event. This is a deliberate
simplification: reconstructing the *exact* prompt used for a past event
means checking out the matching git commit, not just reading the audit
log. A production system handling this at scale would more likely
snapshot the literal prompt text per call (or per version) so the audit
trail is self-contained. Flagged in `docs/limitations.md`.

**`model_version` is captured from the API response (`response.model`),
not hardcoded.** The `MODEL` constant tells the API which model to *call*,
but what gets stored per summary is what the API actually reports back —
so if Anthropic silently routes a request to a slightly different model
snapshot, the audit record still reflects reality.

**Error handling: per-source try/except, with a small fixed retry only
for transient errors.** One bad source (a blocked page, a malformed
model response, a rate limit) shouldn't stop the whole batch. Transient
errors (`anthropic.RateLimitError`, `anthropic.APIConnectionError`) get
up to `MAX_RETRIES` attempts with a short linear backoff; anything else
(validation errors, the model failing to call the tool at all) fails
immediately without retrying, since retrying wouldn't help. Either way,
a failed source is left at `status="discovered"` — untouched, not marked
failed — so a future run naturally picks it back up. This is a "retry the
network," not "retry because the content was bad" strategy; the latter is
explicitly future scope once stage 5's grounding check exists to judge
content quality (see `agent-engineering-roadmap.md`'s planning-loop idea).

**Sources with missing/very short `raw_text` are skipped before calling
the API at all**, rather than sent and left to fail there — cheap local
check (`MIN_RAW_TEXT_CHARS = 50`), saves an API call on pages that
fetched effectively empty (e.g. JS-rendered content BeautifulSoup
couldn't see).


## Session 2 (continued) — non-HTML content handling in discovery.py

Found via real usage, not anticipated in the original design: a NICE
evidence-document URL turned out to be a PDF, and `discovery.py`'s fetch
step (plain `requests` + BeautifulSoup, no content-type check) "parsed"
the raw PDF bytes as HTML anyway — nothing stops BeautifulSoup succeeding
on binary content, since a PDF has no `<tags>` for it to strip. This
silently stored ~840,000 characters of near-garbage text as a source's
`raw_text`, which later broke `summarize.py` (Claude's 200k-token request
limit) — see the summarize.py truncation-cap entry above for the other
half of this same incident.

**Fix:** `fetch_page_text` now checks the response's `Content-Type`
header, and separately checks the raw bytes for the PDF magic number
(`%PDF-`) as a fallback in case a server mislabels its `Content-Type` —
either check raises a new `NotHTMLContentError`, which `discover()`
catches and logs as `decision="skipped_non_html_content"` (distinct from
a generic `fetch_failed`, so the audit log honestly reflects *why* a
source didn't make it through) rather than storing anything.

**Deliberately not built now:** actually extracting text from PDFs (e.g.
via `pypdf`) so this content could be used rather than skipped. Several
allowlisted domains (NICE, NIA) publish real evidence as PDFs, so this is
a real gap, not just an edge case — flagged as a documented future
enhancement in `docs/limitations.md` rather than solved here, to keep
this fix focused on stopping the immediate breakage.