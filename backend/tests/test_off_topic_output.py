"""Unrelated fame outranked the actual expert, and the guard against it was
switched off by a ceiling it sat behind.

`output` is a quarter of the score and measures work shipped. Work that is
not what you asked about has always counted at OFF_TOPIC_WEIGHT (0.25), with
the stated intent that "an unrelated famous repo never outranks on-topic
work". But the discount was applied to the INPUT of a log that saturates at
OUTPUT_SATURATION (5,000), and 0.25 x anything above 20,000 is still past
5,000. For 97 people in the corpus — 12.6% of it — the weight changed
nothing at all: they scored output 1.000 for every query, whatever it asked.

The visible result, on the query "information retrieval": Geoffrey Hinton
first, on 195,452 citations of which NONE were on the subject, ahead of every
information-retrieval researcher in the graph. Six people tied at exactly
0.498 because output had pinned them all to the ceiling and nothing else
separated them.

Scaling each half on its own and discounting the scaled one puts the weight
back in force above the ceiling, which is what it was always documented to
do. These tests fix the property rather than the arithmetic: the constants
can be retuned without rewriting them, and a return to discounting before the
scale fails them.
"""

import pytest

from rip.ingest import ingest_profile
from rip.nlq import (
    OFF_TOPIC_WEIGHT,
    OUTPUT_SATURATION,
    _log_scale,
    _output_component,
    parse,
    relevance_scores,
)
from rip.normalize import EvidenceItem, PublicationData
from tests.test_resolution import make_profile

# well past the ceiling even after the discount: 0.25 x this > OUTPUT_SATURATION
FAMOUS = 200_000


# --------------------------------------------------------------------------
# the component itself
# --------------------------------------------------------------------------

def test_fame_for_something_else_cannot_exceed_its_weight():
    """However cited, work on another subject is worth at most its discount.

    This is the property the old arithmetic lost: it is stated in terms of
    OFF_TOPIC_WEIGHT, so retuning the weight cannot silently repeal it."""
    for citations in (10_000, FAMOUS, 10_000_000):
        assert _output_component(0.0, citations) <= OFF_TOPIC_WEIGHT + 1e-9, citations


def test_modest_on_topic_work_beats_vast_unrelated_work():
    """The sentence OFF_TOPIC_WEIGHT's comment has always claimed."""
    assert _output_component(400.0, 0.0) > _output_component(0.0, 10_000_000.0)


def test_the_discount_still_bites_above_the_ceiling():
    """The actual defect. Two people, both far past OUTPUT_SATURATION, one on
    the subject and one not, must not score the same."""
    on = _output_component(FAMOUS, 0.0)
    off = _output_component(0.0, FAMOUS)
    assert on == pytest.approx(1.0)
    assert off < on, "saturation swallowed the off-topic discount again"


def test_on_topic_work_is_not_diluted_by_anything_else():
    """Someone's on-topic output scores the same whether or not they also did
    unrelated work — the discount bounds the extra, it does not tax the rest."""
    alone = _output_component(2_000.0, 0.0)
    plus_other_work = _output_component(2_000.0, FAMOUS)
    assert plus_other_work >= alone
    assert alone == pytest.approx(_log_scale(2_000.0, OUTPUT_SATURATION))


def test_a_query_with_no_subject_treats_everything_as_on_topic():
    """`_output_signals` routes every row to the on-topic half when the query
    named no subject, so there is nothing to discount and output is the plain
    scale it always was."""
    assert _output_component(12_000.0, 0.0) == pytest.approx(
        _log_scale(12_000.0, OUTPUT_SATURATION))


# --------------------------------------------------------------------------
# end to end, through scoring
# --------------------------------------------------------------------------

def _person(session, name, ext, *, topic, papers):
    # usernames and raw are overridden too: make_profile's defaults are one
    # fixed GitHub login, and two profiles sharing it resolve to ONE person —
    # which silently turns a two-person ranking test into a one-person one.
    return ingest_profile(session, make_profile(
        source="openalex", source_type="scholarly", external_id=ext,
        url=f"https://openalex.org/{ext}", name=name,
        usernames=[f"openalex:{ext}"], raw={"id": ext},
        evidence=[EvidenceItem(attribute_type="research_interest",
                               value=topic, confidence=0.7)],
        publications=[PublicationData(title=t, external_id=f"{ext}-{i}",
                                      citations=c, topics=[pt],
                                      published_date="2025-01-01")
                      for i, (t, c, pt) in enumerate(papers)]))


def test_the_celebrity_does_not_outrank_the_specialist(session):
    """The h-ir case, reduced: someone enormously cited for another subject
    against someone modestly cited for this one. Both claim the topic, so
    depth cannot be what separates them."""
    famous = _person(session, "Famous Elsewhere", "A1",
                     topic="Information Retrieval",
                     papers=[("Deep learning for vision", FAMOUS, "Computer Vision")])
    specialist = _person(session, "Actual Specialist", "A2",
                         topic="Information Retrieval",
                         papers=[("Ranking for information retrieval", 400,
                                  "Information Retrieval")])
    session.commit()

    parsed = parse(session, "information retrieval")
    scores = relevance_scores(session, parsed, [famous.id, specialist.id])
    assert scores[specialist.id]["score"] > scores[famous.id]["score"], (
        scores[specialist.id]["components"], scores[famous.id]["components"])
    # and the breakdown says WHY, which is the point of carrying one
    assert scores[famous.id]["impact"] == 0.0
    assert scores[famous.id]["impact_off_topic"] >= FAMOUS


def test_off_topic_celebrities_still_tie_but_no_longer_at_the_top(session):
    """What this change does NOT fix, asserted so it is not mistaken for fixed.

    Two people cited 900,000 and 30,000 times for an unrelated subject still
    score identically on output — both halves saturate, so the component
    cannot separate them. The difference is where the tie now sits: at
    OFF_TOPIC_WEIGHT rather than at 1.000, below anyone with on-topic work
    instead of above them. Breaking the tie itself would mean not saturating,
    which is a different decision from this one."""
    big = _person(session, "Very Cited", "B1", topic="Information Retrieval",
                  papers=[("Something else entirely", 900_000, "Genetics")])
    bigger = _person(session, "Even More Cited", "B2", topic="Information Retrieval",
                     papers=[("A different thing", 30_000, "Genetics")])
    modest = _person(session, "On The Subject", "B3", topic="Information Retrieval",
                     papers=[("Query expansion", 300, "Information Retrieval")])
    session.commit()

    scores = relevance_scores(session, parse(session, "information retrieval"),
                              [big.id, bigger.id, modest.id])
    out = {p.id: scores[p.id]["components"]["output"] for p in (big, bigger, modest)}
    assert out[big.id] == out[bigger.id], "no longer tied — update this test"
    assert out[big.id] <= OFF_TOPIC_WEIGHT + 1e-9
    assert out[modest.id] > out[big.id], "the tie is still above on-topic work"
