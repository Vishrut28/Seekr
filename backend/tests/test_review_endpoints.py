"""The five HTTP endpoints that change the graph from the Review page.

rip/review.py is well covered; the handlers in front of it were not. Twenty
route handlers in api.py had never been executed by a test, and five of them
mutate: approving a merge, splitting one apart, and merging, rejecting or
deferring a duplicate pair. They are the buttons a reviewer presses, one pair
at a time, 164 times.

What is untested here is not the decision logic — review.py owns that — but
everything between the button and it: whether the id in the URL reaches the
right record, whether an id that does not exist gives a 404 rather than a
500, whether the body is read at all, and whether what comes back says what
happened. A reviewer who presses "Different people" and is told nothing has
no way to know the click landed.

The pair in the live queue that prompted these is the one whose deferral note
runs to a paragraph: deferring has to carry a REASON, because the row is
meant to be read again later by someone deciding whether anything has
changed. An endpoint that dropped the note would lose that silently, and the
queue would fill with "nothing reachable settles it" on pairs where somebody
had written out exactly what they looked for.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rip import api
from rip.db import Base
from rip.ingest import ingest_profile
from rip.models import IdentityLink, MergeCandidate, Person
from rip.normalize import OrgAffiliation
from tests.test_resolution import make_profile


@pytest.fixture()
def session():
    """One in-memory database shared with the client's request thread."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def client(session):
    api.app.dependency_overrides[api.get_db] = lambda: session
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def merged_pair(session):
    """Two profiles the resolver folded together on a fuzzy name+org match,
    which is the kind it files for review."""
    first = ingest_profile(session, make_profile(
        organizations=[OrgAffiliation(name="Acme Labs", is_current=True)]))
    second = ingest_profile(session, make_profile(
        source="dblp", external_id="9/999", url="https://dblp.org/pid/9/999",
        raw={"name": "John Smith"}, name="John Smith", usernames=[],
        organizations=[OrgAffiliation(name="Acme Labs")]))
    assert first.id == second.id, "fixture no longer produces a merge to review"
    session.commit()
    return first


def two_people(session):
    a = ingest_profile(session, make_profile(
        external_id="a1", url="https://github.com/a1", raw={"login": "a1"},
        name="Asha Rao", usernames=["github:a1"]))
    b = ingest_profile(session, make_profile(
        source="dblp", external_id="b1", url="https://dblp.org/pid/b1",
        raw={"name": "Asha Rao"}, name="Asha Rao", usernames=[]))
    session.commit()
    return a, b


def queued(session, a, b):
    """The pending candidate for this pair.

    Ingest files one itself when it resolves two records to different people
    with near-miss names, so this reuses what the pipeline produced rather
    than inserting a second row -- which the unique constraint on the pair
    refuses anyway, and which would be a fixture the product never makes."""
    existing = session.query(MergeCandidate).filter_by(status="pending").first()
    if existing is not None:
        return existing
    candidate = MergeCandidate(person_id=a.id, candidate_person_id=b.id,
                               score=0.8, signals={}, status="pending")
    session.add(candidate)
    session.commit()
    return candidate


def pending_link(session):
    """The fuzzy merge review.list_suspicious would show. `new` links are
    not decisions anybody made, so they are not what gets approved."""
    return session.query(IdentityLink).filter(
        IdentityLink.match_method.like("fuzzy%")).first()


# --------------------------------------------------------------------------
# merges: approve and split
# --------------------------------------------------------------------------

def test_approving_a_merge_records_the_decision(client, session):
    person = merged_pair(session)
    link = pending_link(session)
    assert link is not None, "nothing queued to approve"

    response = client.post(f"/v1/review/merges/{link.id}/approve")
    assert response.status_code == 200, response.text
    assert response.json() == {"link_id": link.id, "review_state": "approved"}

    session.expire_all()
    assert session.get(IdentityLink, link.id).review_state == "approved"
    assert session.get(Person, person.id) is not None, "approving deleted somebody"


