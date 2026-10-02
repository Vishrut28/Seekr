"""Placing people nothing else places, and a merge that lost a stated country.

A researcher at BITS Hyderabad fell out of "machine learning researchers in
India": his OpenAlex record says India, but he was merged into his own
country-less record and merge_persons did not carry country over.
"""

import pytest

from rip import search_index as si
from rip.geo import countries_named, country_of_employers
from rip.ingest import ingest_profile
from rip.models import ChangeLog, Person
from rip.nlq import execute, parse
from rip.normalize import EvidenceItem, OrgAffiliation
from rip.review import merge_persons
from scripts.backfill_merged_fields import fill, missing
from tests.test_resolution import make_profile


def test_a_name_places_itself_by_a_country_part_or_a_city():
    assert countries_named("IBM (India)") == {"IN"}
    assert countries_named("Indian Institute of Technology Kanpur") == {"IN"}
    assert countries_named("Swedish Nuclear Fuel and Waste Management (Sweden)") == {"SE"}
    assert countries_named("Indian Institute of Science") == set()     # a demonym is not a place
    assert countries_named("Birla Institute of Technology and Science - Hyderabad Campus") == set()


def test_a_name_naming_two_countries_places_nobody():
    assert countries_named("New York University Abu Dhabi") == {"US", "AE"}
    assert country_of_employers(["New York University Abu Dhabi"], []) is None


def test_current_workplaces_decide_even_when_none_can_be_placed():
    assert country_of_employers(["Red Hat (United States)"], ["IBM (India)"]) == "US"
    # Lund is not in the gazetteer; a past job at the LSE must not stand in for it
    assert country_of_employers(["Lund University"],
                                ["London School of Economics and Political Science"]) is None


def test_without_a_current_workplace_past_ones_must_agree():
    assert country_of_employers([], ["Teach For India", "Govt. of NCT of Delhi"]) == "IN"
    assert country_of_employers([], ["IBM (India)", "Red Hat (United States)"]) is None
    assert country_of_employers([], []) is None


def person(session, ext, name, orgs=(), country=None, location=None):
    p = ingest_profile(session, make_profile(
        source="openalex", source_type="scholarly", external_id=ext,
        url=f"https://openalex.org/{ext}", raw={"id": ext}, name=name, usernames=[],
        country=country, location=location,
        evidence=[EvidenceItem(attribute_type="research_interest", value="Machine Learning")],
        organizations=[OrgAffiliation(name=o, relation=rel, is_current=cur)
                       for o, rel, cur in orgs]))
    session.commit()
    return p


def countries(session, p):
    return {t.term for t in session.query(si.SearchTerm).filter_by(person_id=p.id, field="c")}


def test_the_index_places_someone_by_where_they_work(session):
    kanpur = person(session, "k", "Kay Kanpur",
                    orgs=[("Indian Institute of Technology Kanpur", "worked_at", True)])
    assert countries(session, kanpur) == {"in"}
    assert "Kay Kanpur" in [p.canonical_name for p in execute(
        session, parse(session, "machine learning researchers in India"))]


def test_where_someone_studied_does_not_place_them(session):
    alum = person(session, "s", "Sam Alum",
                  orgs=[("Indian Institute of Technology Kanpur", "studied_at", False)])
    assert countries(session, alum) == set()


@pytest.mark.parametrize("stated", [{"country": "US"}, {"location": "Boston, USA"}])
def test_a_stated_country_or_location_always_wins(session, stated):
    p = person(session, "b", "Bo Boston",
               orgs=[("Indian Institute of Technology Kanpur", "worked_at", True)], **stated)
    assert countries(session, p) == {"us"}


def test_a_merge_keeps_the_country_a_source_stated(session):
    keep = person(session, "a", "Dee Dixit")
    gone = person(session, "b", "Dee Dixit", country="IN")
    merge_persons(session, keep.id, gone.id)
    assert session.get(Person, keep.id).country == "IN"
    assert countries(session, keep) == {"in"}


def test_the_backfill_repairs_a_merge_made_before_the_fix(session):
    keep = person(session, "a", "Dee Dixit")
    gone = person(session, "b", "Dee Dixit", country="IN")
    merge_persons(session, keep.id, gone.id)
    keep = session.get(Person, keep.id)
    keep.country = None                       # as a merge before the fix left it
    session.commit()
    assert missing(session) == [(keep.id, "country", "IN", gone.id)]
    assert fill(session, missing(session)) == 1
    assert session.get(Person, keep.id).country == "IN"
    assert session.query(ChangeLog).filter_by(person_id=keep.id, field="merge_backfill").count() == 1
    assert missing(session) == []


def test_the_backfill_never_replaces_a_value_the_keeper_has(session):
    keep = person(session, "a", "Dee Dixit", country="US")
    gone = person(session, "b", "Dee Dixit", country="IN")
    merge_persons(session, keep.id, gone.id)
    assert missing(session) == []
    assert session.get(Person, keep.id).country == "US"
