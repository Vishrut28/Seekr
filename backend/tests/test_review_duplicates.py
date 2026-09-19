"""The two buttons under "Possible duplicates", which nothing covered.

A reviewer works this queue by hand, and one of its two decisions rewrites the
graph: merging moves every claim, affiliation and paper one person holds onto
the other and leaves a tombstone behind. Nothing in the codebase puts them
back. These pin down what each button does, including the guards that stop the
same pair being decided twice.
"""

import pytest

from rip import api
from rip.ingest import ingest_profile
from rip.models import Evidence, MergeCandidate, Person
from rip.normalize import EvidenceItem
from rip.review import resolve_duplicate
from tests.test_resolution import make_profile


def pair(session):
    """Two separate people, queued against each other."""
    a = ingest_profile(session, make_profile(
        external_id="a", url="https://openalex.org/a", raw={"id": "a"},
        name="Ann Example", usernames=["openalex:a"],
        evidence=[EvidenceItem(attribute_type="skill", value="Rust")]))
    b = ingest_profile(session, make_profile(
        source="dblp", external_id="b", url="https://dblp.org/pid/b", raw={"id": "b"},
        name="A. Example", usernames=["dblp:b"],
        evidence=[EvidenceItem(attribute_type="skill", value="Go")]))
    session.flush()
    assert a.id != b.id
    candidate = MergeCandidate(
        person_id=a.id, candidate_person_id=b.id, score=0.8,
        signals={"reason": "near-identical name"}, status="pending")
    session.add(candidate)
    session.commit()
    return a, b, candidate


def test_merging_moves_everything_onto_the_survivor(session):
    a, b, candidate = pair(session)
    out = api.review_duplicate_merge(candidate_id=candidate.id, db=session)

    assert out["status"] == "merged" and out["kept_person_id"] == a.id
    assert candidate.status == "merged"
    # the folded person is a tombstone pointing at the survivor, not a deletion
    assert session.get(Person, b.id).merged_into == a.id
    # and what they knew came with them
    skills = {e.value for e in session.query(Evidence).filter(
        Evidence.person_id == a.id, Evidence.attribute_type == "skill")}
    assert skills == {"Rust", "Go"}


def test_rejecting_closes_the_pair_and_changes_nothing_else(session):
    a, b, candidate = pair(session)
    out = api.review_duplicate_reject(candidate_id=candidate.id, db=session)

    assert out["status"] == "rejected" and candidate.status == "rejected"
    # both people are still their own person
    assert session.get(Person, a.id).merged_into is None
    assert session.get(Person, b.id).merged_into is None


def test_a_pair_is_only_decided_once(session):
    """Two reviewers, or one double-click: the second decision must not land
    on a pair already settled, or a rejected pair could still be merged."""
    a, b, candidate = pair(session)
    api.review_duplicate_reject(candidate_id=candidate.id, db=session)
    with pytest.raises(Exception) as caught:
        api.review_duplicate_merge(candidate_id=candidate.id, db=session)
    assert "already rejected" in str(caught.value)
    assert session.get(Person, b.id).merged_into is None


def test_a_candidate_that_does_not_exist_is_not_found(session):
    with pytest.raises(Exception) as caught:
        api.review_duplicate_merge(candidate_id=9999, db=session)
    assert "not found" in str(caught.value)


def test_the_queue_only_offers_pairs_nobody_has_decided(session):
    a, b, candidate = pair(session)
    before = api.review_merges(db=session)["possible_duplicates"]
    assert [d["candidate_id"] for d in before] == [candidate.id]

    resolve_duplicate(session, candidate.id, "reject")
    after = api.review_merges(db=session)["possible_duplicates"]
    assert [d["candidate_id"] for d in after] == []
