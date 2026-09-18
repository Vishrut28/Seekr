from rip.connectors.github import GitHubConnector
from rip.connectors.openalex import OpenAlexConnector
from rip.ingest import ingest_profile
from rip.models import ChangeLog, Evidence, Project, Publication, SourceRecord

GITHUB_USER = {
    "login": "jdoe",
    "name": "Jane Doe",
    "company": "@AcmeAI",
    "blog": "https://janedoe.ai",
    "location": "Berlin, Germany",
    "email": None,
    "bio": "ML systems engineer",
    "twitter_username": "janedoe",
    "html_url": "https://github.com/jdoe",
}
GITHUB_REPOS = [
    {
        "name": "fastserve",
        "language": "Rust",
        "fork": False,
        "description": "High-throughput model server",
        "html_url": "https://github.com/jdoe/fastserve",
        "stargazers_count": 900,
        "forks_count": 50,
        "open_issues_count": 3,
        "created_at": "2021-04-01T00:00:00Z",
        "pushed_at": "2025-12-01T00:00:00Z",
    },
    {
        "name": "toolkit",
        "language": "Python",
        "fork": False,
        "description": None,
        "html_url": "https://github.com/jdoe/toolkit",
        "stargazers_count": 12,
        "forks_count": 1,
        "open_issues_count": 0,
        "created_at": "2020-01-01T00:00:00Z",
        "pushed_at": "2024-06-01T00:00:00Z",
    },
    {"name": "forked", "language": "Go", "fork": True, "html_url": "x", "stargazers_count": 0},
]

OPENALEX_AUTHOR = {
    "id": "https://openalex.org/A42",
    "display_name": "Jane Doe",
    "display_name_alternatives": ["J. Doe"],
    "orcid": "https://orcid.org/0000-0002-1111-2222",
    "last_known_institutions": [{"display_name": "Acme AI", "type": "company"}],
    "topics": [{"display_name": "Distributed Systems", "count": 14}],
}
OPENALEX_WORKS = [
    {
        "id": "https://openalex.org/W1",
        "display_name": "Efficient Serving of Large Models",
        "publication_date": "2023-05-01",
        "doi": "https://doi.org/10.1234/abcd",
        "cited_by_count": 87,
        "primary_location": {"source": {"display_name": "MLSys"}},
        "topics": [{"display_name": "Machine Learning Systems"}],
        "authorships": [
            {"author": {"id": "https://openalex.org/A42", "display_name": "Jane Doe"}},
            {"author": {"id": "https://openalex.org/A43", "display_name": "Bob Ray"}},
        ],
    }
]


def github_profile():
    return GitHubConnector.normalize(GitHubConnector.__new__(GitHubConnector), GITHUB_USER, GITHUB_REPOS)


def openalex_profile():
    return OpenAlexConnector.normalize(
        OpenAlexConnector.__new__(OpenAlexConnector), OPENALEX_AUTHOR, OPENALEX_WORKS
    )


def test_github_normalization_and_ingest(session):
    person = ingest_profile(session, github_profile())
    assert person.canonical_name == "Jane Doe"
    assert person.location == "Berlin, Germany"
    assert person.current_organization == "AcmeAI"
    skills = {
        e.value for e in session.query(Evidence).filter_by(attribute_type="skill")
    }
    assert {"Rust", "Python"} <= skills
    # forked repo excluded, own repos become projects
    projects = session.query(Project).all()
    assert {p.name for p in projects} == {"fastserve", "toolkit"}
    assert projects[0].activity["stars"] in (900, 12)


def test_reingest_is_idempotent(session):
    ingest_profile(session, github_profile())
    before_evidence = session.query(Evidence).count()
    before_projects = session.query(Project).count()
    ingest_profile(session, github_profile())
    assert session.query(Evidence).count() == before_evidence
    assert session.query(Project).count() == before_projects
    assert session.query(SourceRecord).count() == 1


def test_cross_source_merge_and_publications(session):
    p1 = ingest_profile(session, github_profile())
    p2 = ingest_profile(session, openalex_profile())
    # merged via fuzzy name + shared-ish org? Orgs differ ("AcmeAI" vs "Acme AI"),
    # so merge should happen only if a strong key matched; here none do -> two persons.
    # This documents current behavior: near-miss org strings do NOT merge.
    assert (p1.id == p2.id) is False
    pub = session.query(Publication).one()
    assert pub.doi == "10.1234/abcd"
    assert pub.citations == 87
    assert pub.venue == "MLSys"


def test_location_conflict_preserved(session):
    ingest_profile(session, github_profile())
    moved = github_profile()
    moved.location = "Zurich, Switzerland"
    moved.raw = {"user": {**GITHUB_USER, "location": "Zurich, Switzerland"}, "repos": GITHUB_REPOS}
    person = ingest_profile(session, moved)
    # original value kept on person, conflict logged, both evidence rows exist
    assert person.location == "Berlin, Germany"
    conflict = (
        session.query(ChangeLog).filter(ChangeLog.field == "conflict:location").one()
    )
    assert conflict.new_value == "Zurich, Switzerland"
    locations = {
        e.value for e in session.query(Evidence).filter_by(attribute_type="location")
    }
    assert locations == {"Berlin, Germany", "Zurich, Switzerland"}


def test_the_same_subject_in_two_casings_is_one_interest(session):
    """Europe PMC writes MeSH terms both ways. Matched exactly, "Artificial
    Intelligence" and "Artificial intelligence" became two interests on one
    profile, and neither counted as corroborating the other — the person
    looked like they had listed the same subject twice and nothing was
    confirmed by a second source."""
    from rip.normalize import EvidenceItem
    from tests.test_resolution import make_profile

    ingest_profile(session, make_profile(
        external_id="a", url="https://openalex.org/a", raw={"id": "a"},
        name="Case Person", usernames=["openalex:a"],
        evidence=[EvidenceItem(attribute_type="research_interest",
                               value="Artificial Intelligence")]))
    ingest_profile(session, make_profile(
        source="europepmc", external_id="b", url="https://europepmc.org/b",
        raw={"id": "b"}, name="Case Person", usernames=["openalex:a"],
        evidence=[EvidenceItem(attribute_type="research_interest",
                               value="Artificial intelligence")]))
    session.commit()

    rows = session.query(Evidence).filter(
        Evidence.attribute_type == "research_interest").all()
    assert len(rows) == 2, [r.value for r in rows]
    # both are filed under the spelling that arrived first
    assert {r.value for r in rows} == {"Artificial Intelligence"}
    # and the second source now confirms the first rather than sitting beside it
    assert {r.verification_state for r in rows} == {"corroborated"}
