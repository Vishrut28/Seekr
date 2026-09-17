"""The yardstick itself: grading rules and ranking metrics."""

import math

from evaluation.grader import Case, Profile, dcg, grade, score_case, summarize
from rip.textnorm import stems, words


def case(**kw):
    base = dict(id="x", query="q", kind="topic", strong=[], related=[], country=None,
                org=[], person_ids=[])
    base.update(kw)
    for key in ("strong", "related", "org"):
        base[key] = [tuple(stems(s)) for s in base[key]]
    if "name" in base:
        base["name"] = tuple(words(base["name"]))
    return Case(**base)


def person(pid="p", name="Ada Example", country=None, topics=(), texts=(), orgs=(), names=None):
    return Profile(pid, name, country,
                   names=[tuple(words(n)) for n in (names or [name])],
                   topics=[tuple(stems(t)) for t in topics],
                   texts=[tuple(stems(t)) for t in texts],
                   orgs=[tuple(stems(o)) for o in orgs])


def test_a_stated_subject_is_highly_relevant_and_a_related_one_partly():
    c = case(strong=["cosmology"], related=["particle physics"])
    assert grade(c, person(topics=["Cosmology and Gravitation Theories"])) == 2
    assert grade(c, person(topics=["Particle physics theoretical and experimental studies"])) == 1
    assert grade(c, person(topics=["Wildlife Ecology"])) == 0


def test_one_paper_title_is_not_a_research_area_but_two_are():
    c = case(strong=["covid"])
    assert grade(c, person(texts=["COVID-19 and crop yields"])) == 0
    assert grade(c, person(texts=["COVID-19 and crop yields", "COVID vaccine uptake"])) == 1


def test_constraints_must_hold_unless_the_query_is_soft():
    c = case(strong=["cosmology"], country="IN")
    far = person(country="US", topics=["Cosmology"])
    assert grade(c, far) == 0 and grade(c, person(country="IN", topics=["Cosmology"])) == 2
    soft = case(strong=["cosmology"], country="IN", soft=True)
    assert grade(soft, far) == 1


def test_names_are_matched_by_words_and_not_stemmed():
    c = case(name="Rahul Mulajkar")
    assert grade(c, person(names=["Rahul Mukundrao Mulajkar"])) == 2
    assert grade(c, person(names=["R. Mulajkar"])) == 1
    assert grade(case(name="Kim Rogers"), person(names=["Kim Roger"])) == 0


def test_ndcg_rewards_putting_the_best_first():
    profiles = {"a": person("a", topics=["Cosmology"]),
                "b": person("b", topics=["Particle physics"]),
                "c": person("c", topics=["Wildlife"])}
    c = case(strong=["cosmology"], related=["particle physics"])
    best = score_case(c, ["a", "b", "c"], profiles)
    worse = score_case(c, ["c", "b", "a"], profiles)
    assert best["ndcg_at_10"] == 1.0 and worse["ndcg_at_10"] < best["ndcg_at_10"]
    assert best["recall_at_50"] == 1.0 and best["relevant_in_corpus"] == 2
    assert math.isclose(dcg([2, 1]), 3 + 1 / math.log2(3))


def test_a_query_that_returns_nothing_is_counted_when_people_were_there():
    profiles = {"a": person("a", topics=["Cosmology"])}
    rows = [score_case(case(strong=["cosmology"]), [], profiles)]
    assert summarize(rows)["zero_results_with_relevant_people"] == 1
    assert rows[0]["ndcg_at_10"] == 0.0 and rows[0]["recall_at_50"] == 0.0
