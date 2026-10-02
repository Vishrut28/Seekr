"""The measures that score search against the relevance labels."""

from scripts.score_relevance_labels import agreement, arm_scores, by_kind, pool_recall, wilson

LABELS = {"a": 2, "b": 0, "c": 2, "d": 1, "e": 0}
ARMS = {"a": ["seekr", "criteria"], "b": ["seekr"], "c": ["words"], "d": ["criteria"], "e": ["random"]}
QUERY = {"a": "q1", "b": "q1", "c": "q1", "d": "q2", "e": "q2"}


def test_a_card_counts_for_every_arm_that_brought_it_in():
    scores = arm_scores(LABELS, ARMS, QUERY)
    assert scores["seekr"]["cards"] == 2 and scores["seekr"]["graded_2"] == 0.5
    assert scores["criteria"]["cards"] == 2
    assert scores["criteria"]["graded_2"] == 0.5 and scores["criteria"]["graded_1_or_2"] == 1.0
    assert scores["random"]["mean_grade"] == 0.0


def test_the_per_query_mean_weighs_queries_not_cards():
    labels = {"a": 2, "b": 2, "c": 2, "d": 0}
    arms = {c: ["seekr"] for c in labels}
    query = {"a": "q1", "b": "q1", "c": "q1", "d": "q2"}
    s = arm_scores(labels, arms, query)["seekr"]
    assert s["graded_2"] == 0.75
    assert s["graded_2_per_query_mean"] == 0.5


def test_an_arm_with_no_cards_reports_nothing_rather_than_zero():
    s = arm_scores({"a": 2}, {"a": ["seekr"]}, {"a": "q"})["random"]
    assert s["cards"] == 0 and s["graded_2"] is None and s["mean_grade"] is None


def test_the_table_reads_criteria_down_and_labels_across():
    out = agreement({"a": 2, "b": 0, "c": 2}, {"a": 2, "b": 2, "c": 0})
    assert out["rows_criteria_cols_label"] == [[0, 0, 1], [0, 0, 0], [1, 0, 1]]
    assert out["exact"] == 0.333
    assert out["criteria_2_label_0"] == 1 and out["criteria_0_label_2"] == 1


def test_cards_the_criteria_cannot_grade_are_left_out_not_counted_wrong():
    out = agreement({"a": 2, "b": 0}, {"a": 2})
    assert out["cards"] == 1 and out["exact"] == 1.0


def test_pool_recall_skips_queries_with_nothing_relevant():
    out = pool_recall(LABELS, ARMS, QUERY)
    assert out["per_query"] == {"q1": 0.5}
    assert out["mean"] == 0.5


def test_by_kind_splits_each_arm_by_the_kind_of_query():
    kinds = {"a": "topic", "b": "topic", "c": "topic", "d": "typo", "e": "typo"}
    out = by_kind(LABELS, ARMS, kinds)
    assert out["topic"]["seekr"] == 0.5 and out["topic"]["random"] is None
    assert out["typo"]["criteria"] == 0.0


def test_wilson_stays_inside_zero_and_one():
    assert wilson(0, 0) == (0.0, 0.0)
    lo, hi = wilson(10, 10)
    assert 0.69 < lo < 0.73 and hi == 1.0
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and 0.27 < hi < 0.31
