"""Shortlists: the one collection a person builds by hand.

A list could be created and filled but never dropped, so a name typed wrong
stayed on the page for good. These cover the whole round trip, and pin down
what deleting a list is allowed to take with it — the membership rows, and
nothing else.
"""

import pytest

from rip import api
from rip.ingest import ingest_profile
from rip.models import Person, Shortlist, ShortlistMember
from tests.test_resolution import make_profile


def person(session, tag="a"):
    p = ingest_profile(session, make_profile(
        external_id=tag, url=f"https://github.com/{tag}", raw={"login": tag},
        name=f"Listed Person {tag}", usernames=[f"github:{tag}"]))
    session.commit()
    return p


def test_a_person_can_be_put_on_a_list_once(session):
    who = person(session)
    made = api.create_shortlist({"name": "Interviews"}, db=session)
    assert made["created"] is True

    first = api.add_to_shortlist(made["id"], {"person_id": who.id, "query": "ml"}, db=session)
    again = api.add_to_shortlist(made["id"], {"person_id": who.id, "query": "ml"}, db=session)
    assert first["added"] is True and again["added"] is False

    detail = api.get_shortlist(made["id"], db=session)
    assert [m["person_id"] for m in detail["members"]] == [who.id]


def test_naming_a_list_that_exists_returns_it_rather_than_a_second_one(session):
    first = api.create_shortlist({"name": "Interviews"}, db=session)
    second = api.create_shortlist({"name": "Interviews"}, db=session)
    assert second["id"] == first["id"] and second["created"] is False


def test_deleting_a_list_keeps_everyone_who_was_on_it(session):
    who = person(session)
    made = api.create_shortlist({"name": "Mistyped"}, db=session)
    api.add_to_shortlist(made["id"], {"person_id": who.id, "query": "ml"}, db=session)

    out = api.delete_shortlist(made["id"], db=session)
    assert out["deleted"] is True and out["removed_members"] == 1

    assert session.get(Shortlist, made["id"]) is None
    assert session.query(ShortlistMember).filter(
        ShortlistMember.shortlist_id == made["id"]).count() == 0
    # the list was a note about the person, never the person
    assert session.get(Person, who.id) is not None
    assert api.list_shortlists(owner="anonymous", db=session)["shortlists"] == []


def test_deleting_a_list_that_is_not_there_is_a_404(session):
    with pytest.raises(Exception) as caught:
        api.delete_shortlist(9999, db=session)
    assert "not found" in str(caught.value)


def test_taking_someone_off_a_list_twice_is_a_404(session):
    who = person(session)
    made = api.create_shortlist({"name": "Interviews"}, db=session)
    api.add_to_shortlist(made["id"], {"person_id": who.id, "query": "ml"}, db=session)
    api.remove_from_shortlist(made["id"], who.id, db=session)
    with pytest.raises(Exception) as caught:
        api.remove_from_shortlist(made["id"], who.id, db=session)
    assert "not on this shortlist" in str(caught.value)
