from rip.ingest import ingest_profile
from rip.models import Evidence, IdentityLink, Person, Publication
from rip.normalize import OrgAffiliation
from rip.review import approve_link, list_suspicious, split_link
from tests.test_resolution import make_profile


def _fuzzy_merged_pair(session):
    """Two profiles merged via fuzzy name+org (no shared strong key)."""
    p1 = ingest_profile(
        session,
        make_profile(organizations=[OrgAffiliation(name="Acme Labs", is_current=True)]),
    )
    p2 = ingest_profile(
        session,
        make_profile(
            source="dblp",
            external_id="9/999",
            url="https://dblp.org/pid/9/999",
            raw={"name": "John Smith"},
            name="John Smith",
            usernames=[],
            organizations=[OrgAffiliation(name="Acme Labs")],
        ),
    )
    assert p1.id == p2.id
    return p1


def test_list_suspicious_shows_fuzzy_merge(session):
    _fuzzy_merged_pair(session)
    report = list_suspicious(session)
    assert len(report["fuzzy_merges"]) == 1
    entry = report["fuzzy_merges"][0]
    assert entry["match_method"] == "fuzzy:name+org"
    assert entry["source"] == "dblp"


def test_approve_clears_from_review_queue(session):
    _fuzzy_merged_pair(session)
    link_id = list_suspicious(session)["fuzzy_merges"][0]["link_id"]
    approve_link(session, link_id)
    assert list_suspicious(session)["fuzzy_merges"] == []
    assert session.get(IdentityLink, link_id).review_state == "approved"


def test_split_detaches_record_into_new_person(session):
    from rip.normalize import EvidenceItem, PublicationData

    person = ingest_profile(
        session,
        make_profile(organizations=[OrgAffiliation(name="Acme Labs", is_current=True)]),
    )
    ingest_profile(
        session,
        make_profile(
            source="dblp",
            external_id="9/999",
            url="https://dblp.org/pid/9/999",
            raw={"name": "John Smith"},
            name="John Smith",
            usernames=[],
            organizations=[OrgAffiliation(name="Acme Labs")],
            evidence=[EvidenceItem(attribute_type="award", value="Test Prize")],
            publications=[PublicationData(title="Some Paper", external_id="dblp:x")],
        ),
    )
    link_id = list_suspicious(session)["fuzzy_merges"][0]["link_id"]
    new_person = split_link(session, link_id)

    assert new_person.id != person.id
    assert new_person.canonical_name == "John Smith"
    # award evidence + authorship moved to the new person
    award = session.query(Evidence).filter_by(attribute_type="award").one()
    assert award.person_id == new_person.id
    pub = session.query(Publication).filter_by(title="Some Paper").one()
    assert pub.authorships[0].person_id == new_person.id
    # original person keeps their github identity
    github_links = [
        l for l in session.query(IdentityLink).filter_by(person_id=person.id)
    ]
    assert len(github_links) == 1
    assert session.get(IdentityLink, link_id).review_state == "split"
    # split merge no longer in review queue
    assert list_suspicious(session)["fuzzy_merges"] == []


def test_bearer_auth_enforced(monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from rip import api
    from rip.db import Base

    monkeypatch.setenv("RIP_API_TOKEN", "sekret")
    # A test database, not whatever rip.db sits in the working directory: a
    # fresh clone has none, and the authorised request failed on it. Shared
    # across threads, because the test client serves on its own.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    api.app.dependency_overrides[api.get_db] = lambda: session
    try:
        c = TestClient(api.app)
        assert c.get("/v1/persons").status_code == 401
        assert c.get("/v1/persons", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/v1/persons", headers={"Authorization": "Bearer sekret"}).status_code == 200
        assert c.get("/docs").status_code == 200  # docs stay open
    finally:
        api.app.dependency_overrides.clear()
        session.close()
        engine.dispose()
