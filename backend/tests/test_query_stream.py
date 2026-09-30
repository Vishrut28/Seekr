"""The streaming half of the main feature, which nothing had ever run.

/v1/query/stream is 60 statements and is what the UI actually calls — the
source cards are its events. It had never been executed by a test, and it had
drifted from /v1/query on the two things that cost money and change data:

    /v1/query          allow_paid = discover in ("true","queue","1")
                       persist    = _may_write(request)
    /v1/query/stream   allow_paid = True
                       persist    = (not passed — defaults to True)

So an unauthenticated GET to the stream could bill a metered provider and
grow somebody else's corpus, while the same question asked of the endpoint
beside it did neither. That is the fix from e4bee14 applied to one of the two
places that reach live discovery — the same shape as gating
persist_suggestions and leaving _store_results open, and again it was the
live path that was missed.

These tests are written as a comparison rather than as assertions about the
stream alone. "The same search, reported as it happens" is the endpoint's own
first sentence, and a test that only checks the stream cannot notice the two
drifting apart again.
"""

import json

import pytest
from fastapi.testclient import TestClient
from rip.db import Base
from rip.ingest import ingest_profile
from rip.normalize import EvidenceItem
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from tests.test_resolution import make_profile

from rip import api, discovery


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        for i in range(2):
            ingest_profile(s, make_profile(
                external_id=f"p{i}", url=f"https://github.com/p{i}",
                raw={"login": f"p{i}"}, name=f"Rust Person{i}",
                usernames=[f"github:p{i}"],
                evidence=[EvidenceItem(attribute_type="skill", value="Rust",
                                       confidence=0.7)]))
        s.commit()
        yield s
    engine.dispose()


# TestClient reports itself as "testclient", which api.LOOPBACK counts as
# this machine -- correctly, since most tests ARE library use. A caller from
# somewhere else is the case these gates exist for, so the address is set.
REMOTE = ("203.0.113.12", 40000)


@pytest.fixture()
def client(session, monkeypatch):
    """The stream opens its OWN session on a worker thread rather than taking
    the request's, so overriding get_db alone leaves that thread pointed at
    the real database. Both are redirected."""
    monkeypatch.delenv("RIP_API_TOKEN", raising=False)
    api.app.dependency_overrides[api.get_db] = lambda: session
    monkeypatch.setattr(api, "SessionLocal", lambda: session)
    yield TestClient(api.app, client=REMOTE)
    api.app.dependency_overrides.clear()


@pytest.fixture()
def spy(monkeypatch):
    """Records how discovery was asked, and answers with nothing."""
    calls = []

    def fake(session=None, parsed=None, limit=10, allow_paid=True,
             on_source=None, persist=True):
        calls.append({"allow_paid": allow_paid, "persist": persist})
        return []

    # Patched on the module that DEFINES it: both handlers import the name
    # inside the function body, so they pick it up from rip.discovery at call
    # time and a shim on rip.api would be read by neither.
    monkeypatch.setattr(discovery, "discovery_suggestions", fake)
    return calls


def events(response):
    out = []
    for block in response.text.split("\n\n"):
        block = block.strip()
        if block.startswith("data: "):
            out.append(json.loads(block[len("data: "):]))
    return out


def kinds(response):
    return [e["type"] for e in events(response)]


# --------------------------------------------------------------------------
# it runs at all
# --------------------------------------------------------------------------

def test_the_stream_reaches_a_result(client, spy):
    response = client.get("/v1/query/stream", params={"q": "rust"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert kinds(response)[0] == "plan", kinds(response)
    assert "results" in kinds(response), kinds(response)
    assert "error" not in kinds(response), events(response)


def test_live_search_failing_still_streams_the_corpus_answer(client, monkeypatch):
    """A failure inside discovery ended the stream with an error event before
    the corpus was even searched: nothing came back at all."""
    def boom(*_a, **_k):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(discovery, "discovery_suggestions", boom)
    response = client.get("/v1/query/stream", params={"q": "rust", "discover": "true"})
    got = events(response)
    assert "error" not in kinds(response), got
    results = next(e for e in got if e["type"] == "results")
    assert results["count"] == 2
    assert {"type": "source", "source": "live", "state": "failed",
            "reason": "RuntimeError"} in got


def test_the_results_event_carries_the_people(client, spy):
    response = client.get("/v1/query/stream", params={"q": "rust"})
    results = next(e for e in events(response) if e["type"] == "results")
    assert results["count"] == 2
    assert {r["canonical_name"] for r in results["results"]} == {
        "Rust Person0", "Rust Person1"}
    assert all("score" in r for r in results["results"]), "ranking not attached"


# --------------------------------------------------------------------------
# what it costs and what it keeps, against /v1/query
# --------------------------------------------------------------------------

def test_an_anonymous_stream_does_not_write_to_the_corpus(client, spy):
    """The bug. `persist` was never passed, so it defaulted to True and any
    GET grew the graph."""
    client.get("/v1/query/stream", params={"q": "rust"})
    assert spy, "discovery was never reached — the test proves nothing"
    assert spy[-1]["persist"] is False


def test_an_anonymous_stream_does_not_reach_a_metered_provider(client, spy):
    """allow_paid was hardcoded True."""
    client.get("/v1/query/stream", params={"q": "rust"})
    assert spy[-1]["allow_paid"] is False


def test_asking_for_paid_sources_still_works(client, spy):
    client.get("/v1/query/stream", params={"q": "rust", "discover": "true"})
    assert spy[-1]["allow_paid"] is True


def test_the_stream_says_whether_it_kept_anything(client, spy):
    """/v1/query reports `persisted`; a caller of the stream could not tell."""
    response = client.get("/v1/query/stream", params={"q": "rust"})
    results = next(e for e in events(response) if e["type"] == "results")
    assert results["persisted"] is False


def test_the_two_endpoints_agree_about_cost_and_writes(client, spy):
    """The comparison that matters: ask the same question both ways and the
    gates must come out the same. A test of the stream alone cannot see them
    drift apart again."""
    client.get("/v1/query/stream", params={"q": "rust"})
    stream_gates = spy[-1]

    client.get("/v1/query", params={"q": "rust", "discover": "true"})
    plain_paid = spy[-1]

    client.get("/v1/query/stream", params={"q": "rust", "discover": "true"})
    stream_paid = spy[-1]

    assert stream_gates["persist"] is False
    assert stream_paid["allow_paid"] == plain_paid["allow_paid"] is True
    assert stream_paid["persist"] == plain_paid["persist"] is False
