"""
discovery.py
============

Stage 1 of the pipeline: search the web for candidate health-information
sources, keep only the ones on an approved domain allowlist, fetch and
clean their text, and store them in SQLite for the next stage
(summarization) to pick up.

FLOW
----
    topic (e.g. "fall prevention exercises for older adults")
        -> Tavily search API returns a list of {url, title, ...} results
        -> filter: keep only results whose domain is in data/allowlist.json
        -> fetch each kept URL's HTML and strip it down to readable text
        -> store as a Source row with status="discovered"
        -> log every decision (kept / filtered out / fetch failed) to the
           audit log, so you can later answer "why wasn't X included?"

DESIGN DECISIONS
-----------------
- Domain filtering happens BEFORE fetching. There's no point spending a
  network request fetching a page we're going to discard anyway — and it
  means a malicious/irrelevant URL never even gets downloaded.
- Fetching uses plain `requests` + BeautifulSoup rather than a headless
  browser (e.g. Playwright). Most health-org pages are static HTML, and a
  full browser is much slower and heavier. Trade-off: this will produce
  poor/empty text for pages that render their content with JavaScript —
  worth noting in docs/limitations.md if you hit that in practice.
- Every source is logged via audit.log_event, including ones that get
  filtered out or fail to fetch — auditability means recording what
  DIDN'T make it through, not just what did.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tavily import TavilyClient

from src import db
from src.audit import log_event

# Load ANTHROPIC_API_KEY / TAVILY_API_KEY from a local .env file (see
# .env.example). This is a no-op if .env doesn't exist, so it's safe to
# import this module even before the file is created — though the Tavily
# call itself will fail without a real key.
load_dotenv()

ALLOWLIST_PATH = Path(__file__).resolve().parent.parent / "data" / "allowlist.json"

# A realistic User-Agent header. Some sites block requests that look like
# they're coming from a generic script; this is a small courtesy, not a
# way of disguising what the request is.
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; SteadiPrototypeBot/0.1; "
        "+https://github.com/ - student portfolio project)"
    )
}

# Fetching a slow or hung server shouldn't stall the whole discovery run.
FETCH_TIMEOUT_SECONDS = 10


def load_allowlist() -> set[str]:
    """Load the approved domains from data/allowlist.json.

    Returns a set (not a list) because we only ever need fast membership
    checks ("is this domain in the allowlist?"), and sets do that in
    O(1) instead of scanning a list each time.
    """
    with ALLOWLIST_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return set(data["domains"])


def domain_from_url(url: str) -> str:
    """Extract the bare domain from a URL, e.g.
    'https://www.nhs.uk/conditions/falls/' -> 'nhs.uk'

    Strips a leading 'www.' so 'www.nhs.uk' and 'nhs.uk' are treated the
    same when checked against the allowlist.
    """
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc

def normalize_url(url: str) -> str:
    """Collapse cosmetic URL variants that point at the same page into one
    canonical string, so they de-duplicate against each other in the
    database.

    Without this, a search can return the *same* article as several
    different URLs — e.g.:
        http://www.nia.nih.gov/health/older-adults-and-balance-problems
        https://nia.nih.gov/health/older-adults-and-balance-problems
        https://www.nia.nih.gov/health/older-adults-and-balance-problems#section
    — and since db.source_exists() checks for an exact string match,
    each variant would get stored (and later summarized/drafted) as if
    it were a separate source. That wastes both fetch requests now and
    LLM calls at the summarization stage later.

    What this normalizes:
      - scheme: always forced to "https" (http vs https is not a
        meaningful difference for our purposes — we're not fetching
        both to compare, and https is one less thing to think about)
      - "www.": stripped, same reasoning as domain_from_url()
      - trailing slash: stripped from the path (Age UK's page and
        Age UK's page/ are the same page)
      - fragment (the "#section" part): dropped — fragments identify a
        spot *within* a page, not a different page

    What this deliberately does NOT normalize (see docs/limitations.md):
      - query strings (e.g. "?utm_source=..."). Some query strings are
        meaningless tracking params and some genuinely select different
        content, and we can't tell those apart generically — collapsing
        them all away risks merging two actually-different pages.
    """
    parsed = urlparse(url)

    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    path = parsed.path.rstrip("/") or "/"

    # Rebuild the URL: force scheme to "https", drop the fragment
    # (5th element of the tuple), keep the query string as-is.
    return urlunparse(("https", netloc, path, "", parsed.query, ""))


def is_allowed(url: str, allowlist: set[str]) -> bool:
    """Check whether a URL's domain is on the allowlist.

    Uses a suffix check (`domain == allowed or domain.endswith("." + allowed)`)
    rather than exact match, so that a subdomain like
    'patient.info.nhs.uk' is still covered by an allowlist entry of
    'nhs.uk'. This is deliberately permissive — it trusts the whole
    domain, including subdomains, once the root domain is approved.
    """
    domain = domain_from_url(url)
    return any(
        domain == allowed or domain.endswith("." + allowed) for allowed in allowlist
    )


def fetch_page_text(url: str) -> str | None:
    """Download a URL and return its readable text content, or None on
    failure (network error, non-200 status, or no extractable text).

    WHY BeautifulSoup + get_text() rather than something fancier:
    this is a simple, dependency-light way to strip HTML tags, scripts,
    and styles down to plain text. It won't be as clean as a dedicated
    "readability" extraction library (which tries to isolate just the
    main article body from navigation/footers/ads) — that's a reasonable
    upgrade to note in docs/limitations.md if the extracted text turns
    out noisy in practice.
    """
    try:
        response = requests.get(
            url, headers=REQUEST_HEADERS, timeout=FETCH_TIMEOUT_SECONDS
        )
        response.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(response.text, "lxml")

    # Remove elements that never contain useful body text.
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    # get_text(separator=" ") joins text nodes with spaces rather than
    # jamming words together; the extra whitespace is then collapsed.
    text = soup.get_text(separator=" ")
    text = " ".join(text.split())

    return text if text else None


def search_sources(
    topic: str, max_results: int = 10, include_domains: list[str] | None = None
) -> list[dict]:
    """Query the Tavily search API for a topic and return raw results.

    Each result is a dict with at least 'url' and 'title' keys (Tavily's
    response shape). We don't filter or fetch here — that's the caller's
    job — this function's only responsibility is "ask Tavily, hand back
    what it said."

    Args:
        include_domains: If given, Tavily restricts its search to these
            domains directly, rather than searching the whole web and
            leaving us to filter afterward. This is the main lever for
            actually getting results back: asking Tavily for "fall
            prevention exercises" with no domain restriction returns
            whatever ranks best generally (often commercial health
            sites, YouTube, etc.), almost none of which will be on our
            small allowlist. Passing our allowlist in here means Tavily
            only looks within it in the first place, so we stop wasting
            search quota on results we were always going to discard.
    """
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError(
            "TAVILY_API_KEY is not set. Copy .env.example to .env and add your key."
        )

    client = TavilyClient(api_key=api_key)
    search_kwargs: dict = {"query": topic, "max_results": max_results}
    if include_domains:
        search_kwargs["include_domains"] = include_domains

    response = client.search(**search_kwargs)
    return response.get("results", [])


def discover(topic: str, max_results: int = 10) -> list[db.Source]:
    """Run the full discovery step for one topic: search, filter, fetch,
    store, and log. Returns the list of newly-stored Source objects
    (sources that were already in the database from a previous run are
    skipped, not re-returned).

    This is the function other code (a CLI, a script, eventually an
    orchestration loop) should call — it's the public entry point for
    stage 1 of the pipeline.
    """
    allowlist = load_allowlist()
    # Pass the allowlist to Tavily as include_domains, so the search
    # itself is restricted to trusted domains rather than searching the
    # whole web and relying on the loop below to filter afterward.
    raw_results = search_sources(
        topic, max_results=max_results, include_domains=sorted(allowlist)
    )

    log_event(
        stage="discovery",
        inputs={"topic": topic, "max_results": max_results},
        output={"raw_result_count": len(raw_results)},
        decision="searched",
    )

    stored_sources: list[db.Source] = []

    for result in raw_results:
        raw_url = result.get("url", "")
        title = result.get("title", "(untitled)")

        if not raw_url:
            continue

        # Collapse cosmetic variants (http vs https, www., trailing slash,
        # #fragment) to one canonical form before doing anything else, so
        # the same underlying page is only ever fetched/stored/summarized
        # once, however many differently-shaped URLs it turns up as.
        url = normalize_url(raw_url)

        # --- Domain filter -------------------------------------------------
        if not is_allowed(url, allowlist):
            log_event(
                stage="discovery",
                inputs={"url": url, "raw_url": raw_url, "title": title},
                decision="filtered_out_domain_not_allowed",
            )
            continue

        # --- Skip duplicates -------------------------------------------------
        # Checking the *normalized* url is what actually catches
        # near-duplicate results now — a second search hit for the same
        # article under a slightly different raw URL will normalize to a
        # url already stored from earlier in this same loop (insert_source
        # commits immediately, so this check sees it right away) or from a
        # previous run.
        if db.source_exists(url):
            log_event(
                stage="discovery",
                inputs={"url": url, "raw_url": raw_url},
                decision="skipped_already_stored",
            )
            continue

        # --- Fetch + clean text ----------------------------------------------
        text = fetch_page_text(url)
        if not text:
            log_event(
                stage="discovery",
                inputs={"url": url, "raw_url": raw_url, "title": title},
                decision="fetch_failed",
            )
            continue

        # --- Store -------------------------------------------------------------
        # Store the normalized url, not raw_url — that's what makes future
        # source_exists() checks (including within this same loop) work.
        source = db.Source(
            url=url,
            domain=domain_from_url(url),
            title=title,
            raw_text=text,
            status="discovered",
        )
        db.insert_source(source)
        stored_sources.append(source)

        log_event(
            stage="discovery",
            inputs={"url": url, "raw_url": raw_url, "title": title},
            output={"source_id": source.source_id, "text_length": len(text)},
            decision="stored",
        )

    return stored_sources


if __name__ == "__main__":
    # Simple manual entry point for testing this module on its own, e.g.:
    #   python -m src.discovery
    # A proper CLI (argparse, accepting --topic) can replace this once
    # we're wiring discovery into review.py.
    import sys

    db.init_db()

    query = sys.argv[1] if len(sys.argv) > 1 else "fall prevention exercises for older adults"
    found = discover(query)

    print(f"Discovered {len(found)} new source(s) for topic: {query!r}")
    for s in found:
        print(f"  - [{s.domain}] {s.title} ({s.url})")