def test_splitting_a_merge_gives_back_a_person(client, session):
    merged_pair(session)
    link = pending_link(session)
    before = session.query(Person).filter(Person.merged_into.is_(None)).count()

    response = client.post(f"/v1/review/merges/{link.id}/split")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["new_person_id"]
    assert body["canonical_name"]

    session.expire_all()
    after = session.query(Person).filter(Person.merged_into.is_(None)).count()
    assert after == before + 1, "split did not produce a second person"
    assert session.get(Person, body["new_person_id"]) is not None


@pytest.mark.parametrize("action", ["approve", "split"])
def test_a_link_that_does_not_exist_is_a_404(client, session, action):
    """Not a 500. The reviewer is clicking through a list that may be stale."""
    assert client.post(f"/v1/review/merges/999999/{action}").status_code == 404


# --------------------------------------------------------------------------
# duplicates: merge, reject, defer
# --------------------------------------------------------------------------

def test_merging_a_duplicate_pair_leaves_one_person(client, session):
    a, b = two_people(session)
    candidate = queued(session, a, b)

    response = client.post(f"/v1/review/duplicates/{candidate.id}/merge")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "merged"
    assert body["kept_person_id"] in {a.id, b.id}

    session.expire_all()
    living = session.query(Person).filter(Person.merged_into.is_(None)).count()
    assert living == 1, "both records still standing after a merge"
    assert session.get(MergeCandidate, candidate.id).status == "merged"


def test_rejecting_keeps_both_and_carries_the_note(client, session):
    a, b = two_people(session)
    candidate = queued(session, a, b)

    response = client.post(f"/v1/review/duplicates/{candidate.id}/reject",
                           json={"note": "different ORCIDs"})
    assert response.status_code == 200, response.text

    session.expire_all()
    assert session.query(Person).filter(Person.merged_into.is_(None)).count() == 2
    row = session.get(MergeCandidate, candidate.id)
    assert row.status == "rejected"
    assert "different ORCIDs" in str(row.signals)


def test_deferring_keeps_the_reason_it_was_given(client, session):
    """The whole point of deferring rather than rejecting is that somebody
    reads the row again. What they read is this note."""
    a, b = two_people(session)
    candidate = queued(session, a, b)
    reason = ("one mass-authored paper, no shared ORCID, and OpenAlex holds "
              "them as different authors")

    response = client.post(f"/v1/review/duplicates/{candidate.id}/defer",
                           json={"note": reason})
    assert response.status_code == 200, response.text

    session.expire_all()
    row = session.get(MergeCandidate, candidate.id)
    assert row.status == "deferred"
    assert row.signals["deferred_because"] == reason
    assert session.query(Person).filter(Person.merged_into.is_(None)).count() == 2


def test_deferring_without_a_note_still_says_something(client, session):
    """A blank note must not produce a blank reason — the row would be
    indistinguishable from one nobody looked at."""
    a, b = two_people(session)
    candidate = queued(session, a, b)

    assert client.post(f"/v1/review/duplicates/{candidate.id}/defer").status_code == 200
    session.expire_all()
    assert session.get(MergeCandidate, candidate.id).signals["deferred_because"]


@pytest.mark.parametrize("action", ["merge", "reject", "defer"])
def test_a_candidate_that_does_not_exist_is_a_404(client, session, action):
    assert client.post(f"/v1/review/duplicates/999999/{action}").status_code == 404


@pytest.mark.parametrize("action", ["merge", "reject", "defer"])
def test_a_pair_cannot_be_decided_twice(client, session, action):
    """Two reviewers on the same list, or one double-click. The second answer
    must be refused rather than applied on top of the first."""
    a, b = two_people(session)
    candidate = queued(session, a, b)

    assert client.post(f"/v1/review/duplicates/{candidate.id}/defer").status_code == 200
    second = client.post(f"/v1/review/duplicates/{candidate.id}/{action}")
    assert second.status_code == 404, second.text

    session.expire_all()
    assert session.get(MergeCandidate, candidate.id).status == "deferred"
