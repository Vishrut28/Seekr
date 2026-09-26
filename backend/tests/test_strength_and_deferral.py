"""Two things about how dedupe weighs evidence, found together.

strength() scored a co-author overlap by its ratio alone, so 2-of-2 counted
exactly like 40-of-40 — "100%" either way. It orders the clustering of proven
pairs and decides _torn(), where a rival at least half as strong makes a match
look contested. On the live corpus the rival holding a well-evidenced pair
back was a one-paper fragment sharing 1 co-author out of 1.

Fixing that let the pair through — and the pair was one a reviewer had
DEFERRED two days earlier, because judge() accepts a co-author overlap as
proof where the reviewer's rule wants a shared ORCID or a shared paper.
Nothing in plan() respected a deferral for merges; only an accident had.
Now a deferred pair is merged only when what grew is shared papers -- the
reviewer's own standard. Proof on anything else goes back to review, and
only when its evidence has grown.
"""

import pytest
from rip.dedupe import COAUTHOR_POOL_FOR_FULL_WEIGHT, Judgement, _torn, strength
from rip.ingest import ingest_profile
from rip.models import MergeCandidate
from tests.test_dedupe import profile

from rip import dedupe


def j(*, pubs=0, shared=0, ratio=0.0, pool=None, orgs=0, decision="merge"):
    signals = {"shared_publications": pubs, "shared_coauthors": shared,
               "coauthor_overlap": ratio, "shared_organizations": orgs,
               "topic_overlap": None}
    if pool is not None:
        signals["coauthor_pool"] = pool
    return Judgement(decision, "test", signals)


# --------------------------------------------------------------------------
# strength()
# --------------------------------------------------------------------------

def test_a_thin_hundred_percent_weighs_less_than_a_thick_one():
    thin = strength(j(shared=2, ratio=1.0, pool=2))
    thick = strength(j(shared=40, ratio=1.0, pool=40))
    assert thin < thick
    assert thin == pytest.approx(thick * 2 / COAUTHOR_POOL_FOR_FULL_WEIGHT)


def test_a_pool_at_the_bar_counts_in_full():
    at_bar = strength(j(shared=5, ratio=1.0, pool=COAUTHOR_POOL_FOR_FULL_WEIGHT))
    assert at_bar == pytest.approx(3.0)


def test_shared_papers_and_organisations_are_untouched():
    assert strength(j(pubs=2, orgs=2)) == pytest.approx(2 + 0.5 * 2)


def test_a_judgement_recorded_before_the_pool_existed_still_scores():
    """Old queue rows carry only the shared count, a lower bound on the pool."""
    assert strength(j(shared=2, ratio=1.0)) == pytest.approx(3.0 * 2 / 5)


# --------------------------------------------------------------------------
# _torn(): a one-of-one rival is no longer a contest
# --------------------------------------------------------------------------

def test_a_one_of_one_rival_does_not_make_a_strong_match_contested():
    """The live case: 22 of 46 shared co-authors against a rival fragment
    sharing 1 co-author of a pool of 1, vetoed against the target."""
    record, target_member, rival = "R", "T", "X"
    match = j(shared=22, ratio=0.48, pool=46)
    judged = {frozenset((record, target_member)): match,
              frozenset((record, rival)): j(shared=1, ratio=1.0, pool=1,
                                            decision=None)}
    vetoed = {frozenset((rival, target_member))}
    cluster_of = {record: {record}, target_member: {target_member}, rival: {rival}}
    assert not _torn(record, {target_member}, frozenset((record, target_member)), match,
                     [record, target_member, rival], judged, vetoed, cluster_of)


def test_a_rival_with_real_evidence_still_makes_it_contested():
    """The protection itself must survive: comparable evidence toward a
    provably different person is still a coin toss."""
    record, target_member, rival = "R", "T", "X"
    match = j(shared=22, ratio=0.48, pool=46)
    judged = {frozenset((record, target_member)): match,
              frozenset((record, rival)): j(shared=20, ratio=0.45, pool=44,
                                            decision=None)}
    vetoed = {frozenset((rival, target_member))}
    cluster_of = {record: {record}, target_member: {target_member}, rival: {rival}}
    assert _torn(record, {target_member}, frozenset((record, target_member)), match,
                 [record, target_member, rival], judged, vetoed, cluster_of)


# --------------------------------------------------------------------------
# a deferral beats judge()'s proof
# --------------------------------------------------------------------------

@pytest.fixture()
def proven(session):
    """Two records judge() calls proven: two shared papers."""
    papers = [("W1", "Face recognition", ["Dhruv Dixit", "Priya Raman", "Arjun Mehta"]),
              ("W2", "Multimodal learning", ["Dhruv Dixit", "Priya Raman", "Arjun Mehta"])]
    a = ingest_profile(session, profile("openalex", "A1", "Dhruv Dixit", pubs=papers))
    b = ingest_profile(session, profile("semanticscholar", "S1", "Dhruv Dixit", pubs=papers))
    assert a.id != b.id
    session.query(MergeCandidate).delete()
    session.commit()
    planned = dedupe.plan(session)
    assert len(planned.merges) == 1, "fixture no longer produces a proven pair"
    return a, b, planned.merges[0][2].signals


def defer(session, a, b, signals):
    session.add(MergeCandidate(person_id=a.id, candidate_person_id=b.id, score=1.0,
                               signals={**signals, "deferred_because": "not proven"},
                               status="deferred"))
    session.commit()


def test_a_deferred_pair_is_not_merged_on_judges_word(session, proven):
    a, b, live = proven
    defer(session, a, b, live)
    planned = dedupe.plan(session)
    assert planned.merges == [], "merged over a reviewer's deferral"
    assert planned.reviews == [], "and its evidence has not grown, so it stays put"


def test_new_co_authors_send_a_deferred_pair_back_to_a_human(session, proven):
    """Co-authors are judge()'s standard, not the reviewer's. More of them is
    worth a second look, not a merge."""
    a, b, live = proven
    defer(session, a, b, {**live, "shared_coauthors": live["shared_coauthors"] - 1})
    planned = dedupe.plan(session)
    assert planned.merges == [], "merged on co-authors over a reviewer's deferral"
    assert len(planned.reviews) == 1
    assert "deferred by a reviewer" in planned.reviews[0][2].reason


def test_a_new_shared_paper_may_merge_a_deferred_pair(session, proven):
    """A shared paper IS the reviewer's standard, so new ones settle it --
    the path test_review_duplicates holds open."""
    a, b, live = proven
    defer(session, a, b, {**live, "shared_publications": live["shared_publications"] - 1})
    assert len(dedupe.plan(session).merges) == 1


def test_an_undeferred_proven_pair_still_merges(session, proven):
    """The ordinary path must not change: no deferral, proof, merge."""
    assert len(dedupe.plan(session).merges) == 1
