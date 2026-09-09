"""
Tests for the pure/offline logic in discovery.py — URL parsing and
allowlist checking. These don't hit the network or Tavily, so they run
fast and don't need an API key: exactly the kind of function worth unit
testing (deterministic input -> deterministic output), as opposed to
search_sources()/discover(), which call external services and are better
suited to manual/integration testing.
"""

from src.discovery import domain_from_url, is_allowed, normalize_url

def test_normalize_url_forces_https():
    assert (
        normalize_url("http://www.nia.nih.gov/health/balance")
        == "https://nia.nih.gov/health/balance"
    )


def test_normalize_url_strips_www():
    assert (
        normalize_url("https://www.ageuk.org.uk/exercise")
        == "https://ageuk.org.uk/exercise"
    )


def test_normalize_url_strips_trailing_slash():
    assert (
        normalize_url("https://nia.nih.gov/health/balance/")
        == "https://nia.nih.gov/health/balance"
    )


def test_normalize_url_strips_fragment():
    assert (
        normalize_url("https://nia.nih.gov/health/balance#section-2")
        == "https://nia.nih.gov/health/balance"
    )


def test_normalize_url_collapses_all_variants_to_the_same_string():
    # This is the actual bug from discovery: the same article, returned
    # by search as several cosmetically different URLs, must all
    # normalize down to one identical string so de-duplication catches
    # them.
    variants = [
        "http://www.nia.nih.gov/health/older-adults-and-balance-problems",
        "https://nia.nih.gov/health/older-adults-and-balance-problems",
        "https://www.nia.nih.gov/health/older-adults-and-balance-problems/",
        "https://www.nia.nih.gov/health/older-adults-and-balance-problems#top",
    ]
    normalized = {normalize_url(v) for v in variants}
    assert len(normalized) == 1


def test_normalize_url_preserves_query_string():
    # Deliberately NOT stripped (see the docstring on normalize_url and
    # docs/limitations.md) — a query string sometimes selects genuinely
    # different content, so we don't assume it's always safe to discard.
    assert (
        normalize_url("https://www.nhs.uk/search?q=falls")
        == "https://nhs.uk/search?q=falls"
    )


def test_domain_from_url_strips_scheme_and_path():
    assert domain_from_url("https://www.nhs.uk/conditions/falls/") == "nhs.uk"


def test_domain_from_url_keeps_non_www_subdomains():
    # Only a leading "www." is stripped — other subdomains are preserved,
    # since is_allowed() handles subdomain matching separately.
    assert domain_from_url("https://patient.info.nhs.uk/page") == "patient.info.nhs.uk"


def test_domain_from_url_no_www():
    assert domain_from_url("https://who.int/news") == "who.int"


def test_is_allowed_exact_match():
    allowlist = {"nhs.uk", "who.int"}
    assert is_allowed("https://www.nhs.uk/conditions/falls/", allowlist) is True


def test_is_allowed_subdomain_of_allowed_domain():
    allowlist = {"nhs.uk"}
    assert is_allowed("https://patient.info.nhs.uk/page", allowlist) is True


def test_is_allowed_rejects_unlisted_domain():
    allowlist = {"nhs.uk", "who.int"}
    assert is_allowed("https://example.com/health-tips", allowlist) is False


def test_is_allowed_rejects_lookalike_domain():
    # "nhs.uk.evil.com" ends with "evil.com", not "nhs.uk" — the suffix
    # check in is_allowed() must not be fooled by an allowed domain
    # appearing as a *prefix* of an unrelated one.
    allowlist = {"nhs.uk"}
    assert is_allowed("https://nhs.uk.evil.com/phish", allowlist) is False
