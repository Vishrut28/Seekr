"""GET /v1/query is a read that writes, and that had no gate on it.

When the corpus cannot answer, live discovery searches the free sources and
keeps what they hand over in full. That is deliberate: the provider has been
paid by the time the payload arrives, so storing it means the next person
asking is answered from the graph instead of from the provider again.

What it also meant is that an unauthenticated GET grew somebody else's
database. Probing the running server with a handful of ordinary queries added
47 people to the real corpus, three of which were not people at all. So the
write half follows the same rule as every other write: a configured token, or
this machine.
"""

import pytest
from fastapi.testclient import TestClient
from rip.models import Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rip import api, nlq

REMOTE = ("203.0.113.12", 40000)


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.delenv("RIP_API_TOKEN", raising=False)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    api.app.dependency_overrides[api.get_db] = lambda: session
    try:
        yield session
    finally:
        api.app.dependency_overrides.clear()
        session.close()
        engine.dispose()


@pytest.fixture
def watched(monkeypatch):
    """Record how discovery was asked to behave, without going near a network."""
    calls = []

    def fake(session=None, parsed=None, limit=10, allow_paid=True, on_source=None,
             persist=True):
        calls.append({"persist": persist})
        return []

    monkeypatch.setattr(api, "discovery_suggestions", fake, raising=False)
    monkeypatch.setattr(nlq, "discovery_suggestions", fake)
    return calls


def test_a_stranger_gets_the_search_without_the_write(wired, watched):
    got = TestClient(api.app, client=REMOTE).get(
        "/v1/query?q=quantum%20gravity%20researchers&discover=true")
    assert got.status_code == 200
    assert got.json()["persisted"] is False
    assert watched and watched[-1]["persist"] is False


def test_this_machine_still_keeps_what_it_paid_for(wired, watched):
    got = TestClient(api.app).get(
        "/v1/query?q=quantum%20gravity%20researchers&discover=true")
    assert got.status_code == 200
    assert got.json()["persisted"] is True
    assert watched and watched[-1]["persist"] is True


def test_a_token_holder_may_write_from_anywhere(wired, watched, monkeypatch):
    monkeypatch.setenv("RIP_API_TOKEN", "sekret")
    got = TestClient(api.app, client=REMOTE).get(
        "/v1/query?q=quantum%20gravity%20researchers&discover=true",
        headers={"Authorization": "Bearer sekret"})
    assert got.status_code == 200
    assert got.json()["persisted"] is True


def test_a_stranger_cannot_fill_the_discovery_lead_queue_either(wired, watched):
    """discover=queue records intent for a worker to ingest later. Intent is
    still a write."""
    got = TestClient(api.app, client=REMOTE).get(
        "/v1/query?q=quantum%20gravity%20researchers&discover=queue")
    assert got.status_code == 200
    assert got.json().get("queued_leads", 0) == 0
    from rip.models import DiscoveryLead
    assert wired.query(DiscoveryLead).count() == 0


def test_the_flag_stops_the_store_itself_not_only_the_caller(session, monkeypatch):
    """Load-bearing at the bottom, not only at the top.

    The tests above stub discovery out to watch what it is ASKED to do. This
    one drives the real discovery_suggestions against a fake source, and
    asserts on the corpus rather than on which function ran -- there are two
    storage paths, a sequential one through persist_suggestions and a parallel
    one that calls _store_results directly. Gating only the first left the
    second wide open, and only this test noticed.
    """
    from rip.models import Person
    from rip.normalize import NormalizedProfile

    class Fake:
        source = "orcid"

        def __init__(self, *_a):
            pass

        def fetch(self, identifier):
            return NormalizedProfile(
                source="orcid", source_type="scholarly", external_id=identifier,
                url=f"https://orcid.org/{identifier}", raw={"id": identifier},
                name=f"Ada {identifier}", usernames=[f"orcid:{identifier}"])

    def searcher(who):
        return lambda _q, _limit: [
            {"source": "orcid", "external_id": who, "name": f"Ada {who}"}]

    monkeypatch.setattr("rip.connectors.get_connector", lambda s: Fake())

    monkeypatch.setattr(nlq, "SUGGESTION_SEARCHERS", (("orcid", searcher("one"), False),))
    nlq.discovery_suggestions(session, nlq.parse(session, "Ada Lovelace"),
                              allow_paid=False, persist=False)
    assert session.query(Person).count() == 0, "persist=False still grew the corpus"

    # a different candidate, so the search cache cannot answer for it
    monkeypatch.setattr(nlq, "SUGGESTION_SEARCHERS", (("orcid", searcher("two"), False),))
    nlq.discovery_suggestions(session, nlq.parse(session, "Ada Byron"),
                              allow_paid=False, persist=True)
    assert session.query(Person).count() == 1, "persist=True stored nothing"
