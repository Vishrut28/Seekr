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


# The criteria themselves, not the grading rules. A one-word `strong` term
# means different things in different fields, and when the ranker shares the
# confusion the benchmark certifies it: "neuroscientists" scored a perfect
# 1.000 because "neural" graded neural-NETWORK researchers relevant and the
# ranker returned exactly those people. These pin the six that were fixed.

def real_cases():
    from evaluation.grader import load_cases
    return {c.id: c for c in load_cases()}


def test_a_neural_network_researcher_is_not_a_neuroscientist_or_a_chemist():
    cases = real_cases()
    ml = person(topics=["Advanced Neural Network Applications",
                        "Generative Adversarial Network and Image Synthesis",
                        "Domain Adaptation and Few Shot Learning"])
    assert grade(cases["h2-neuro-agent"], ml) == 0
    assert grade(cases["c-neuro"], ml) == 0
    assert grade(cases["h-chemists"], ml) == 0


def test_speech_and_protein_synthesis_are_not_chemistry():
    c = real_cases()["h-chemists"]
    assert grade(c, person(topics=["Speech Recognition and Synthesis"])) == 0
    assert grade(c, person(topics=["RNA and Protein Synthesis Mechanisms"])) == 0
    # a chemist synthesises OF something, and that is the discriminator
    assert grade(c, person(topics=["Magnetic property and synthesis of ferrite"])) == 2
    assert grade(c, person(topics=["Multicomponent synthesis of heterocycles"])) == 2
    assert grade(c, person(topics=["Advanced ceramic material synthesis"])) == 2


def test_brain_metastases_are_oncology_not_neuroscience():
    cases = real_cases()
    onc = person(topics=["Brain metastases and treatment", "Glioma diagnosis and treatment"])
    assert grade(cases["c-neuro"], onc) == 0
    assert grade(cases["h2-neuro-agent"], onc) == 0
    assert grade(cases["h2-neuro-agent"],
                 person(topics=["EEG and Brain Computer Interface"])) == 2
    assert grade(cases["h2-neuro-agent"],
                 person(topics=["Advanced neuroimaging technique and application"])) == 2


def test_pathology_the_disease_process_is_weak_evidence_not_strong():
    """In medicine "pathology" means the disease, so it cannot grade 2 on its
    own — but it is the only stated topic of a real computational pathologist,
    so demote it rather than drop it."""
    c = real_cases()["h2-pathology"]
    assert grade(c, person(topics=["Pancreatitis pathology and treatment"])) == 1
    assert grade(c, person(topics=["Pathology"])) == 1
    assert grade(c, person(topics=["Digital pathology", "Molecular pathology"])) == 2


def test_statistical_mechanics_and_the_dsm_are_not_statistics():
    c = real_cases()["h2-statisticians"]
    assert grade(c, person(topics=["Statistical mechanics and entropy"])) == 0
    assert grade(c, person(topics=["Diagnostic and Statistical Manual of Mental Disorders"])) == 0
    assert grade(c, person(topics=["Statistical method and inference"])) == 2
    assert grade(c, person(topics=["Advanced statistical modeling techniques"])) == 2


def test_solar_plasma_physics_is_not_renewable_energy():
    c = real_cases()["h2-energy"]
    assert grade(c, person(topics=["Solar and space plasma dynamics"])) == 0
    assert grade(c, person(topics=["TiO2 photocatalysis and solar cells"])) == 2


def test_a_criterion_edited_after_seeing_results_says_so():
    """A holdout is only a holdout while nobody has touched it. Five were
    touched on 2026-09-22; each records that, so its number is not read as
    an untouched one."""
    import json
    from evaluation.grader import JUDGMENTS
    raw = {c["id"]: c for c in json.loads(JUDGMENTS.read_text(encoding="utf-8"))["cases"]}
    for cid in ("h-chemists", "h2-neuro-agent", "h2-statisticians",
                "h2-energy", "h2-pathology"):
        assert raw[cid]["kind"].startswith("holdout")
        assert raw[cid].get("tuned"), f"{cid} was edited after results and must say so"
