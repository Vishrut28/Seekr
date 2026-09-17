"""Unit tests for rip.weight_fitting — the advisory logistic-regression
fitter behind scripts/fit_weights.py.

Every case here uses small, hand-constructed data with a mathematically
known correct answer (a feature perfectly correlated with the label MUST
get a strongly positive coefficient, etc.). This is deliberately NOT tested
against a large "realistic-looking" synthetic MatchFeedback dataset: that
would only prove the model recovers patterns someone else already assumed
when generating the fake data, which validates nothing about real user
judgement. These tests validate the arithmetic is correct; they say nothing
about what any real fit would mean, because they were never meant to.
"""
import math

from rip.weight_fitting import (
    coefficients_to_weights,
    fit_logistic_regression,
    sigmoid,
)


def test_sigmoid_basic_values():
    assert sigmoid(0.0) == 0.5
    assert sigmoid(30.0) > 0.999999
    assert sigmoid(-30.0) < 0.000001
    # clipping: must not raise OverflowError on an extreme input
    assert 0.0 <= sigmoid(10_000.0) <= 1.0
    assert 0.0 <= sigmoid(-10_000.0) <= 1.0


def test_perfectly_correlated_feature_gets_strong_positive_weight():
    """feature=1 exactly when label=1, feature=0 exactly when label=0 — the
    single cleanest possible positive signal a fitter could be given."""
    X = [[1.0], [1.0], [1.0], [0.0], [0.0], [0.0]] * 5
    y = [1, 1, 1, 0, 0, 0] * 5
    coefs = fit_logistic_regression(X, y)
    assert coefs[0] > 1.0


def test_perfectly_anticorrelated_feature_gets_negative_weight():
    """The mirror image: feature=1 exactly when label=0."""
    X = [[1.0], [1.0], [1.0], [0.0], [0.0], [0.0]] * 5
    y = [0, 0, 0, 1, 1, 1] * 5
    coefs = fit_logistic_regression(X, y)
    assert coefs[0] < -1.0


def test_irrelevant_feature_stays_near_zero():
    """A feature with no relationship to the label — alternating 0/1
    regardless of label — should not be reported as a strong signal
    either way, positive or negative."""
    X = [[0.0], [1.0], [0.0], [1.0], [0.0], [1.0], [0.0], [1.0]]
    y = [1, 1, 0, 0, 1, 1, 0, 0]
    coefs = fit_logistic_regression(X, y)
    assert abs(coefs[0]) < 0.5


def test_stronger_signal_gets_larger_coefficient_than_weaker_signal():
    """Two features, one a clean predictor and one a noisy version of the
    same underlying signal — the clean one must end up weighted higher."""
    # feature A: perfectly matches the label
    # feature B: matches the label 60% of the time (noisy)
    X = [
        [1.0, 1.0], [1.0, 1.0], [1.0, 0.0],   # label 1: A always 1, B mostly 1
        [0.0, 0.0], [0.0, 0.0], [0.0, 1.0],   # label 0: A always 0, B mostly 0
    ] * 5
    y = [1, 1, 1, 0, 0, 0] * 5
    coefs = fit_logistic_regression(X, y)
    assert coefs[0] > coefs[1]


def test_empty_input_returns_empty_coefficients():
    assert fit_logistic_regression([], []) == []


def test_coefficients_to_weights_normalizes_to_sum_one():
    names = ["a", "b", "c"]
    coefs = [2.0, 1.0, 0.0]
    weights, negative = coefficients_to_weights(names, coefs)
    assert abs(sum(weights.values()) - 1.0) < 1e-9
    assert weights["a"] > weights["b"] > weights["c"]
    assert negative == []


def test_coefficients_to_weights_flags_negative_and_excludes_it():
    names = ["a", "b", "c"]
    coefs = [2.0, 1.0, -3.0]
    weights, negative = coefficients_to_weights(names, coefs)
    assert negative == ["c"]
    # excluded from the normalized dict, not clipped-to-zero-and-counted
    assert weights["c"] == 0.0
    assert abs(sum(weights.values()) - 1.0) < 1e-9
    # the ratio between the two positive weights is preserved (2:1), within
    # the rounding the implementation applies (round to 4 decimal places)
    assert abs(weights["a"] - 2 * weights["b"]) < 1e-3


