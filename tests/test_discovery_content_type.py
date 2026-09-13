"""
tests/test_discovery_content_type.py

Covers the non-HTML content handling added to discovery.py after a real
NICE evidence URL turned out to be a PDF that got silently mis-parsed as
HTML (see docs/design-decisions.md). This is a separate file from your
existing tests/test_discovery.py so it doesn't risk clashing with
fixtures/conventions already defined there — feel free to fold it in if
you'd rather have one file.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src import db
from src.discovery import NotHTMLContentError, discover, fetch_page_text


def _fake_response(content_type: str, body: bytes, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Content-Type": content_type}
    resp.content = body
    resp.text = body.decode("utf-8", errors="replace")
    resp.raise_for_status = MagicMock()
    return resp


def test_html_response_still_parses_normally():
    html = b"<html><body><script>bad()</script><p>Real article text.</p></body></html>"
    with patch(
        "src.discovery.requests.get",
        return_value=_fake_response("text/html; charset=utf-8", html),
    ):
        text = fetch_page_text("https://example.nhs.uk/page")
    assert text == "Real article text."


def test_pdf_content_type_raises_not_html_error():
    pdf_bytes = b"%PDF-1.6 garbage garbage garbage"
    with patch(
        "src.discovery.requests.get",
        return_value=_fake_response("application/pdf", pdf_bytes),
    ):
        try:
            fetch_page_text("https://example.nhs.uk/doc.pdf")
            assert False, "expected NotHTMLContentError"
        except NotHTMLContentError as e:
            assert e.content_type == "application/pdf"


def test_pdf_mislabeled_as_html_still_caught_by_magic_bytes():
    # Server claims text/html but actually sends PDF bytes — the header
    # alone would miss this; the magic-byte check is what catches it.
    pdf_bytes = b"%PDF-1.6 garbage garbage garbage"
    with patch(
        "src.discovery.requests.get",
        return_value=_fake_response("text/html", pdf_bytes),
    ):
        try:
            fetch_page_text("https://example.nhs.uk/mislabeled.pdf")
            assert False, "expected NotHTMLContentError"
        except NotHTMLContentError:
            pass


def test_discover_skips_non_html_content_without_storing(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()

    fake_allowlist = {"nhs.uk"}
    fake_results = [{"url": "https://www.nhs.uk/evidence.pdf", "title": "Evidence PDF"}]

    with patch("src.discovery.load_allowlist", return_value=fake_allowlist), patch(
        "src.discovery.search_sources", return_value=fake_results
    ), patch(
        "src.discovery.fetch_page_text",
        side_effect=NotHTMLContentError("application/pdf"),
    ):
        stored = discover("test topic")

    assert stored == []
    assert db.list_sources() == []
