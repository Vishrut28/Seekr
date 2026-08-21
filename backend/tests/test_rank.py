from rip.api import list_persons
from rip.ingest import ingest_profile
from rip.nlq import execute, parse
from rip.normalize import EvidenceItem, OrgAffiliation
from tests.test_resolution import make_profile


def _seed_python_pair(session):
    """Two Python matches: Ada is ingested first (would win insertion order)."""
    ingest_profile(
        session,
        make_profile(
            name="Ada Thin",
            evidence=[EvidenceItem(attribute_type="skill", value="Python")],
        ),
    )
    ingest_profile(
        session,
        make_profile(
            name="Lin Rich",
            external_id="lin",
            url="https://github.com/lin",
            raw={"login": "lin"},
            usernames=["github:lin"],
            orcid="0000-0002-1111-2222",
            location="Bengaluru, India",
            organizations=[OrgAffiliation(name="Acme Labs", role="Staff Engineer", is_current=True)],
            evidence=[EvidenceItem(attribute_type="skill", value="Python", confidence=0.85)],
        ),
    )
    ingest_profile(
        session,
        make_profile(
            source="openalex",
            source_type="scholarly",
            external_id="A-LIN",
            url="https://openalex.org/A-LIN",
            raw={"id": "A-LIN"},
            name="L. Rich",
            usernames=[],
            orcid="0000-0002-1111-2222",
            evidence=[EvidenceItem(attribute_type="skill", value="Python", confidence=0.8)],
        ),
    )


def _call_persons(session, **kw):
    base = dict(
        q=None, skill=None, organization=None, current_organization=None,
        education=None, role=None, country=None, location=None, source=None,
        technology=None, min_publications=None, min_citations=None,
        min_sources=None, active_since=None, updated_since=None,
        has_cv=None, has_email=None, sort="relevance", limit=50, offset=0,
        db=session,
    )
    base.update(kw)
    return list_persons(**base)


def test_corroborated_profile_outranks_insertion_order(session):
    _seed_python_pair(session)
    names = [p.canonical_name for p in execute(session, parse(session, "python"))]
    assert names[0] == "Lin Rich"
    assert "Ada Thin" in names


def test_persons_relevance_explains_the_order(session):
    _seed_python_pair(session)
    data = _call_persons(session, skill="Python")
    assert [r["canonical_name"] for r in data["results"]][0] == "Lin Rich"
    top = data["results"][0]
    assert top["match_score"] > data["results"][-1]["match_score"]
    joined = " ".join(top["match_reasons"]).lower()
    assert "2 sources" in joined or "corroborated" in joined


def test_name_sort_is_alphabetical_not_score(session):
    _seed_python_pair(session)
    data = _call_persons(session, skill="Python", sort="name")
    names = [r["canonical_name"] for r in data["results"]]
    assert names == sorted(names)
    assert "match_score" not in data["results"][0]
