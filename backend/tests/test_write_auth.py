"""Who may write when no token is configured.

RIP_API_TOKEN is empty in the shipped .env, and the middleware used to enforce
only when it was set. So the default deployment accepted a plain POST from
anywhere that could reach the port: during a probe of the running server a
shortlist was created, owner "anonymous", with no credential of any kind.

Reads staying open is deliberate -- serving a public read-only snapshot is a
supported deployment and demanding a token to read would break it. An open
WRITE endpoint is never what anybody meant, so without a token those are
answered for this machine only.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rip import api
from rip.models import Base

REMOTE = ("203.0.113.7", 40000)      # TEST-NET-3, never a real peer


@pytest.fixture
def wired(monkeypatch):
    """An app talking to its own empty database.

    One engine shared across threads, because the test client serves on its
    own — the same reason test_review.py builds one this way.
    """
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


def test_a_write_from_somewhere_else_is_refused_when_no_token_is_set(wired):
    client = TestClient(api.app, client=REMOTE)
    refusals = [
        client.post("/v1/shortlists", json={"name": "should not exist"}),
        client.post("/v1/review/duplicates/1/merge", json={}),
        client.delete("/v1/shortlists/1"),
    ]
    for got in refusals:
        assert got.status_code == 401, got.text
        assert "RIP_API_TOKEN" in got.json()["detail"]
    from rip.models import Shortlist
    assert wired.query(Shortlist).count() == 0      # and nothing was written


def test_reads_stay_open_without_a_token(wired):
    """A public read-only snapshot is a deployment this project supports."""
    client = TestClient(api.app, client=REMOTE)
    assert client.get("/v1/auth").status_code == 200
    assert client.get("/v1/persons").status_code == 200


def test_a_write_from_this_machine_still_works(wired):
    """Local development is unchanged: the review UI posts verdicts all day."""
    got = TestClient(api.app).post("/v1/shortlists", json={"name": "local"})
    assert got.status_code == 200
    from rip.models import Shortlist
    assert wired.query(Shortlist).count() == 1


def test_a_forwarded_header_does_not_buy_write_access(wired):
    """X-Forwarded-For is a header; anybody can send one. The check reads the
    socket peer, which is why a deployment behind a reverse proxy has to set
    RIP_API_TOKEN -- every forwarded request looks local from inside."""
    got = TestClient(api.app, client=REMOTE).post(
        "/v1/shortlists", json={"name": "spoofed"},
        headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"})
    assert got.status_code == 401


def test_a_configured_token_still_governs_everything(wired, monkeypatch):
    monkeypatch.setenv("RIP_API_TOKEN", "sekret")
    client = TestClient(api.app)
    assert client.get("/v1/persons").status_code == 401           # reads too
    assert client.post("/v1/shortlists", json={"name": "x"}).status_code == 401
    assert client.get("/v1/persons",
                      headers={"Authorization": "Bearer sekret"}).status_code == 200
