"""A country in the filter menu has to count the people picking it returns.

The menu is there so nobody has to guess at the vocabulary, and each entry
carries a people-count. That count was taken from the stated country column
while the filter ALSO accepts a location that names the country — sources
routinely give "Bangalore, India" and no ISO code at all. The menu offered
"IN - 48" over a filter that returned 70.
"""

from rip import api
from rip import search_index as si
from rip.ingest import ingest_profile
from tests.test_resolution import make_profile


def corpus(session):
    # a stated code, and two people known only by where they say they are
    ingest_profile(session, make_profile(
        external_id="a", url="https://github.com/a", raw={"login": "a"},
        name="Coded Person", usernames=["github:a"], country="IN"))
    for tag, place in (("b", "Bangalore, India"), ("c", "Mumbai, India")):
        ingest_profile(session, make_profile(
            external_id=tag, url=f"https://github.com/{tag}", raw={"login": tag},
            name=f"Placed Person {tag}", usernames=[f"github:{tag}"], location=place))
    session.commit()
    si.rebuild(session)
    session.commit()


def counts(session):
    values = api._facets_uncached("country", 40, session)["values"]
    return {v["value"]: v["people"] for v in values}


def filtered(session, code):
    return api.list_persons(
        **{**api._ALL_FILTERS_NONE, "country": code, "limit": 1, "db": session}
    )["total_matches"]


def test_the_menu_counts_what_picking_it_returns(session):
    corpus(session)
    assert filtered(session, "IN") == 3           # one stated, two by location
    assert counts(session).get("IN") == 3


def test_every_country_offered_agrees_with_its_filter(session):
    corpus(session)
    for code, people in counts(session).items():
        assert filtered(session, code) == people, code
