"""Phase 2: auto-enrichment chain (no network — connectors are stubbed)."""

import pytest
from rip.enrich import _hops_from, enrich
from rip.ingest import ingest_profile, run_connector
from rip.models import IngestionRun, Person, SourceRecord
from rip.normalize import NormalizedProfile


def gh_profile(**over) -> NormalizedProfile:
    base = {
        "source": "github", "source_type": "code_hosting", "external_id": "jdoe",
        "url": "https://github.com/jdoe", "raw": {"login": "jdoe"}, "name": "Jane Doe",
        "usernames": ["github:jdoe"], "summary": "ML researcher, ORCID 0000-0002-1111-2222",
        "websites": ["https://janedoe.ai"],
    }
    base.update(over)
    return NormalizedProfile(**base)


def orcid_profile() -> NormalizedProfile:
    return NormalizedProfile(
        source="orcid", source_type="researcher_registry",
        external_id="0000-0002-1111-2222", url="https://orcid.org/0000-0002-1111-2222",
        raw={"orcid": True}, name="Jane Doe", orcid="0000-0002-1111-2222",
    )


def openalex_profile() -> NormalizedProfile:
    return NormalizedProfile(
        source="openalex", source_type="scholarly", external_id="A42",
        url="https://openalex.org/A42", raw={"id": "A42"}, name="Jane Doe",
        orcid="https://orcid.org/0000-0002-1111-2222",
    )


def web_profile() -> NormalizedProfile:
    return NormalizedProfile(
        source="web", source_type="personal_site", external_id="janedoe.ai",
        url="https://janedoe.ai", raw={"url": "https://janedoe.ai", "html": "<html/>"},
        name="Jane Doe", websites=["https://janedoe.ai"],
    )


class StubConnector:
    """Returns a canned profile; records what was asked for."""

    def __init__(self, source, profile, calls):
        self.source = source
        self._profile = profile
        self._calls = calls

    def fetch(self, identifier):
        self._calls.append((self.source, identifier))
        if self._profile is None:
            raise RuntimeError("source unavailable")
        return self._profile


@pytest.fixture()
def stub_connectors(monkeypatch):
    calls: list[tuple[str, str]] = []
    registry = {
        "orcid": orcid_profile(),
        "openalex": openalex_profile(),
        "web": web_profile(),
    }

    def fake_get_connector(source):
        if source not in registry:
            raise ValueError(f"unknown source {source}")
        return StubConnector(source, registry[source], calls)

    monkeypatch.setattr("rip.connectors.get_connector", fake_get_connector)
    return calls, registry


def test_hops_extracted_from_github_profile():
    hops = _hops_from(gh_profile())
    kinds = {(h.source, h.identifier) for h in hops}
    assert ("orcid", "0000-0002-1111-2222") in kinds
    assert ("openalex", "https://orcid.org/0000-0002-1111-2222") in kinds
    assert ("web", "https://janedoe.ai") in kinds


def test_known_hosts_not_fetched_as_generic_web_pages():
    hops = _hops_from(gh_profile(websites=["https://orcid.org/0000-0002-1111-2222"]))
    assert not [h for h in hops if h.source == "web"]


def test_chain_creates_multiple_source_records(session, stub_connectors):
    calls, _ = stub_connectors
    ingest_profile(session, gh_profile())
    enrich(session, gh_profile())
    sources = {r.source for r in session.query(SourceRecord)}
    assert {"github", "orcid", "openalex", "web"} <= sources
    # all on one person: orcid is a strong key shared by orcid+openalex
    assert session.query(Person).count() <= 2


def test_no_loop_on_circular_orcid_openalex(session, stub_connectors):
    calls, _ = stub_connectors
    ingest_profile(session, gh_profile())
    enrich(session, gh_profile())
    # openalex points back at the same ORCID; each pair fetched at most once
    assert len(calls) == len(set(calls))


def test_existing_source_record_is_not_refetched(session, stub_connectors):
    calls, _ = stub_connectors
    ingest_profile(session, gh_profile())
    ingest_profile(session, orcid_profile())  # already present
    result = enrich(session, gh_profile())
    assert ("orcid", "0000-0002-1111-2222") not in calls
    assert any(h.source == "orcid" for h in result.skipped)


def test_failing_hop_does_not_fail_root(session, monkeypatch):
    calls: list = []

    def fake_get_connector(source):
        return StubConnector(source, None, calls)  # always raises

    monkeypatch.setattr("rip.connectors.get_connector", fake_get_connector)
    result = enrich(session, gh_profile())
    assert result.ingested == []
    assert result.failed  # recorded, not raised


