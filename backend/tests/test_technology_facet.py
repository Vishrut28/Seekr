"""The `technology` facet: closes the gap where Filters.tsx's "Technology"
autocomplete suggested SKILL vocabulary (Evidence.value) while the actual
/v1/persons?technology= filter searches Project.technologies — a completely
different, unrelated vocabulary. A suggested value from the wrong facet
could return zero results, and the values that WOULD actually match were
never surfaced at all.
"""
from rip.api import facets
from rip.ingest import ingest_profile
from rip.normalize import ProjectData
from tests.test_resolution import make_profile


def _person_with_project(session, *, name, external_id, technologies):
    return ingest_profile(
        session,
        make_profile(
            name=name, external_id=external_id, url=f"https://github.com/{external_id}",
            raw={"login": external_id}, usernames=[f"github:{external_id}"],
            projects=[ProjectData(
                name=f"{external_id}-proj", url=f"https://github.com/{external_id}/proj",
                technologies=technologies,
            )],
        ),
    )


def test_technology_facet_lists_project_technologies_not_skills(session):
    """A skill value must never leak into the technology facet, and vice
    versa — they are deliberately separate vocabularies."""
    from rip.normalize import EvidenceItem

    ingest_profile(
        session,
        make_profile(
            name="Skill Only Person",
            evidence=[EvidenceItem(attribute_type="skill", value="Distributed Systems")],
        ),
    )
    _person_with_project(session, name="Tech Person", external_id="techperson",
                         technologies=["Rust", "Kubernetes"])

    result = facets(field="technology", limit=10, db=session)
    values = {v["value"] for v in result["values"]}
    assert values == {"Rust", "Kubernetes"}
    assert "Distributed Systems" not in values


def test_technology_facet_counts_distinct_people_not_raw_projects(session):
    """Two DIFFERENT people each with a project using "Rust" must count as
    2 — not 2 projects counted separately, and not double-counted if the
    same person contributes to more than one Rust project."""
    _person_with_project(session, name="Person One", external_id="p1",
                         technologies=["Rust"])
    _person_with_project(session, name="Person Two", external_id="p2",
                         technologies=["Rust"])

    result = facets(field="technology", limit=10, db=session)
    rust = next(v for v in result["values"] if v["value"] == "Rust")
    assert rust["people"] == 2

    # the SAME person contributing to a SECOND Rust project must not
    # inflate the count to 3 — it is still 2 distinct people
    from rip.ingest import ingest_profile as _ingest
    from rip.normalize import NormalizedProfile
    from rip.normalize import ProjectData as _PD

    # reuse p1's identity via a matching username so this attaches to the
    # SAME person rather than creating a third one
    _ingest(session, NormalizedProfile(
        source="github", source_type="code", external_id="p1-second-project",
        url="https://github.com/p1/second", raw={}, name="Person One",
        usernames=["github:p1"],
        projects=[_PD(name="second-proj", url="https://github.com/p1/second/proj",
                      technologies=["Rust"])],
    ))
    result = facets(field="technology", limit=10, db=session)
    rust = next(v for v in result["values"] if v["value"] == "Rust")
    assert rust["people"] == 2, "same person's second project must not double-count"


def test_technology_facet_matches_the_actual_person_filter(session):
    """The facet must suggest values that genuinely work when passed back
    into the real filter — the whole point of this fix."""
    from rip.api import list_persons

    _person_with_project(session, name="Filter Match Person", external_id="fm1",
                         technologies=["Kubernetes"])

    result = facets(field="technology", limit=10, db=session)
    assert any(v["value"] == "Kubernetes" for v in result["values"])

    base = {"q": None, "skill": None, "organization": None, "current_organization": None,
                "education": None, "role": None, "country": None, "location": None, "source": None,
                "technology": "Kubernetes", "min_publications": None, "min_citations": None,
                "min_sources": None, "active_since": None, "updated_since": None,
                "has_cv": None, "has_email": None, "sort": "relevance", "limit": 10, "offset": 0,
                "db": session}
    found = list_persons(**base)
    assert found["total_matches"] == 1
    assert found["results"][0]["canonical_name"] == "Filter Match Person"


def test_technology_facet_handles_empty_technologies_gracefully(session):
    """A project with no technologies listed at all must not crash the
    aggregation or produce a spurious empty-string facet value."""
    _person_with_project(session, name="No Tech Person", external_id="notech",
                         technologies=[])
    result = facets(field="technology", limit=10, db=session)
    assert result["values"] == []


def test_unknown_facet_field_still_lists_technology_in_the_error(session):
    from fastapi import HTTPException

    try:
        facets(field="nonsense", limit=10, db=session)
        raise AssertionError("should reject unknown facet")
    except HTTPException as exc:
        assert exc.status_code == 422
        assert "technology" in exc.detail
