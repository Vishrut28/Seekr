"""The audit that reads the criteria rather than the ranker.

Its job is to catch a criterion selecting the wrong field, which is the
failure the ranking eval cannot report: when the ranker shares the mistake,
the query scores 1.000 and looks like the best thing in the set.
"""

from evaluation.grader import Case
from rip.textnorm import stems
from scripts.audit_judgments import ceiling, term_report
from tests.test_evaluation import person


def case(cid, query, strong):
    return Case(id=cid, query=query, kind="holdout2",
                strong=[tuple(stems(s)) for s in strong],
                related=[], country=None, org=[], person_ids=[])


def test_a_one_word_term_pulling_in_another_field_is_reported(capsys):
    """Only the one-word terms are reported, and each is shown with what the
    people it brought in actually say -- reading those topics is the check."""
    c = case("x", "chemists", ["analytical chemistry", "synthesis"])
    profiles = {
        "ml": person("ml", "Deep Learner",
                     topics=["Generative adversarial network and image synthesis"]),
        "chem": person("chem", "Real Chemist", topics=["Analytical chemistry"]),
    }
    assert term_report([c], profiles) == 1
    out = capsys.readouterr().out
    assert "'synthesis' alone brings 1" in out
    assert "image synthesis" in out
    assert "Real Chemist" not in out       # graded by the phrase, not the word


def test_a_word_sharing_its_case_with_the_phrase_that_means_the_query_is_not_blamed():
    """Alone is the test. A term is only the criterion for someone when no
    other term of its case would have graded them relevant anyway."""
    c = case("x", "chemists", ["chemistry", "synthesis"])
    both = {"p": person("p", "Both", topics=["Analytical chemistry",
                                             "Organic synthesis of heterocycles"])}
    assert term_report([c], both) == 0


def test_a_case_with_nothing_to_report_says_so(capsys):
    c = case("x", "computational pathology", ["digital pathology"])
    term_report([c], {"p": person("p", "P", topics=["Digital pathology"])}, only="x")
    assert "no one-word term" in capsys.readouterr().out


def test_the_recall_ceiling_falls_as_the_relevant_set_grows():
    assert ceiling(10) == 1.0          # fifty results hold all ten
    assert ceiling(50) == 1.0
    assert ceiling(126) == 50 / 126    # "machine learning engineers": 0.40
    assert ceiling(0) == 0.0           # nobody relevant measures nothing
    assert ceiling(100, at=100) == 1.0
