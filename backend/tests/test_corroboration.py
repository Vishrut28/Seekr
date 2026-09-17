"""Corroboration should count DISTINCT SOURCES agreeing, not corroborated
evidence ROWS — matching WEIGHTS' own stated intent ("independent sources
agreeing"), which the row-counting implementation did not actually measure:
three rows from the same source landing in "corroborated" state is one
source seen three times, not three independent agreements.

Evidence rows are constructed directly via the ORM rather than through the
full ingest/resolution pipeline: relevance_scores() only cares what rows
exist in the DB, not how they got there, and going through real
cross-source identity resolution would test resolution.py's matching
heuristics (a separate, already-tested concern) instead of isolating the
aggregation logic this change actually touches.
"""
from rip.ingest import ingest_profile
from rip.models import Evidence
from rip.nlq import execute, parse, relevance_scores
from tests.test_resolution import make_profile


def _add_evidence_row(session, person_id, *, source, value="Kubernetes",
                       attribute_type="skill", state="corroborated", confidence=0.7):
    ev = Evidence(
        person_id=person_id, attribute_type=attribute_type, value=value,
        source=source, confidence=confidence, verification_state=state,
    )
    session.add(ev)
    session.flush()
    return ev


def test_three_rows_one_source_scores_as_one_corroborating_source(session):
    """The exact case this fix targets: the SAME source appearing three
    times must score identically to that source appearing just once —
    proving the aggregation counts DISTINCT sources, not corroborated rows."""
    person = ingest_profile(session, make_profile(name="Repeated Source Person"))
    for _ in range(3):
        _add_evidence_row(session, person.id, source="github")
    session.commit()
    three_rows = relevance_scores(
        session, parse(session, "kubernetes"), [person.id]
    )[person.id]["components"]["corroboration"]

    other = ingest_profile(
        session,
        make_profile(external_id="one", url="https://github.com/one",
                     raw={"login": "one"}, name="One Row Person",
                     usernames=["github:one"]),
    )
    _add_evidence_row(session, other.id, source="github")
    session.commit()
    one_row = relevance_scores(
        session, parse(session, "kubernetes"), [other.id]
    )[other.id]["components"]["corroboration"]

    assert three_rows == one_row


def test_distinct_sources_score_higher_than_repeated_single_source(session):
    """Two people, both with 3 'corroborated' evidence rows for the SAME
    query term — one from 3 distinct sources, one from 1 source repeated 3
    times. The distinct-source person must score higher corroboration."""
    single_source = ingest_profile(session, make_profile(name="Single Source Person"))
    for _ in range(3):
        _add_evidence_row(session, single_source.id, source="github")

    multi_source = ingest_profile(
        session,
        make_profile(external_id="multi", url="https://github.com/multi",
                     raw={"login": "multi"}, name="Multi Source Person",
                     usernames=["github:multi"]),
    )
    for src in ("github", "openalex", "dblp"):
        _add_evidence_row(session, multi_source.id, source=src)
    session.commit()

    scored = relevance_scores(
        session, parse(session, "kubernetes"), [single_source.id, multi_source.id]
    )
    single_corr = scored[single_source.id]["components"]["corroboration"]
    multi_corr = scored[multi_source.id]["components"]["corroboration"]
    assert multi_corr > single_corr


def test_uncorroborated_evidence_does_not_count(session):
    """A row that was never corroborated (verification_state='unverified')
    must contribute nothing, regardless of how many distinct sources have
    UNCORROBORATED claims — corroboration specifically means agreement, not
    just "known from N sources" (that is what breadth already measures)."""
    person = ingest_profile(session, make_profile(name="No Corroboration Person"))
    for src in ("github", "openalex", "dblp"):
        _add_evidence_row(session, person.id, source=src, state="unverified")
    session.commit()

    scored = relevance_scores(session, parse(session, "kubernetes"), [person.id])
    assert scored[person.id]["components"]["corroboration"] == 0.0


def test_end_to_end_ranking_prefers_genuinely_corroborated_person(session):
    """Full execute() path, not just relevance_scores() in isolation: a
    person corroborated by 3 distinct sources should rank above an
    otherwise-identical person whose 3 corroborated rows are all the same
    source, when that is the only thing distinguishing them."""
    repeated = ingest_profile(
        session,
        make_profile(name="Repeated Source", usernames=["github:repeated"]),
    )
    for _ in range(3):
        _add_evidence_row(session, repeated.id, source="github", confidence=0.7)

    distinct = ingest_profile(
        session,
        make_profile(external_id="distinct", url="https://github.com/distinct",
                     raw={"login": "distinct"}, name="Distinct Sources",
                     usernames=["github:distinct"]),
    )
    for src in ("github", "openalex", "dblp"):
        _add_evidence_row(session, distinct.id, source=src, confidence=0.7)
    session.commit()

    rows = execute(session, parse(session, "kubernetes"))
    names = [p.canonical_name for p in rows]
    assert names.index("Distinct Sources") < names.index("Repeated Source")
