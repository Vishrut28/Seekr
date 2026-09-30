"""When a query matches too few people: the subject is kept, place and
employer give way, and every partial result says what it is missing."""

from rip.ingest import ingest_profile
from rip.nlq import execute_progressive, parse
from rip.normalize import EvidenceItem, OrgAffiliation
from tests.test_resolution import make_profile


def person(session, ext, name, topics=(), orgs=(), location=None):
    ingest_profile(session, make_profile(
        external_id=ext, url=f"https://github.com/{ext}", raw={"login": ext},
        name=name, usernames=[f"github:{ext}"], location=location,
        organizations=[OrgAffiliation(name=o) for o in orgs],
        evidence=[EvidenceItem(attribute_type="research_interest", value=t) for t in topics]))


def run(session, query):
    rows, applied, dropped = execute_progressive(session, parse(session, query))
    return [(p.canonical_name, getattr(p, "partial_match", None)) for p in rows], dropped


def test_the_subject_is_kept_and_the_employer_gives_way(session):
    """Typed first or last, "at Oxford" is dropped before "deep learning"."""
    person(session, "a", "Ada Deep", topics=["Deep Learning"])
    person(session, "b", "Bob Oxford", topics=["Medieval History"], orgs=["University of Oxford"])
    # "at" makes Oxford an employer; typed bare it is the place, which also
    # covers the university (a location matches affiliation names), and a
    # place gives way before the subject just as an employer does
    for query, reading in (("deep learning researchers at Oxford", "org"),
                           ("Oxford deep learning researchers", "location")):
        rows, dropped = run(session, query)
        assert [name for name, _ in rows] == ["Ada Deep"], query
        assert rows[0][1] == {"missing": [{"term": "Oxford", "as": reading}]}
        assert dropped == [{"term": "Oxford", "as": reading}]


def test_a_thin_page_is_topped_up_behind_the_full_matches(session):
    """"drug discovery researchers in India" found two people and stopped."""
    person(session, "a", "Ann India", topics=["Drug Discovery"], location="Pune, India")
    person(session, "b", "Bea Elsewhere", topics=["Drug Discovery"], location="Basel, Switzerland")
    person(session, "c", "Cy Unrelated", topics=["Wildlife"], location="Delhi, India")
    rows, dropped = run(session, "drug discovery researchers in India")
    assert rows[0] == ("Ann India", None)                    # full match first
    assert rows[1][0] == "Bea Elsewhere"
    assert rows[1][1] == {"missing": [{"term": "India", "as": "country"}]}
    assert "Cy Unrelated" not in [name for name, _ in rows]
    assert dropped == []                                     # the query itself was met


def test_a_country_named_by_a_demonym_is_a_constraint_that_can_give_way(session):
    """"Indian" filtered on India but was no clause, so relaxing lost it silently."""
    person(session, "a", "Ada Ai", topics=["Artificial Intelligence"], location="Berlin, Germany")
    parsed = parse(session, "Indian AI researchers")
    assert ("Indian", "country") in [(c["token"], c["label"]) for c in parsed.clause_order]
    rows, dropped = run(session, "Indian AI researchers")
    assert [name for name, _ in rows] == ["Ada Ai"]
    assert dropped == [{"term": "Indian", "as": "country"}]


def test_a_name_search_is_not_topped_up_with_other_names(session):
    person(session, "a", "Dhruv Dixit")
    person(session, "b", "Dhruv Kumar")
    rows, _ = run(session, "Dhruv Dixit")
    assert rows == [("Dhruv Dixit", None)]


def test_enough_full_matches_are_never_diluted(session):
    for i in range(10):
        person(session, f"in{i}", f"Indian Researcher{i}", topics=["Cosmology"], location="Pune, India")
    person(session, "far", "Far Away", topics=["Cosmology"], location="Paris, France")
    rows, _ = run(session, "cosmology researchers in India")
    assert "Far Away" not in [name for name, _ in rows]
    assert all(flag is None for _, flag in rows)


def test_the_api_labels_partial_results(session, monkeypatch):
    from rip import api

    person(session, "a", "Ada Deep", topics=["Deep Learning"])
    person(session, "b", "Bob Oxford", topics=["Medieval History"], orgs=["University of Oxford"])
    body = api.nl_query(q="deep learning researchers at Oxford", limit=0, offset=0,
                        discover="false", db=session)
    assert body["results"][0]["match"] == "partial"
    assert body["results"][0]["missing"] == [{"term": "Oxford", "as": "org"}]


def clause(token, at):
    return {"kind": "skill_groups", "payload": {"term": token}, "token": token,
            "label": "skill", "at": at}


def test_a_modifier_is_dropped_before_the_subject_it_modifies():
    """"computational pathology" is not one stored topic, so it parses as two
    subjects side by side. Dropping the last one typed kept computational
    mechanics and threw pathology away — the wrong half of the question."""
    from rip.nlq import _drop_sequence

    order = _drop_sequence([clause("computational", 0), clause("pathology", 1)])
    assert [c["token"] for c in order] == ["computational", "pathology"]


def test_subjects_that_are_not_side_by_side_are_dropped_last_typed_first():
    """"robotics and computer vision" are two questions, not a modifier and a
    head, and the last one typed is the one that gives way."""
    from rip.nlq import _drop_sequence

    order = _drop_sequence([clause("robotics", 0), clause("computer vision", 2)])
    assert [c["token"] for c in order] == ["computer vision", "robotics"]


def test_subjects_that_are_not_one_phrase_still_give_way_last_first(session):
    """"robotics and computer vision" are two questions, not a modifier and a
    head: the one typed last is the one dropped."""
    person(session, "a", "Ann Robot", topics=["Robotics and Sensor-Based Localization"])
    person(session, "b", "Bob Vision", topics=["Computer Vision and Pattern Recognition"])
    rows, dropped = run(session, "robotics and computer vision")
    assert [name for name, _ in rows] == ["Ann Robot"]
    assert dropped == [{"term": "computer vision", "as": "skill"}]
