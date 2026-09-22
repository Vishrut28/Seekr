"""The two buttons under "Possible duplicates", which nothing covered.

A reviewer works this queue by hand, and one of its two decisions rewrites the
graph: merging moves every claim, affiliation and paper one person holds onto
the other and leaves a tombstone behind. Nothing in the codebase puts them
back. These pin down what each button does, including the guards that stop the
same pair being decided twice.
"""

import pytest
from rip.ingest import ingest_profile
from rip.models import Evidence, MergeCandidate, Person
from rip.normalize import EvidenceItem
from rip.review import resolve_duplicate
from tests.test_resolution import make_profile

from rip import api


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


def test_deferring_clears_the_queue_without_claiming_anything(session):
    """The third outcome, and the reason it exists: eleven ALICE fragments had
    no evidence either way, and "Different people" would have said otherwise
    permanently."""
    a, b, candidate = pair(session)
    out = resolve_duplicate(session, candidate.id, "defer",
                            "every paper is a mass-authorship credit")

    assert out["status"] == "deferred" and candidate.status == "deferred"
    assert candidate.signals["deferred_because"] == \
        "every paper is a mass-authorship credit"
    # nobody was merged and nobody was declared separate
    assert session.get(Person, a.id).merged_into is None
    assert session.get(Person, b.id).merged_into is None
    assert api.review_merges(db=session)["possible_duplicates"] == []


def deferred_or_rejected(session, decision):
    """Two same-named records, that decision applied, then a paper appears
    under both. Returns what the next sweep did about it.

    Same name on both, because that is the condition for the sweep to look at
    the pair at all: it groups by name key. A pair spelled two ways --
    "Dhruv Utpalkumar Dixit" against "D. Dixit" -- is never re-derived, so
    deferring one of those is as final as rejecting it. Worth knowing, and
    true of one of the thirteen pairs this was built for.
    """
    from rip.ingest import ingest_profile
    from rip.models import Authorship, Publication
    from rip.normalize import EvidenceItem
    from tests.test_resolution import make_profile

    from rip import dedupe

    a = ingest_profile(session, make_profile(
        external_id="same-a", url="https://openalex.org/same-a", raw={"id": "a"},
        name="Ann Example", usernames=["openalex:same-a"],
        evidence=[EvidenceItem(attribute_type="skill", value="Rust")]))
    b = ingest_profile(session, make_profile(
        source="dblp", external_id="same-b", url="https://dblp.org/pid/same-b",
        raw={"id": "b"}, name="Ann Example", usernames=["dblp:same-b"],
        evidence=[EvidenceItem(attribute_type="skill", value="Go")]))
    session.commit()
    # ingest queues the pair itself when two records share a name, so the
    # candidate is already here -- inserting one collides with uq_merge_candidate
    candidate = session.query(MergeCandidate).filter(
        MergeCandidate.person_id.in_([a.id, b.id]),
        MergeCandidate.candidate_person_id.in_([a.id, b.id])).one()
    resolve_duplicate(session, candidate.id, decision)

    shared = Publication(title="A paper they share", external_id="W-shared",
                         raw_authors=["Ann Example", "Bo Helper"])
    session.add(shared)
    session.flush()
    session.add_all([Authorship(person_id=a.id, publication_id=shared.id),
                     Authorship(person_id=b.id, publication_id=shared.id)])
    session.commit()

    # apply(), not plan(): plan only computes, and acting on a deferred row
    # happens where the plan is carried out. So this is the next SWEEP --
    # nightly_refresh.sh -- not something that happens the moment a paper lands.
    dedupe.apply(session, dedupe.plan(session, person_ids=[a.id, b.id]))
    session.refresh(candidate)
    return candidate, a, b


def test_deferring_leaves_the_pair_open_to_the_next_sweep(session):
    """The whole difference from rejecting. Evidence arrives, and the sweep is
    free to act on a deferred pair -- here the shared paper is strong enough
    that it merges them outright rather than re-queueing."""
    candidate, a, b = deferred_or_rejected(session, "defer")
    assert candidate.status == "merged", candidate.status
    living = [p for p in (session.get(Person, a.id), session.get(Person, b.id))
              if p.merged_into is None]
    assert len(living) == 1, "the sweep acted on it"


def test_rejecting_puts_the_pair_beyond_the_sweeps_reach(session):
    """The same evidence, and nothing happens, for ever. Rejecting records
    that they are two people, and dedupe.plan excludes every non-deferred row
    from being re-proposed -- which is exactly why it is the wrong answer for
    a pair nothing could settle."""
    candidate, a, b = deferred_or_rejected(session, "reject")
    assert candidate.status == "rejected"
    assert session.get(Person, a.id).merged_into is None
    assert session.get(Person, b.id).merged_into is None


def test_a_deferred_pair_is_not_treated_as_decided(session):
    """plan's `already` set is what stops a decided pair coming back, and a
    deferred row must not be in it."""
    from sqlalchemy import select as sa_select

    a, b, candidate = pair(session)
    resolve_duplicate(session, candidate.id, "defer")
    decided = {
        frozenset(row) for row in session.execute(
            sa_select(MergeCandidate.person_id, MergeCandidate.candidate_person_id)
            .where(MergeCandidate.status != "deferred")).all()
    }
    assert frozenset((a.id, b.id)) not in decided


# NOT TESTED, and why: the deferred->pending branch in dedupe.apply re-queues
# rather than merges when new evidence is enough to weigh but not to act on.
# It is hard to reach in a small fixture, because plan's ambiguity guard drops
# a pair whose record matches two others equally. The two tests above pin the
# reachable halves instead: a deferred pair can be acted on, a rejected one
# cannot, and that is the difference the reviewer is choosing between.


def test_deferring_records_a_reason_even_without_one_given(session):
    _a, _b, candidate = pair(session)
    resolve_duplicate(session, candidate.id, "defer")
    assert candidate.signals["deferred_because"]


def test_an_unknown_action_is_refused(session):
    _a, _b, candidate = pair(session)
    with pytest.raises(ValueError, match="merge, reject or defer"):
        resolve_duplicate(session, candidate.id, "ignore")
    assert candidate.status == "pending"


def test_a_rejection_can_say_why_too(session):
    _a, _b, candidate = pair(session)
    resolve_duplicate(session, candidate.id, "reject", "different ORCIDs")
    assert candidate.status == "rejected"
    assert candidate.signals["rejected_because"] == "different ORCIDs"
