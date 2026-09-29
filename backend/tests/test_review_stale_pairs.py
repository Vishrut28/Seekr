"""A queued pair names who these two WERE. Either may since have moved.

Merging folds one person into the other and leaves a tombstone pointing at
the survivor. A pair queued before that still names the tombstone, so the
review queue showed a record that is no longer its own person — an empty
profile beside a real one, under a Merge button that would have folded the
tombstone and left the real person untouched. Two candidate rows that end up
naming the same living pair also asked the same question twice.
"""


from rip.ingest import ingest_profile
from rip.models import MergeCandidate, Person
from rip.review import merge_persons, resolve_duplicate
from tests.test_resolution import make_profile

from rip import api


def person(session, tag, name):
    return ingest_profile(session, make_profile(
        external_id=tag, url=f"https://github.com/{tag}", raw={"login": tag},
        name=name, usernames=[f"github:{tag}"]))


def queue(session, a, b, ident=None):
    mc = MergeCandidate(person_id=a.id, candidate_person_id=b.id, score=90.0,
                        signals={"reason": "near-identical name"}, status="pending")
    session.add(mc)
    session.commit()
    return mc


def test_the_queue_names_who_each_side_is_now(session):
    a = person(session, "a", "Ann Example")
    b = person(session, "b", "Ann Example")
    c = person(session, "c", "Ann Example")
    queue(session, a, b)
    # b is folded into c after the pair was queued
    merge_persons(session, c.id, b.id)
    session.commit()

    rows = api.review_merges(db=session)["possible_duplicates"]
    assert len(rows) == 1
    row = rows[0]
    # the queue offers the living person, not the tombstone it was queued against
    assert row["duplicate_person_id"] == c.id
    assert row["person_id"] == a.id


def test_a_pair_that_became_one_person_leaves_the_queue(session):
    a = person(session, "a", "Ann Example")
    b = person(session, "b", "Ann Example")
    queue(session, a, b)
    merge_persons(session, a.id, b.id)      # answered by hand elsewhere
    session.commit()

    assert api.review_merges(db=session)["possible_duplicates"] == []


def test_the_same_living_pair_is_asked_once(session):
    a = person(session, "a", "Ann Example")
    b = person(session, "b", "Ann Example")
    c = person(session, "c", "Ann Example")
    queue(session, a, b)
    queue(session, a, c)
    merge_persons(session, c.id, b.id)      # b and c are now one person
    session.commit()

    rows = api.review_merges(db=session)["possible_duplicates"]
    assert len(rows) == 1, [r["candidate_id"] for r in rows]
    assert rows[0]["duplicate_person_id"] == c.id


def test_merging_a_stale_pair_merges_the_living_people(session):
    a = person(session, "a", "Ann Example")
    b = person(session, "b", "Ann Example")
    c = person(session, "c", "Ann Example")
    mc = queue(session, a, b)
    merge_persons(session, c.id, b.id)
    session.commit()

    out = resolve_duplicate(session, mc.id, "merge")
    # c is the person b became, and c is what actually got merged with a
    assert out["kept_person_id"] in {a.id, c.id}
    living = {p.id for p in session.query(Person).filter(Person.merged_into.is_(None))}
    assert len(living & {a.id, c.id}) == 1, "the two living people are now one"


def test_merging_a_pair_that_is_already_one_person_is_a_no_op(session):
    """Merging by hand closes the row for that exact pair, so reaching this
    guard means the two became one person some other way — through a chain of
    merges that never touched this row. Put the row back to pending to stand
    in for that, since the shape is what matters: both sides now resolve to
    the same person, and there is nothing left to merge."""
    a = person(session, "a", "Ann Example")
    b = person(session, "b", "Ann Example")
    mc = queue(session, a, b)
    merge_persons(session, a.id, b.id)
    mc.status = "pending"
    session.commit()

    out = resolve_duplicate(session, mc.id, "merge")
    assert out["already_one_person"] is True
    assert mc.status == "merged"
    # and it did not invent a second merge on top of the first
    assert session.get(Person, b.id).merged_into == a.id
