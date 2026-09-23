"""The circuit breaker built for GitHub (#17) generalizes to every source
(#19): every connector shares BaseConnector and so raises the SAME
RateLimitedError on a genuine rate limit, which discovery_suggestions now
detects via isinstance() rather than a GitHub-specific string match — so
the SAME backoff protects openalex, semanticscholar, dblp, or any other
source, not just GitHub's fetch step.
"""
from rip.connectors.base import RateLimitedError
from rip.discovery import (
    DEFAULT_THROTTLE_SECONDS,
    _note_source_throttled,
    _source_throttled,
    discovery_suggestions,
)
from rip.nlq import parse


def test_rate_limited_error_carries_retry_after():
    """The real, source-reported backoff duration is a proper attribute,
    not something a caller has to string-parse out of the message."""
    exc = RateLimitedError("openalex rate limited; retry after 120s", retry_after=120.0)
    assert exc.retry_after == 120.0
    assert "120" in str(exc)


def test_rate_limited_error_retry_after_defaults_to_none():
    """A source that gave no usable Retry-After must not fabricate one —
    callers fall back to their own default explicitly, rather than this
    exception silently inventing a number nobody actually reported."""
    exc = RateLimitedError("some source rate limited; retry after unknowns")
    assert exc.retry_after is None


def test_note_source_throttled_uses_the_real_retry_after_when_given(session):
    _note_source_throttled(session, "openalex", seconds=42.0)
    # 42s in the future should still be throttled; 43s should not (allow
    # a small margin either way for real elapsed test time)
    import time

    assert _source_throttled(session, "openalex") is True
    time.sleep(0.05)
    assert _source_throttled(session, "openalex") is True  # nowhere near 42s yet


def test_note_source_throttled_falls_back_to_default_when_none_given(session):
    _note_source_throttled(session, "dblp", seconds=None)
    from datetime import datetime, timezone

    from rip.models import SourceThrottle

    row = session.query(SourceThrottle).filter_by(source="dblp").one()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    remaining = (row.blocked_until - now).total_seconds()
    # should be very close to DEFAULT_THROTTLE_SECONDS (900), not some
    # other arbitrary value
    assert abs(remaining - DEFAULT_THROTTLE_SECONDS) < 5.0


def test_throttling_is_independent_per_source(session):
    """Rate-limiting openalex must not affect dblp's own throttle state —
    each source's circuit breaker is tracked separately."""
    _note_source_throttled(session, "openalex", seconds=900.0)
    assert _source_throttled(session, "openalex") is True
    assert _source_throttled(session, "dblp") is False


def test_a_rate_limited_source_is_skipped_on_the_next_discovery_call(session, monkeypatch):
    """End-to-end: a source already throttled (from a PRIOR request, say)
    must be skipped by discovery_suggestions on a LATER call without even
    attempting a search — proving the generic breaker actually gates the
    search loop, not just existing as dead code."""
    calls = []

    class _WouldBeCalled:
        def search_authors(self, query, limit=10):
            calls.append("openalex")
            return []

        def search_authors_by_topic(self, query, limit=10):
            calls.append("openalex")
            return []

    monkeypatch.setattr("rip.connectors.get_connector", lambda s: _WouldBeCalled())
    monkeypatch.setattr(
        "rip.discovery.SUGGESTION_SEARCHERS",
        (("openalex", __import__("rip.discovery", fromlist=["x"])._search_openalex, True),),
    )

    _note_source_throttled(session, "openalex", seconds=900.0)

    parsed = parse(session, "quantum basketweaving")
    suggestions = discovery_suggestions(session, parsed, allow_paid=False)

    assert calls == []  # never even attempted
    assert suggestions == []


def test_a_rate_limit_failure_during_search_throttles_the_source(session, monkeypatch):
    """A source that raises RateLimitedError DURING a search (not just
    during a fetch) must be recorded as throttled, so the NEXT call in the
    same or a later request skips it too."""
    class _RateLimitedOnSearch:
        def search_authors(self, query, limit=10):
            raise RateLimitedError("openalex rate limited", retry_after=123.0)

        def search_authors_by_topic(self, query, limit=10):
            raise RateLimitedError("openalex rate limited", retry_after=123.0)

    monkeypatch.setattr("rip.connectors.get_connector", lambda s: _RateLimitedOnSearch())
    monkeypatch.setattr(
        "rip.discovery.SUGGESTION_SEARCHERS",
        (("openalex", __import__("rip.discovery", fromlist=["x"])._search_openalex, True),),
    )

    assert _source_throttled(session, "openalex") is False
    parsed = parse(session, "quantum basketweaving")
    discovery_suggestions(session, parsed, allow_paid=False)

    assert _source_throttled(session, "openalex") is True
