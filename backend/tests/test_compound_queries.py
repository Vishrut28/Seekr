"""Questions with more than one part: all of several employers, exclusions,
and output thresholds."""

import pytest

from rip.ingest import ingest_profile
from rip.nlq import count_matches, execute_progressive, parse, subjects_asked
from rip.normalize import EvidenceItem, OrgAffiliation, PublicationData
from tests.test_resolution import make_profile


def person(session, ext, name, topics=(), orgs=(), location=None, source="github",
           raw=None, papers=()):
    ingest_profile(session, make_profile(
        source=source, external_id=ext, url=f"https://example.org/{source}/{ext}",
        raw=raw if raw is not None else {"login": ext},
        name=name, usernames=[f"{source}:{ext}"], location=location,
        organizations=[OrgAffiliation(name=o) for o in orgs],
        evidence=[EvidenceItem(attribute_type="research_interest", value=t) for t in topics],
        publications=[PublicationData(title=f"{name} paper {i}", external_id=f"{ext}-{i}",
                                      citations=c) for i, c in enumerate(papers)]))


def names(session, query):
    rows, _, _ = execute_progressive(session, parse(session, query))
    return sorted(p.canonical_name for p in rows if not getattr(p, "partial_match", None))


@pytest.fixture
def legacy(monkeypatch):
    """The SQL path used before the search index is built."""
    monkeypatch.setattr("rip.nlq.si.is_ready", lambda s: False)


# --- both ---------------------------------------------------------------------

def _employers(session):
    person(session, "a", "Ada Both", topics=["Robotics"], orgs=["Google", "Microsoft"])
    person(session, "b", "Bob Google", topics=["Robotics"], orgs=["Google"])
    person(session, "c", "Cy Microsoft", topics=["Robotics"], orgs=["Microsoft"])


def test_both_requires_every_organization(session):
    _employers(session)
    parsed = parse(session, "robotics people who worked at both Google and Microsoft")
    assert parsed.require_all_orgs
    assert names(session, "robotics people who worked at both Google and Microsoft") == ["Ada Both"]
    assert count_matches(session, parsed) == 1


def test_without_both_a_list_of_organizations_is_a_choice(session):
    _employers(session)
    parsed = parse(session, "robotics people at Google and Microsoft")
    assert not parsed.require_all_orgs
    assert names(session, "robotics people at Google and Microsoft") == [
        "Ada Both", "Bob Google", "Cy Microsoft"]


def test_both_on_the_sql_path(session, legacy):
    _employers(session)
    assert names(session, "robotics people who worked at both Google and Microsoft") == ["Ada Both"]


def test_relaxing_a_both_query_drops_one_organization_at_a_time(session):
    person(session, "b", "Bob Google", topics=["Robotics"], orgs=["Google"])
    person(session, "c", "Cy Microsoft", topics=["Robotics"], orgs=["Microsoft"])
    rows, _, _ = execute_progressive(
        session, parse(session, "robotics at both Google and Microsoft"))
    # nobody is at both: the last organization typed gives way first
    # ...and only it: Cy Microsoft is not at Google either
    assert [p.canonical_name for p in rows] == ["Bob Google"]
    assert rows[0].partial_match == {"missing": [{"term": "Microsoft", "as": "org"}]}


# --- negation -----------------------------------------------------------------

def test_not_at_an_organization_excludes_its_people(session):
    _employers(session)
    parsed = parse(session, "robotics researchers not at Google")
    assert parsed.organizations == []                  # not a positive filter
    assert [e["term"] for e in parsed.exclusions] == ["not at Google"]
    assert names(session, "robotics researchers not at Google") == ["Cy Microsoft"]
    assert count_matches(session, parsed) == 1


@pytest.mark.parametrize("query", [
    "robotics people who do not work at Google",
    "robotics researchers excluding Google",
    "robotics researchers, except Google",
    "robotics people who never worked at Google",
    "robotics researchers other than Google",
])
def test_ways_of_saying_not(session, query):
    _employers(session)
    assert names(session, query) == ["Cy Microsoft"], query


def test_excluding_a_subject_leaves_its_people_out(session):
    person(session, "a", "Ada Vision", topics=["Robotics", "Computer Vision"])
    person(session, "b", "Bob Control", topics=["Robotics", "Control Theory"])
    # near computer vision, not in it: excluding related subjects too would
    # leave out everyone who has ever touched an image
    person(session, "c", "Cy Images", topics=["Robotics", "Image Processing"])
    assert names(session, "robotics but not computer vision") == ["Bob Control", "Cy Images"]


def test_excluding_a_subject_known_only_by_its_related_topics(session):
    """No topic is called "deep learning" here; its neighbours are all it is."""
    person(session, "a", "Ada Nets", topics=["Robotics", "Neural Networks"])
    person(session, "b", "Bob Control", topics=["Robotics", "Control Theory"])
    assert names(session, "deep learning") == ["Ada Nets"]
    assert names(session, "robotics but not deep learning") == ["Bob Control"]


def test_not_just_leaves_no_stray_words(session):
    _employers(session)
    assert parse(session, "not just robotics researchers").unmatched_terms == []


def test_excluding_a_country(session):
    person(session, "a", "Ada Pune", topics=["Cosmology"], location="Pune, India")
    person(session, "b", "Bob Paris", topics=["Cosmology"], location="Paris, France")
    assert names(session, "cosmology researchers not in India") == ["Bob Paris"]


def test_negation_on_the_sql_path(session, legacy):
    _employers(session)
    assert names(session, "robotics researchers not at Google") == ["Cy Microsoft"]


