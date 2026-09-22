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


# The soft fallback: "nobody is a deep learning researcher at Oxford, so the
# best answer left is a deep learning researcher". It has to mean the subject
# they STATE -- and when nobody meets the query in full, the score that comes
# out is about the subject, not the constraint, and has to say so.

def test_the_soft_fallback_needs_the_subject_stated_not_merely_published_on():
    c = case(strong=["deep learning"], org=["oxford"], soft=True)
    titles = ["Deep learning for genomes", "Deep learning for proteins"]
    assert grade(c, person(topics=["Deep learning"], orgs=["Oxford"])) == 2
    assert grade(c, person(texts=titles, orgs=["Oxford"])) == 1      # constraint holds
    assert grade(c, person(topics=["Deep learning"], orgs=["MIT"])) == 1   # subject stated
    # neither at Oxford nor a stated deep learning researcher: two levels of
    # weak evidence used to add up to relevant, and 61 people rode in on it
    assert grade(c, person(texts=titles, orgs=["MIT"])) == 0


def test_a_soft_query_nobody_meets_in_full_reports_what_it_measured():
    c = case(strong=["deep learning"], org=["oxford"], soft=True)
    profiles = {
        "a": person("a", topics=["Deep learning"], orgs=["MIT"]),
        "b": person("b", topics=["Deep learning"], orgs=["Berkeley"]),
        "ox": person("ox", topics=["Medieval history"], orgs=["Oxford"]),
    }
    row = score_case(c, ["a", "b"], profiles)
    assert row["subject_only"] is True
    assert row["meets_constraint"] == 1     # someone IS at Oxford: not an empty corpus
    assert row["ndcg_at_10"] == 1.0         # and any order of the two scores the same
    assert summarize([row])["measuring_the_subject_only"] == [c.id]


def test_a_soft_query_someone_does_meet_measures_the_constraint_normally():
    c = case(strong=["drug discovery"], country="IN", soft=True)
    profiles = {
        "in": person("in", country="IN", topics=["Drug discovery"]),
        "us": person("us", country="US", topics=["Drug discovery"]),
    }
    assert score_case(c, ["in", "us"], profiles)["subject_only"] is False
    # and the constraint is what the ranking is tested on
    assert score_case(c, ["in", "us"], profiles)["ndcg_at_10"] == 1.0
    assert score_case(c, ["us", "in"], profiles)["ndcg_at_10"] < 1.0


def test_a_query_the_corpus_cannot_answer_is_named_not_averaged_away():
    """Every figure is None, so it vanishes into the means and sits in the set
    looking like a measurement. n-aggarwal did that for 89 queries."""
    profiles = {"a": person("a", topics=["Cosmology"])}
    rows = [score_case(case(strong=["headache"]), ["a"], profiles),
            score_case(case(id="ok", strong=["cosmology"]), ["a"], profiles)]
    assert rows[0]["relevant_in_corpus"] == 0 and rows[0]["ndcg_at_10"] is None
    assert summarize(rows)["grading_nobody"] == ["x"]


# `related` is the same defect one level down. A related subject is one NEXT
# to the query, worth partial credit; a PARENT CATEGORY is not, because it
# admits every sibling. Measured across the set, eleven queries had a single
# related term admitting half or more of everyone they counted.

def test_a_neighbouring_field_is_not_a_neighbouring_subject():
    cases = real_cases()
    tb = person(topics=["Infectious disease and tuberculosis"])
    ebola = person(topics=["Ebola virus", "Global health"])
    # fungal infection is ONE KIND of infectious disease, so the parent
    # admitted every tuberculosis, Ebola and leprosy researcher: 11 of 15
    assert grade(cases["h-fungal"], tb) == 0
    assert grade(cases["h-fungal"], ebola) == 0
    assert grade(cases["h-fungal"], person(topics=["Aspergillus"])) > 0


def test_a_parent_category_does_not_admit_every_sibling():
    cases = real_cases()
    # "machine learning" made computer-vision and NLP people partially
    # relevant to reinforcement learning -- 45 of the 70 it counted
    ml = person(topics=["Machine learning and data classification"])
    assert grade(cases["h2-rl"], ml) == 0
    assert grade(cases["h2-typo-rl"], ml) == 0
    assert grade(cases["h2-rl"], person(topics=["Reinforcement learning in robotics"])) == 2
    # a graph neural network IS a neural network, and the parent admitted all
    # of them: 24 of 47
    assert grade(cases["h2-gnn"], person(topics=["Advanced neural network application"])) == 0
    # ecology is the parent field of wildlife conservation: 10 of 20, including
    # insect, polar and marine ecologists
    assert grade(cases["t-wildlife"], person(topics=["Echinoderm biology and ecology"])) == 0
    # and epidemiology made every epidemiologist relevant to tuberculosis
    assert grade(cases["t-tb"], person(topics=["Clinical epidemiology"])) == 0


def test_adjacency_that_is_real_is_kept():
    """The rule is not "drop every related term". Topic modelling is a core
    information-retrieval technique; cosmology and particle physics genuinely
    overlap; robotics is where reinforcement learning is applied."""
    cases = real_cases()
    assert grade(cases["h-ir"], person(topics=["Topic modeling"])) == 1
    assert grade(cases["t-cosmology"],
                 person(topics=["Particle physics theoretical study"])) == 1
    assert grade(cases["h2-rl"], person(topics=["Robotics"])) == 1


def test_a_query_the_search_code_was_changed_for_says_so(capsys):
    """`tuned` covers a criterion edited after seeing results, `covered` a
    subject ingested after a query failed. This is the third way a number can
    stop meaning what it looks like: the SEARCH CODE changed knowing the
    query. Four holdout queries drove concept-map entries, and holdout nDCG
    went 0.735 -> 0.863 -- most of it from exactly those four."""
    import json

    from evaluation.grader import JUDGMENTS
    raw = {c["id"]: c for c in json.loads(JUDGMENTS.read_text(encoding="utf-8"))["cases"]}
    for cid in ("h-alz", "h-evs", "h-fungal", "h-toxicology"):
        assert raw[cid].get("informed"), f"{cid} drove a concept entry and must say so"
    from evaluation.grader import load_cases

    cases = {c.id: c for c in load_cases()}
    assert cases["h-alz"].informed
    assert not cases["t-nlp"].informed


def test_the_possessive_in_a_disease_name_is_not_part_of_the_subject():
    """"Alzheimer's" stemmed to "alzheimer s", matched no concept key, and the
    query reached only people whose topic contained the word itself. Diseases
    are routinely named this way."""
    from rip.concepts import _key, related_subjects

    assert _key("Alzheimer's") == _key("alzheimer") == "alzheimer"
    assert related_subjects("Alzheimer's") == related_subjects("alzheimer")
    assert related_subjects("Alzheimer's")
    # and a word that simply ends in s is not a possessive
    assert _key("systems") != "system"[:0]
    assert _key("graph neural networks")
