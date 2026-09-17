from rip.connectors.stackoverflow import StackOverflowConnector
from rip.discover import discover_openalex_coauthors
from rip.ingest import ingest_profile
from rip.models import DiscoveryLead, Evidence
from tests.test_ingest import openalex_profile


def test_stackoverflow_search_users():
    conn = StackOverflowConnector.__new__(StackOverflowConnector)
    conn._params = lambda extra=None: {"site": "stackoverflow", **(extra or {})}

    def fake_get(url, params=None):
        assert url.endswith("/users")
        return {"items": [
            {"user_id": 22656, "display_name": "Jon Skeet", "location": "Reading"},
        ]}

    conn.get_json = fake_get
    found = StackOverflowConnector.search_users(conn, "Skeet", limit=5)
    assert found[0]["id"] == "22656"
    assert found[0]["name"] == "Jon Skeet"


def test_stackoverflow_normalization(session):
    user = {
        "user_id": 22656,
        "display_name": "Jon Skeet",
        "location": "Reading, United Kingdom",
        "website_url": "http://csharpindepth.com",
        "link": "https://stackoverflow.com/users/22656/jon-skeet",
        "reputation": 1529300,
    }
    tags = [
        {"tag_name": "c#", "answer_count": 19991, "answer_score": 269851},
        {"tag_name": "java", "answer_count": 10585, "answer_score": 159705},
        {"tag_name": "unused", "answer_count": 0, "answer_score": 0},
    ]
    profile = StackOverflowConnector.normalize(
        StackOverflowConnector.__new__(StackOverflowConnector), user, tags
    )
    assert profile.external_id == "22656"
    assert "csharpindepth.com" in profile.websites[0]
    person = ingest_profile(session, profile)
    assert person.canonical_name == "Jon Skeet"
    skills = {e.value for e in session.query(Evidence).filter_by(attribute_type="skill")}
    assert skills == {"c#", "java"}  # zero-answer tag excluded


def test_openalex_coauthor_discovery(session):
    ingest_profile(session, openalex_profile())  # work W1 has co-author A43 (Bob Ray)
    added = discover_openalex_coauthors(session)
    assert added == 1
    lead = session.query(DiscoveryLead).one()
    assert lead.source == "openalex"
    assert lead.identifier == "A43"
    assert lead.status == "pending"
    assert "co-author of Jane Doe" in lead.reason
    # idempotent: second scan adds nothing
    assert discover_openalex_coauthors(session) == 0


def test_discovery_skips_already_ingested(session):
    ingest_profile(session, openalex_profile())
    # ingest A43 directly first
    p = openalex_profile()
    p.external_id = "A43"
    p.url = "https://openalex.org/A43"
    p.raw = {"author": {"id": "https://openalex.org/A43", "display_name": "Bob Ray"}, "works": []}
    p.name = "Bob Ray"
    p.orcid = None
    p.aliases = []
    ingest_profile(session, p)
    assert discover_openalex_coauthors(session) == 0