@pytest.mark.parametrize("query", [
    "not just robotics researchers at Google",
    "not only robotics people at Google",
])
def test_not_just_widens_rather_than_excludes(session, query):
    _employers(session)
    parsed = parse(session, query)
    assert parsed.exclusions == []
    assert parsed.organizations                         # Google still asked for


def test_an_exclusion_that_matches_nothing_excludes_nobody_and_says_so(session):
    _employers(session)
    parsed = parse(session, "robotics researchers without a PhD")
    assert parsed.exclusions == [{"term": "without a PhD", "clauses": []}]
    assert len(names(session, "robotics researchers without a PhD")) == 3


def test_an_exclusion_never_gives_way_when_relaxing(session):
    """Relaxation drops constraints the user asked FOR; dropping an exclusion
    would return exactly the people the user ruled out."""
    person(session, "a", "Ada Google", topics=["Robotics"], orgs=["Google"],
           location="Pune, India")
    person(session, "b", "Bob Google", topics=["Robotics"], orgs=["Google"],
           location="Paris, France")
    rows, _, _ = execute_progressive(
        session, parse(session, "robotics researchers in India not at Google"))
    assert rows == []


def test_excluding_by_a_protected_attribute_is_not_applied(session):
    _employers(session)
    parsed = parse(session, "robotics researchers but not women")
    assert parsed.protected_terms
    assert all(not e["clauses"] for e in parsed.exclusions)


def test_the_api_reports_what_was_excluded(session):
    from rip import api

    _employers(session)
    body = api.nl_query(q="robotics researchers not at Google", limit=0, offset=0,
                        discover="false", db=session)
    assert body["exclusions"] == [{"term": "not at Google", "as": ["org"]}]
    assert [r["canonical_name"] for r in body["results"]] == ["Cy Microsoft"]


# --- counts -------------------------------------------------------------------

@pytest.mark.parametrize("query,pubs,cites", [
    ("robotics with at least 20 papers", 20, None),
    ("robotics researchers with 20+ publications", 20, None),
    ("robotics researchers with 20 or more papers", 20, None),
    ("robotics researchers with over 1,000 citations", None, 1001),
    ("robotics researchers cited more than 2k times", None, 2001),
    ("robotics researchers with more than 5 papers and at least 100 citations", 6, 100),
])
def test_thresholds_are_read(session, query, pubs, cites):
    parsed = parse(session, query)
    assert (parsed.min_publications, parsed.min_citations) == (pubs, cites)
    assert not any(t.isdigit() for t in parsed.unmatched_terms)


def test_a_bare_number_is_not_a_threshold(session):
    parsed = parse(session, "authors of 3 papers on robotics")
    assert (parsed.min_publications, parsed.min_citations) == (None, None)


def _output(session):
    # stored works only
    person(session, "a", "Ada Stored", topics=["Robotics"], papers=[400, 300, 200])
    person(session, "b", "Bob Small", topics=["Robotics"], papers=[5])
    # a source reporting totals far beyond the few works stored
    person(session, "C1", "Cy Reported", topics=["Robotics"], source="openalex",
           raw={"author": {"works_count": 146, "cited_by_count": 2357}}, papers=[10])


def test_publication_threshold_uses_the_larger_of_stored_and_reported(session):
    _output(session)
    assert names(session, "robotics researchers with at least 3 papers") == [
        "Ada Stored", "Cy Reported"]
    assert names(session, "robotics researchers with at least 100 papers") == ["Cy Reported"]


def test_citation_threshold(session):
    _output(session)
    assert names(session, "robotics researchers with over 800 citations") == [
        "Ada Stored", "Cy Reported"]
    assert names(session, "robotics researchers with over 900 citations") == ["Cy Reported"]


def test_a_threshold_alone_is_a_question(session):
    _output(session)
    parsed = parse(session, "people with at least 2000 citations")
    assert names(session, "people with at least 2000 citations") == ["Cy Reported"]
    assert count_matches(session, parsed) == 1


def test_thresholds_on_the_sql_path_use_stored_works(session, legacy):
    _output(session)
    assert names(session, "robotics researchers with at least 3 papers") == ["Ada Stored"]


def test_the_subject_asked_for_is_reported_even_when_it_resolves_to_neighbours(session):
    """"physicists" searches physics, whose evidence here is related subjects
    and free text only, so the applied_filters `skills` list was empty and the
    UI could show nothing at all for the subject."""
    from rip import api
    from rip.nlq import subjects_asked

    # nothing here is called physics: its evidence is a neighbouring subject
    person(session, "a", "Ada Quantum", topics=["Quantum Mechanics and Relativity"])
    parsed = parse(session, "physicists")
    assert parsed.skills == []                       # no exact topic value
    assert subjects_asked(parsed) == ["physics"]
    body = api.nl_query(q="physicists", limit=0, offset=0, discover="false", db=session)
    assert body["applied_filters"]["subjects"] == ["physics"]


def test_one_subject_is_reported_once_however_many_topics_it_matches(session):
    person(session, "a", "Ada ML", topics=["Machine Learning and Algorithms",
                                           "Automated Machine Learning"])
    parsed = parse(session, "machine learning")
    assert len(parsed.skills) > 1
    assert subjects_asked(parsed) == ["machine learning"]


def test_a_country_filter_reads_the_location_without_the_index(session, legacy):
    """Before the index is built, "in India" matched only a stated country
    field, so people whose location says Pune, India were missed — and an
    exclusion of the same country excluded nobody."""
    person(session, "a", "Ada Pune", topics=["Robotics"], location="Pune, India")
    person(session, "b", "Bob Paris", topics=["Robotics"], location="Paris, France")
    assert names(session, "robotics researchers in India") == ["Ada Pune"]
    assert names(session, "robotics researchers not in India") == ["Bob Paris"]
