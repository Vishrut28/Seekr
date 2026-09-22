from rip.connectors.github import GitHubConnector
from rip.connectors.openalex import OpenAlexConnector
from rip.ingest import ingest_profile
from rip.models import (ChangeLog, Evidence, Person, Project, Publication,
                        SourceRecord)

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


def _typed(value):
    from rip.normalize import EvidenceItem
    from tests.test_resolution import make_profile
    return make_profile(
        external_id="rl", url="https://orcid.org/rl", raw={"id": "rl"},
        name="Hernando Example", usernames=["orcid:rl"],
        evidence=[EvidenceItem(attribute_type="research_interest", value=value)])


def test_a_misspelled_keyword_is_stored_as_the_word_it_meant(session):
    """A keyword is what a person typed about themselves, and ORCID keeps the
    slip. Four profiles say "Reinforcment Learning", which made those four
    unfindable under the right spelling -- and made the misspelling a
    vocabulary term in its own right, so a query repeating the typo matched it
    exactly and never reached the people who do the subject."""
    from rip.ingest import ingest_profile

    ingest_profile(session, _typed("Reinforcment Learning"))
    session.commit()
    row = session.query(Evidence).filter(
        Evidence.attribute_type == "research_interest").one()
    assert row.value == "Reinforcement Learning"
    # what the source said is not lost, and the payload still has it verbatim
    assert "'Reinforcment Learning'" in row.extracted_info


def test_the_repair_survives_the_next_refresh_of_that_record(session):
    """The reason this lives in ingest and not in a one-off UPDATE.

    _retract_evidence deletes any row its source record no longer asserts. A
    corrected row is exactly that: ORCID keeps sending the typo, so the next
    refresh used to delete the correction and put the misspelling back -- a
    fix that works the day it is made and quietly expires.
    """
    from rip.ingest import ingest_profile

    ingest_profile(session, _typed("Reinforcment Learning"))
    session.commit()
    first = session.query(Evidence).filter(
        Evidence.attribute_type == "research_interest").one()
    before = first.id

    ingest_profile(session, _typed("Reinforcment Learning"))   # ORCID, unchanged
    session.commit()
    rows = session.query(Evidence).filter(
        Evidence.attribute_type == "research_interest").all()
    assert [r.value for r in rows] == ["Reinforcement Learning"]
    assert rows[0].id == before      # not deleted and re-added, just kept


def test_only_known_misspellings_and_only_whole_words_are_touched():
    """The corpus cannot tell a typo from a plural: of 75 word pairs one edit
    apart in these keywords, 74 are plurals, spelling variants or different
    words (generics/genetics, material/maternal). So this is a named list."""
    from rip.ingest import correct_spelling

    assert correct_spelling("Reinforcment Learning")[0] == "Reinforcement Learning"
    assert correct_spelling("Deep reinforcment learning")[0] == "Deep reinforcement learning"
    assert correct_spelling("REINFORCMENT LEARNING")[0] == "REINFORCEMENT LEARNING"
    for left_alone in ("Reinforcement Learning in Robotics", "Techniques",
                       "reinforcmental", "Material Science", "Modelling"):
        assert correct_spelling(left_alone) == (left_alone, None)


def test_a_misspelling_inside_a_sentence_is_left_alone(session):
    """Only self-typed keyword attributes are repaired. A bio is prose, not a
    subject heading, and rewriting someone's sentences is not the job."""
    from rip.ingest import ingest_profile
    from rip.normalize import EvidenceItem
    from tests.test_resolution import make_profile

    said = "I work on reinforcment learning"
    ingest_profile(session, make_profile(
        external_id="bio", url="https://orcid.org/bio", raw={"id": "bio"},
        name="Prose Person", usernames=["orcid:bio"],
        evidence=[EvidenceItem(attribute_type="bio", value=said)]))
    session.commit()
    assert session.query(Evidence).filter(
        Evidence.attribute_type == "bio").one().value == said
def test_a_payload_describing_nobody_does_not_become_a_person(session):
    """Europe PMC answers an ORCID search it holds nothing for with an empty
    payload. assess() passes it -- "no name given" means there is nothing to
    JUDGE, not that somebody is there -- so it became a living person with a
    null name, invisible to every search because it held nothing to match.
    One reached the corpus through a topical ingest before this guard."""
    import pytest

    from rip.ingest import ingest_profile
    from rip.personhood import NotAPerson
    from tests.test_resolution import make_profile

    empty = make_profile(source="europepmc", source_type="scholarly",
                         external_id="0000-0001-5152-1242", name=None,
                         url="https://europepmc.org/x", usernames=[],
                         raw={"orcid": "0000-0001-5152-1242", "articles": []})
    with pytest.raises(NotAPerson):
        ingest_profile(session, empty)
    assert session.query(Person).count() == 0


def test_a_nameless_payload_that_carries_something_is_still_ingested(session):
    """The guard is about emptiness, not about the name. A source that states
    an interest but no name is a person whose name arrives from somewhere
    else, and refusing it would throw away what it did say."""
    from rip.ingest import ingest_profile
    from rip.normalize import EvidenceItem
    from tests.test_resolution import make_profile

    person = ingest_profile(session, make_profile(
        source="europepmc", source_type="scholarly", external_id="has-claims",
        name=None, url="https://europepmc.org/y", usernames=[],
        raw={"orcid": "x"},
        evidence=[EvidenceItem(attribute_type="research_interest", value="Migraine")]))
    session.commit()
    assert person.id
    assert session.query(Evidence).count() == 1
