"""
tests/conftest.py

Shared pytest fixtures.

WHY THIS EXISTS
---------------
`audit.log_event` appends to `audit.AUDIT_LOG_PATH` (data/audit_log.jsonl),
which is meant to be an immutable record of what the *real* pipeline did.
Before this fixture existed, running `pytest` wrote test events (e.g. a
"test topic" discovery search) into that same real file, quietly polluting
the audit trail. This autouse fixture redirects every test's audit writes
to a throwaway file so the real log only ever contains real runs.
"""

from __future__ import annotations

import pytest

from src import audit


@pytest.fixture(autouse=True)
def isolated_audit_log(tmp_path, monkeypatch):
    """Point audit.AUDIT_LOG_PATH at a per-test temp file.

    autouse=True means every test gets this without asking for it, so a
    future test can't accidentally forget and write to the real log.
    """
    log_path = tmp_path / "test_audit_log.jsonl"
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", log_path)
    return log_path
