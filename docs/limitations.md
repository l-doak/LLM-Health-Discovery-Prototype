# Known limitations

Honest gaps in the current prototype, and what a production system would
need instead. This is a deliverable, not an afterthought — see the
project brief and the original job description's emphasis on this.

## Discovery (stage 1 — current)

- **Domain allowlist is small and hand-picked** (`data/allowlist.json`
  currently has 7 domains). It's a reasonable starting set for
  frailty/mobility content, but far from comprehensive. A production
  system would need a maintained, reviewed process for adding/removing
  trusted domains, not a static JSON file edited by hand.
  - **Some allowlisted sites block automated fetches.** In testing,
  `mayoclinic.org` search results consistently came back as
  `fetch_failed` even though the domain passed the allowlist check —
  likely bot detection on Mayo Clinic's side rejecting the request
  before we ever see the page. Not investigated further yet (would need
  inspecting the actual response status/headers). Worth deciding
  deliberately later whether to add scraping-friendlier request headers,
  drop sites that block bots, or accept the gap.
- **Page text extraction is naive.** `discovery.fetch_page_text` uses
  BeautifulSoup's `get_text()` after stripping obvious non-content tags
  (script/style/nav/footer/header). This will pull in leftover
  boilerplate (breadcrumbs, related-links lists, cookie notices) on many
  real pages, rather than isolating just the main article body the way a
  dedicated "readability" extraction library would. Worth revisiting if
  summarization quality suffers because of noisy input text.
- **No JavaScript rendering.** Pages that load their main content via
  client-side JavaScript will return little or no usable text, since we
  only fetch the initial HTML response. A headless-browser fetch
  (Playwright) would fix this at the cost of speed and complexity.
- **Subdomain trust is all-or-nothing.** `is_allowed()` treats any
  subdomain of an allowlisted root domain as trusted (e.g. adding
  `nhs.uk` trusts `www.nhs.uk` and `patient.info.nhs.uk` alike). For most
  official health-org sites this is fine, but it's a broader trust
  boundary than allowlisting exact hostnames.
- - **URL normalization doesn't touch query strings.** `normalize_url()`
  collapses scheme (http/https), `www.`, trailing slashes, and
  `#fragments`, but deliberately leaves query strings (`?...`) untouched
  — some carry real meaning (e.g. `?q=falls` on a search page) and some
  are pure tracking noise (`?utm_source=...`), and there's no generic way
  to tell those apart. So the same content reachable via a URL that
  differs only by a tracking parameter will still be stored twice. A
  production system would likely maintain a known-tracking-param
  stripping list (e.g. `utm_*`, `fbclid`, `gclid`) rather than leaving
  all query strings untouched.
- **URL normalization doesn't catch same-content-different-path
  duplicates.** Two stored NIA sources during testing served the same
  article under different URL paths (a legacy path and a redirect-target
  canonical path) — `normalize_url()` only fixes cosmetic string
  variants (scheme, www, trailing slash, fragment) and can't detect this
  case. A proper fix would mean following HTTP redirects to their final
  URL before storing (`requests.get(..., allow_redirects=True)` already
  does this partially — worth checking whether `response.url` differs
  from the requested URL and storing that instead) or comparing page
  titles/content for near-duplicates. Not fixed yet — noted as a
  deliberate stopping point for this session.
  
## Audit logging (current)

- **Local JSONL file, not a real log pipeline.** `data/audit_log.jsonl`
  is fine for a single-user prototype but isn't durable/queryable at
  scale, has no retention policy, and isn't backed up. A production
  system would ship these events to a proper observability/logging
  service.

## Not yet built

- Summarization, human review, draft generation, and evaluation stages
  don't exist yet — see `steadi-portfolio-plan.md` in project knowledge
  for the intended architecture. Limitations for those stages will be
  added here as they're built, rather than speculated about now.
- The `ContentDraft.tags` field has no controlled vocabulary yet (see
  the open question in `docs/schema.md`) — it currently accepts any
  string, which undermines the "agreed schema" goal for tags
  specifically (categories are already locked down).
