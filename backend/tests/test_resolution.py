from rip.ingest import ingest_profile
from rip.models import IdentityLink, Person, SourceRecord
from rip.normalize import EvidenceItem, NormalizedProfile, OrgAffiliation


def make_profile(**overrides) -> NormalizedProfile:
    base = {
        "source": "github",
        "source_type": "code_hosting",
        "external_id": "jsmith",
        "url": "https://github.com/jsmith",
        "raw": {"login": "jsmith"},
        "name": "John Smith",
        "usernames": ["github:jsmith"],
    }
    base.update(overrides)
    return NormalizedProfile(**base)


def test_query_string_urls_stay_distinct(session):
    """Regression: scholar.google.com/citations?user=X must not collide across users."""
    from rip.normalize import normalize_url

    a = normalize_url("https://scholar.google.com/citations?user=AAA")
    b = normalize_url("https://scholar.google.com/citations?user=BBB")
    assert a != b
    p1 = ingest_profile(
        session, make_profile(websites=["https://scholar.google.com/citations?user=AAA"])
    )
    p2 = ingest_profile(
        session,
        make_profile(
            source="dblp",
            external_id="1/1",
            url="https://dblp.org/pid/1/1",
            raw={"name": "Someone Else"},
            name="Someone Else",
            usernames=[],
            websites=["https://scholar.google.com/citations?user=BBB"],
        ),
    )
    assert p1.id != p2.id


def test_new_person_created(session):
    person = ingest_profile(session, make_profile())
    assert person.canonical_name == "John Smith"
    assert session.query(Person).count() == 1
    link = session.query(IdentityLink).one()
    assert link.match_method == "new"


def test_orcid_merges_across_sources(session):
    p1 = ingest_profile(
        session,
        make_profile(orcid="https://orcid.org/0000-0001-2345-6789"),
    )
    p2 = ingest_profile(
        session,
        make_profile(
            source="openalex",
            source_type="scholarly",
            external_id="A123",
            url="https://openalex.org/A123",
            raw={"id": "A123"},
            name="J. Smith",
            usernames=[],
            orcid="0000-0001-2345-6789",
        ),
    )
    assert p1.id == p2.id
    assert session.query(Person).count() == 1
    assert session.query(SourceRecord).count() == 2  # both raw records preserved
    methods = {link.match_method for link in session.query(IdentityLink)}
    assert "strong:orcid" in methods


def test_shared_profile_url_merges(session):
    p1 = ingest_profile(session, make_profile(websites=["https://johnsmith.dev"]))
    p2 = ingest_profile(
        session,
        make_profile(
            source="openalex",
            external_id="A999",
            url="https://openalex.org/A999",
            raw={"id": "A999"},
            name="John A. Smith",
            usernames=[],
            websites=["johnsmith.dev/"],
        ),
    )
    assert p1.id == p2.id


def test_fuzzy_name_plus_org_merges(session):
    p1 = ingest_profile(
        session,
        make_profile(organizations=[OrgAffiliation(name="Acme Labs", is_current=True)]),
    )
    p2 = ingest_profile(
        session,
        make_profile(
            source="openalex",
            external_id="A777",
            url="https://openalex.org/A777",
            raw={"id": "A777"},
            name="Smith John",
            usernames=[],
            organizations=[OrgAffiliation(name="Acme Labs")],
        ),
    )
    assert p1.id == p2.id
    methods = {link.match_method for link in session.query(IdentityLink)}
    assert "fuzzy:name+org" in methods


def test_same_name_different_org_stays_separate(session):
    ingest_profile(
        session,
        make_profile(organizations=[OrgAffiliation(name="Acme Labs")]),
    )
    ingest_profile(
        session,
        make_profile(
            source="openalex",
            external_id="A555",
            url="https://openalex.org/A555",
            raw={"id": "A555"},
            name="John Smith",
            usernames=[],
            organizations=[OrgAffiliation(name="Globex University", relation="studied_at")],
        ),
    )
    assert session.query(Person).count() == 2


def test_corroboration_across_sources(session):
    ingest_profile(
        session,
        make_profile(evidence=[EvidenceItem(attribute_type="skill", value="Rust")]),
    )
    ingest_profile(
        session,
        make_profile(
            source="openalex",
            external_id="A1",
            url="https://openalex.org/A1",
            raw={"id": "A1"},
            usernames=[],
            orcid=None,
            websites=["https://github.com/jsmith"],  # merges via url key
            evidence=[EvidenceItem(attribute_type="skill", value="Rust")],
        ),
    )
    from rip.models import Evidence

    rows = session.query(Evidence).filter_by(attribute_type="skill", value="Rust").all()
    assert len(rows) == 2
    assert all(r.verification_state == "corroborated" for r in rows)
