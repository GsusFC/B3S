"""Shared pytest setup for the Brand3 test suite.

Isolates unit tests from any real LLM provider: the default analyzer used by
`src.reports.narrative` is stubbed out so that `None` always propagates,
which in turn routes every narrative call through its deterministic
fallback. Tests that want to exercise the LLM path must pass an explicit
`analyzer=<mock>` argument — see `tests/test_reports_narrative.py`.
"""

from __future__ import annotations

import os

import pytest

from src.reports import narrative

# Input-collection paths gate on a configured Exa key; the dev .env provides
# one locally but CI and clean checkouts have none, which flips those tests
# into the missing_key branch. Default a dummy so the suite is hermetic.
os.environ.setdefault("EXA_API_KEY", "test-exa-key")


@pytest.fixture(autouse=True)
def _disable_real_llm_default(monkeypatch):
    monkeypatch.setattr(narrative, "_default_analyzer", lambda: None)
    narrative.clear_cache()
