"""Every documented filter on /v1/persons is exercised at least once.

`has_email` raised NameError on every call: PersonKey was used in the query
and never imported. It is a documented filter in the README's table and 711
tests went past it, because not one of them used it. A linter found it in its
first run over this repository.

So this walks the filters the endpoint declares rather than the ones somebody
remembered to test, which is the only version of this test that would have
caught it.
"""

import pytest
from fastapi.testclient import TestClient
from rip.models import Base, Evidence, Person, PersonKey
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rip import api

# a value for each declared query parameter that is worth sending
SAMPLES = {
    "q": "Ada", "skill": "cosmology", "organization": "MIT",
    "current_organization": "MIT", "education": "MIT", "role": "professor",
    "country": "US", "location": "Berlin", "source": "openalex",
    "technology": "python", "has_cv": "true", "has_email": "true",
    "min_publications": "1", "min_citations": "1", "min_sources": "1",
    "sort": "name", "limit": "5", "offset": "0",
    "updated_since": "2020-01-01T00:00:00", "active_since": "2020",
    "name": "Ada", "id": "p1", "ids": "p1",
}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.delenv("RIP_API_TOKEN", raising=False)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    person = Person(id="p1", canonical_name="Ada Lovelace", country="US")
    session.add(person)
    session.add(PersonKey(person_id="p1", key_type="email", key_value="ada@example.org"))
    session.add(Evidence(person_id="p1", attribute_type="cv_url",
                         value="https://example.org/cv.pdf", source="web"))
    session.commit()
    api.app.dependency_overrides[api.get_db] = lambda: session
    try:
        yield session
    finally:
        api.app.dependency_overrides.clear()
        session.close()
        engine.dispose()


def declared_filters():
    route = next(r for r in api.app.routes if getattr(r, "path", "") == "/v1/persons")
    return [p.name for p in route.dependant.query_params]


def test_every_declared_filter_answers(wired):
    client = TestClient(api.app, raise_server_exceptions=False)
    untested = [name for name in declared_filters() if name not in SAMPLES]
    assert not untested, f"no sample value for {untested}; add one rather than skipping"
    broken = []
    for name in declared_filters():
        got = client.get(f"/v1/persons?{name}={SAMPLES[name]}")
        if got.status_code >= 500:
            broken.append((name, got.status_code))
    assert not broken, broken


def test_has_email_selects_on_the_key_not_the_evidence(wired):
    """The filter reads person_key, where an email actually lives."""
    client = TestClient(api.app)
    with_email = client.get("/v1/persons?has_email=true").json()
    without = client.get("/v1/persons?has_email=false").json()
    assert [p["id"] for p in with_email["results"]] == ["p1"]
    assert without["results"] == []

    wired.add(Person(id="p2", canonical_name="Grace Hopper"))
    wired.commit()
    assert [p["id"] for p in
            TestClient(api.app).get("/v1/persons?has_email=false").json()["results"]] == ["p2"]