def test_coefficients_to_weights_all_negative_returns_all_zero_no_crash():
    """Every component correlated negatively with 'good' — a real, if
    alarming, possible outcome. Must not divide by zero."""
    names = ["a", "b"]
    coefs = [-1.0, -2.0]
    weights, negative = coefficients_to_weights(names, coefs)
    assert weights == {"a": 0.0, "b": 0.0}
    assert set(negative) == {"a", "b"}


def test_coefficients_to_weights_all_zero_returns_all_zero_no_crash():
    names = ["a", "b"]
    coefs = [0.0, 0.0]
    weights, negative = coefficients_to_weights(names, coefs)
    assert weights == {"a": 0.0, "b": 0.0}
    assert negative == []


# --- integration: does the DB/component-extraction plumbing actually work? ---
# One deliberately small, controlled row — enough to prove collect_training_rows
# correctly wires MatchFeedback -> parse() -> relevance_scores() -> a feature
# vector. This is NOT a synthetic dataset meant to produce a believable
# report (see module docstring for why that would be the wrong kind of test);
# it exists only to catch a plumbing bug like a wrong dict key or an unhandled
# missing-person case, which the pure-math tests above cannot see.

def test_collect_training_rows_wires_feedback_to_score_components(session):
    from rip.ingest import ingest_profile
    from rip.models import MatchFeedback
    from rip.normalize import EvidenceItem
    from scripts.fit_weights import GENERAL_COMPONENTS, collect_training_rows
    from tests.test_resolution import make_profile

    person = ingest_profile(
        session,
        make_profile(
            name="Test Person",
            evidence=[EvidenceItem(attribute_type="skill", value="Rust", confidence=0.8)],
        ),
    )
    session.add(MatchFeedback(
        person_id=person.id, query_raw="rust", query_norm="rust", verdict="good",
    ))
    session.commit()

    (gX, gy), (nX, ny), skipped = collect_training_rows(session)
    assert skipped == 0
    assert len(gX) == 1 and gy == [1]
    assert len(gX[0]) == len(GENERAL_COMPONENTS)
    assert nX == [] and ny == []


def test_collect_training_rows_skips_a_merged_away_person(session):
    """A vote for a person later merged into someone else (or deleted) must
    be skipped, since relevance_scores() always returns a components dict —
    even all-zero — for any id it's asked about, so an absent/tombstoned
    person needs an explicit existence check, not a check on the score."""
    from rip.ingest import ingest_profile
    from rip.models import MatchFeedback
    from scripts.fit_weights import collect_training_rows
    from tests.test_resolution import make_profile

    person = ingest_profile(session, make_profile(name="Later Merged Away"))
    session.add(MatchFeedback(
        person_id=person.id, query_raw="kubernetes", query_norm="kubernetes",
        verdict="bad",
    ))
    session.commit()
    person.merged_into = "some-other-person-id"
    session.commit()

    (gX, gy), (nX, ny), skipped = collect_training_rows(session)
    assert skipped == 1
    assert gX == [] and gy == []


def test_collect_training_rows_keeps_a_zero_evidence_person_as_real_signal(session):
    """A vote for a person who currently has weak/no matching evidence is
    NOT skipped — an all-zero component vector on a "good" vote is real,
    if concerning, information for the fitter, not something to hide."""
    from rip.ingest import ingest_profile
    from rip.models import MatchFeedback
    from scripts.fit_weights import collect_training_rows
    from tests.test_resolution import make_profile

    person = ingest_profile(session, make_profile(name="No Skills Here"))
    session.add(MatchFeedback(
        person_id=person.id, query_raw="kubernetes", query_norm="kubernetes",
        verdict="bad",
    ))
    session.commit()

    (gX, gy), (nX, ny), skipped = collect_training_rows(session)
    assert skipped == 0
    assert len(gX) == 1 and gy == [0]