def test_run_connector_enrich_flag(session, stub_connectors):
    calls, _ = stub_connectors

    class Root:
        source = "github"

        def fetch(self, identifier):
            return gh_profile()

    run_connector(session, Root(), "jdoe", enrich_chain=False)
    assert calls == []  # nothing chained
    assert session.query(SourceRecord).count() == 1

    run_connector(session, Root(), "jdoe", enrich_chain=True)
    assert calls  # chain ran on the second call
    assert session.query(SourceRecord).count() > 1


def test_enrich_failures_recorded_as_ingestion_runs(session, monkeypatch):
    def fake_get_connector(source):
        return StubConnector(source, None, [])

    monkeypatch.setattr("rip.connectors.get_connector", fake_get_connector)

    class Root:
        source = "github"

        def fetch(self, identifier):
            return gh_profile()

    run_connector(session, Root(), "jdoe", enrich_chain=True)
    errors = session.query(IngestionRun).filter_by(status="error").all()
    assert errors and all(e.error for e in errors)


def test_depth_limit_stops_the_chain(session, stub_connectors):
    calls, _ = stub_connectors
    enrich(session, gh_profile(), depth=0)
    assert calls == []


def test_rate_limited_hop_does_not_fail_root_ingest(session, monkeypatch):
    """A throttled source must degrade the chain, not the ingest."""
    from rip.connectors.base import RateLimitedError
    from rip.models import IngestionRun, Person

    class Throttled:
        def __init__(self, source):
            self.source = source

        def fetch(self, identifier):
            raise RateLimitedError(f"{self.source} rate limited (HTTP 429)")

    monkeypatch.setattr("rip.connectors.get_connector", lambda s: Throttled(s))

    class Root:
        source = "github"

        def fetch(self, identifier):
            return gh_profile()

    person = run_connector(session, Root(), "jdoe", enrich_chain=True)
    # root person survived intact
    assert person.canonical_name == "Jane Doe"
    assert session.query(Person).count() == 1
    # and the throttling is recorded for the operator
    errors = session.query(IngestionRun).filter_by(status="error").all()
    assert errors and any("RateLimitedError" in (e.error or "") for e in errors)


def test_rate_limited_hops_are_reported_separately(session, monkeypatch):
    from rip.connectors.base import RateLimitedError

    class Throttled:
        def __init__(self, source):
            self.source = source

        def fetch(self, identifier):
            raise RateLimitedError("429")

    monkeypatch.setattr("rip.connectors.get_connector", lambda s: Throttled(s))
    result = enrich(session, gh_profile())
    assert result.rate_limited
    assert result.ingested == []


def test_orcid_hop_builds_a_key_openalex_accepts():
    """Regression: the ORCID -> OpenAlex hop 404'd on every author.

    enrich.py hands OpenAlex an ORCID *URL*; the connector reduced any
    identifier to its last path segment, and a bare ORCID is not a key
    OpenAlex resolves. The hop failed silently for every academic, which is
    exactly the hop that ties a scholarly identity to a code one.
    """
    from rip.connectors.openalex import OpenAlexConnector

    key = OpenAlexConnector._author_key
    assert key("https://orcid.org/0009-0007-3143-9875") == "orcid:0009-0007-3143-9875"
    assert key("0009-0007-3143-9875") == "orcid:0009-0007-3143-9875"
    assert key("0000-0002-1825-009X") == "orcid:0000-0002-1825-009X"  # checksum X
    # OpenAlex IDs keep working in both forms
    assert key("https://openalex.org/A5108009533") == "A5108009533"
    assert key("A5108009533") == "A5108009533"


def test_orcid_hop_is_actually_emitted_for_a_profile_carrying_an_orcid():
    """The hop has to exist before the key it builds can matter."""
    from rip.enrich import _hops_from
    from rip.normalize import NormalizedProfile

    profile = NormalizedProfile(
        source="github", source_type="code_hosting", external_id="x",
        url="https://github.com/x", raw={}, name="X",
        orcid="https://orcid.org/0009-0007-3143-9875",
    )
    targets = {h.source: h.identifier for h in _hops_from(profile)}
    assert targets["orcid"] == "0009-0007-3143-9875"
    assert "0009-0007-3143-9875" in targets["openalex"]
