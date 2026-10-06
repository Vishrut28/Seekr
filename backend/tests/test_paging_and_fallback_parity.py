"""A page past the last one, and the SQL path agreeing with the index."""

from rip.ingest import ingest_profile
from rip.nlq import count_matches, execute, execute_progressive, parse
from rip.normalize import EvidenceItem, OrgAffiliation
from tests.test_resolution import make_profile


def person(session, ext, name, topics=(), orgs=(), country=None):
    ingest_profile(session, make_profile(
        external_id=ext, url=f"https://github.com/{ext}", raw={"login": ext},
        name=name, usernames=[f"github:{ext}"], country=country,
        organizations=[OrgAffiliation(name=n, is_current=cur) for n, cur in orgs],
        evidence=[EvidenceItem(attribute_type="research_interest", value=t) for t in topics]))
    session.commit()


def test_a_page_past_the_end_is_not_a_failed_search(session):
    """Page two of eight Google DeepMind people dropped the employer, called
    it not found, and offered to go live for it."""
    from rip.api import nl_query

    for i in range(3):
        person(session, f"d{i}", f"Dee Mind{i}", ["Reinforcement Learning"],
               orgs=[("Google DeepMind", True)])
    person(session, "x", "Rex Elsewhere", ["Reinforcement Learning"])
    rows, _parsed, dropped = execute_progressive(session, _at(parse(session, "reinforcement learning at Google DeepMind"), 50))
    assert rows == [] and dropped == []
    resp = nl_query(q="reinforcement learning at Google DeepMind", limit=0, offset=50,
                    discover="false", db=session)
    assert resp["count"] == 0 and resp["total_matches"] == 3 and resp["not_found"] == []
    assert resp["empty_reason"]["past_the_end"] is True
    assert resp["discover_available"] is False


def _at(parsed, offset):
    parsed.offset = offset
    return parsed


def both_paths(session, monkeypatch, query):
    p = parse(session, query)
    indexed = (sorted(x.canonical_name for x in execute(session, p)), count_matches(session, p))
    monkeypatch.setattr("rip.nlq.si.is_ready", lambda s: False)
    p = parse(session, query)
    fallback = (sorted(x.canonical_name for x in execute(session, p)), count_matches(session, p))
    monkeypatch.undo()
    return indexed, fallback


def test_both_paths_find_a_name_with_a_middle_initial(session, monkeypatch):
    person(session, "a", "Karan Singh")
    person(session, "b", "Karan P. Singh")
    person(session, "c", "Kiran Singh")
    indexed, fallback = both_paths(session, monkeypatch, "karan singh")
    assert indexed == fallback == (["Karan P. Singh", "Karan Singh"], 2)


def test_both_paths_place_people_by_where_they_work_now(session, monkeypatch):
    """An IIT Delhi alumnus now at a Norwegian university is not in India."""
    person(session, "n", "Vee Larsen", ["Machine Learning"],
           orgs=[("Indian Institute of Technology Delhi", False), ("UiT The Arctic University of Norway", True)])
    person(session, "i", "Ira Kumar", ["Machine Learning"],
           orgs=[("Indian Institute of Technology Delhi", True)])
    indexed, fallback = both_paths(session, monkeypatch, "machine learning researchers in India")
    assert indexed == fallback == (["Ira Kumar"], 1)


def test_related_subjects_can_be_switched_off(session):
    from rip.api import nl_query

    person(session, "n", "Nell Stated", ["Natural Language Processing"])
    person(session, "t", "Tom Topics", ["Topic Modeling"])
    on = nl_query(q="NLP", limit=0, offset=0, discover="false", db=session)
    off = nl_query(q="NLP", limit=0, offset=0, discover="false", related=False, db=session)
    assert {r["canonical_name"] for r in on["results"]} == {"Nell Stated", "Tom Topics"}
    assert [r["canonical_name"] for r in off["results"]] == ["Nell Stated"]
    assert on["related"] is True and off["related"] is False


def test_each_row_says_why_it_answers(session):
    from rip.api import nl_query

    person(session, "a", "Ada Deep", ["Natural Language Processing"],
           orgs=[("Google DeepMind", True)])
    person(session, "t", "Tom Topics", ["Topic Modeling"], orgs=[("Google DeepMind", True)])
    rows = {r["canonical_name"]: r["why"] for r in nl_query(
        q="NLP researchers at Google DeepMind", limit=0, offset=0, discover="false",
        db=session)["results"]}
    assert rows["Ada Deep"] == ["Natural Language Processing", "at Google DeepMind"]
    assert rows["Tom Topics"] == ["related: Topic Modeling", "at Google DeepMind"]
