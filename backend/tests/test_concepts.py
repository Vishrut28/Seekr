"""What a query means beyond its words: people nouns, related subjects,
research fields, and phrases found another way. Each case is a query the
evaluation (evaluation/judgments.json) measured failing."""

from rip.concepts import agent_subject, related_subjects, rewrite_agents
from rip.connectors.openalex import OpenAlexConnector
from rip.ingest import ingest_profile
from rip.nlq import parse
from rip.normalize import EvidenceItem
from tests.test_resolution import make_profile


def person(session, ext, name, *topics, fields=()):
    evidence = [EvidenceItem(attribute_type="research_interest", value=t) for t in topics]
    evidence += [EvidenceItem(attribute_type="research_field", value=f, confidence=0.4)
                 for f in fields]
    ingest_profile(session, make_profile(
        external_id=ext, url=f"https://github.com/{ext}", raw={"login": ext},
        name=name, usernames=[f"github:{ext}"], evidence=evidence))


def groups(parsed):
    return {g["term"]: (g.get("values", []), g.get("related_values", []))
            for g in parsed.skill_groups}


def test_people_nouns_name_their_subjects():
    assert agent_subject("physicists") == "physics"
    assert agent_subject("cardiologists") == "cardiology"
    assert agent_subject("epidemiologists") == "epidemiology"
    assert agent_subject("oceanographers") == "oceanography"
    assert agent_subject("physicians") == "medicine"         # not physics
    assert agent_subject("technicians") is None
    tokens, rewrites = rewrite_agents(["data", "scientists", "in", "India"])
    assert tokens == ["data", "science", "in", "India"]
    assert rewrites[0]["searched"] == "data science"


def test_physicists_reach_physics(session):
    person(session, "p", "Pat Sample", "Particle physics theoretical and experimental studies")
    parsed = parse(session, "physicists")
    assert "Particle physics theoretical and experimental studies" in groups(parsed)["physics"][0]
    assert parsed.rewrites[0] == {"typed": "physicists", "searched": "physics", "how": "people noun"}


def test_a_broad_subject_counts_related_ones_as_partial_evidence(session):
    person(session, "d", "Dee Sample", "Deep Learning")
    person(session, "n", "Nia Sample", "Advanced Neural Network Applications")
    values, related = groups(parse(session, "deep learning researchers"))["deep learning"]
    assert values == ["Deep Learning"]
    assert related == ["Advanced Neural Network Applications"]


def test_a_broad_term_the_vocabulary_lacks_is_answered_by_related_subjects(session):
    person(session, "s", "Sec Sample", "Network Security and Intrusion Detection")
    parsed = parse(session, "cybersecurity experts")
    assert groups(parsed)["cybersecurity"] == ([], ["Network Security and Intrusion Detection"])
    assert "cybersecurity" not in parsed.unmatched_terms


def test_a_known_concept_phrase_is_not_split_into_its_words(session):
    """"air pollution" became the field "Pollution" — its exact second word."""
    person(session, "a", "Aria Sample", "Air Quality and Health Impacts")
    person(session, "p", "Plas Sample", "Microplastics and Plastic Pollution", fields=["Pollution"])
    parsed = parse(session, "air pollution scientists")
    assert groups(parsed) == {"air pollution": ([], ["Air Quality and Health Impacts"])}


def test_related_subjects_never_pull_in_a_broad_field(session):
    """"drug discovery" reached the field Pharmacology, and with it migraine
    researchers."""
    person(session, "d", "Drew Sample", "Computational Drug Discovery Methods")
    person(session, "m", "Mig Sample", "Migraine and Headache Studies", fields=["Pharmacology"])
    values, related = groups(parse(session, "drug discovery"))["drug discovery"]
    assert "Pharmacology" not in values + related


def test_a_phrase_is_found_when_its_words_are_not_side_by_side(session):
    person(session, "w", "Wil Sample", "Wildlife Ecology and Conservation")
    parsed = parse(session, "wildlife conservation")
    assert groups(parsed)["wildlife conservation"][0] == ["Wildlife Ecology and Conservation"]
    assert parsed.rewrites[0]["how"] == "all words"


def test_an_abbreviation_inside_a_phrase_is_spelled_out(session):
    person(session, "h", "Hal Sample", "Artificial Intelligence in Healthcare and Education")
    parsed = parse(session, "AI in healthcare")
    assert groups(parsed)["AI in healthcare"][0] == \
        ["Artificial Intelligence in Healthcare and Education"]


def test_a_generic_head_is_dropped_but_a_generic_modifier_is_not(session):
    person(session, "c", "Cli Sample", "Climate variability and models")
    person(session, "f", "Fun Sample", "functional-programming")
    assert groups(parse(session, "climate science researchers"))["climate science"][0] == \
        ["Climate variability and models"]
    assert parse(session, "systems programming").skill_groups == []


def test_a_word_glued_to_a_generic_suffix_searches_its_stem(session):
    person(session, "n", "Nan Sample", "Copper-based nanomaterials and applications")
    person(session, "f", "Fib Sample", "Electrospun Nanofibers in Biomedical Applications")
    values, _ = groups(parse(session, "nanotechnology"))["nanotechnology"]
    assert sorted(values) == ["Copper-based nanomaterials and applications",
                              "Electrospun Nanofibers in Biomedical Applications"]


def test_a_one_word_field_widens_to_the_fields_filed_under_it(session):
    person(session, "c", "Chem Sample", "Photocatalysis", fields=["Chemistry"])
    person(session, "o", "Org Sample", "Synthesis", fields=["Organic Chemistry"])
    values, _ = groups(parse(session, "chemists"))["chemistry"]
    assert values == ["Chemistry", "Organic Chemistry"]


def test_a_one_word_topic_does_not_widen(session):
    """Widening a topic would count "Robotics 1".."Robotics 8" as depth."""
    person(session, "r", "Rob Sample", "Robotics", "Robotics in Surgery")
    values, _ = groups(parse(session, "robotics"))["robotics"]
    assert values == ["Robotics"]


def test_openalex_files_topics_under_their_subfield_and_field():
    author = {
        "id": "https://openalex.org/A1", "display_name": "Glia Researcher",
        "topics": [{"display_name": "Glioma Diagnosis and Treatment", "count": 5,
                    "subfield": {"display_name": "Oncology"},
                    "field": {"display_name": "Medicine"}}],
    }
    profile = OpenAlexConnector.__new__(OpenAlexConnector).normalize(author, [])
    assert profile.name == "Glia Researcher"          # a loop variable once overwrote it
    fields = [(e.attribute_type, e.value) for e in profile.evidence]
    assert fields == [("research_interest", "Glioma Diagnosis and Treatment"),
                      ("research_field", "Oncology"), ("research_field", "Medicine")]


def test_aliases_share_a_concept():
    assert related_subjects("cyber security") == related_subjects("cybersecurity")
    assert related_subjects("NLP") == related_subjects("natural language processing")
    assert related_subjects("underwater basket weaving") == []


def test_someone_who_states_the_subject_outranks_someone_with_a_related_one(session):
    from rip.nlq import execute

    # the related person has MORE matching topics, so only the weighting can
    # put the person who states the subject first
    person(session, "n", "Nia Related", "Advanced Neural Network Applications",
           "Convolutional Neural Networks", "Transformer Models")
    person(session, "d", "Dee Stated", "Deep Learning")
    ranked = [p.canonical_name for p in execute(session, parse(session, "deep learning"))]
    assert ranked == ["Dee Stated", "Nia Related"]


def test_a_subject_with_the_word_in_it_is_not_the_subject():
    """"web services" and "semantic web" were listed as related to web
    development. Both have the word in them and neither is it: the first
    reaches "Service-Oriented Architecture and Web Services" and the second
    "Semantic Web and Ontologies", academic subjects whose people carry the
    citation counts of a career. They took the first NINE places for "web
    developers" -- database and software-engineering professors, every one
    graded 0 -- and pushed the people whose topics are css, angular and jquery
    to tenth. nDCG 0.199, now 0.943."""
    related = related_subjects("web development")
    assert "web services" not in related
    assert "semantic web" not in related
    for real in ("html", "css", "javascript", "jquery", "php"):
        assert real in related
    assert related_subjects("web") == related      # the alias goes to the same place
