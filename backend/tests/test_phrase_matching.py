"""A phrase the corpus does not know must not be answered with one of its words.

From the benchmark on the real graph: "graph neural networks" came back as
wireless sensor networks and VANETs, and "low-level systems programming" as
functional-programming — confident answers to questions nobody asked.
"""

from rip.ingest import ingest_profile
from rip.nlq import parse
from rip.normalize import EvidenceItem
from tests.test_resolution import make_profile


def person(session, ext, name, *skills):
    ingest_profile(session, make_profile(
        external_id=ext, url=f"https://github.com/{ext}", raw={"login": ext},
        name=name, usernames=[f"github:{ext}"],
        evidence=[EvidenceItem(attribute_type="research_interest", value=s) for s in skills]))


def seed(session):
    person(session, "a", "Ada Example", "Neural Networks and Applications")
    person(session, "b", "Bo Sample", "Energy Efficient Wireless Sensor Networks")
    person(session, "c", "Cy Instance", "Vehicular Ad Hoc Networks (VANETs)")
    person(session, "d", "Di Case", "functional-programming")


def skills_of(parsed):
    return [v for group in parsed.skill_groups for v in group.get("values", [])]


def test_the_longest_sub_phrase_wins_over_a_bare_word(session):
    """"graph neural networks" is unknown; "neural networks" is not, and it
    sits one token to the right of the miss that used to hide it."""
    seed(session)
    parsed = parse(session, "graph neural networks")
    assert skills_of(parsed) == ["Neural Networks and Applications"]
    assert "Energy Efficient Wireless Sensor Networks" not in skills_of(parsed)
    assert parsed.unmatched_terms == ["graph neural networks"]


def test_one_word_of_an_unknown_phrase_is_not_a_filter(session):
    seed(session)
    parsed = parse(session, "low-level systems programming")
    assert skills_of(parsed) == []          # not functional-programming
    assert parsed.unmatched_terms == ["low-level systems programming"]


def test_a_dropped_phrase_is_reported_whole_and_once(session):
    seed(session)
    parsed = parse(session, "content delivery networks at the edge")
    assert parsed.unmatched_terms.count("content delivery networks") == 1
    # no fragments of it reported alongside the phrase itself
    assert not {"content delivery", "delivery networks", "networks", "content"} \
        & set(parsed.unmatched_terms)
    assert skills_of(parsed) == []


def test_a_word_that_is_real_vocabulary_survives_an_unknown_phrase(session):
    """"Python developers" is unknown as a phrase; Python is not a casualty."""
    person(session, "e", "Eve Sample", "Python")
    parsed = parse(session, "senior Python developers")
    assert skills_of(parsed) == ["Python"]


def test_a_person_named_after_a_topic_word_does_not_hijack_the_phrase(session):
    """The real graph holds a GitHub account display-named "Graph". That made
    "graph neural networks" a name search, which dropped the phrase and left
    the bare word to answer it with graph theory."""
    seed(session)
    person(session, "g", "Graph", "SVG animation")
    person(session, "h", "Hema Rao", "Advanced Graph Theory Research")
    parsed = parse(session, "graph neural networks")
    assert skills_of(parsed) == ["Neural Networks and Applications"]
    assert parsed.name_terms == []
    assert parsed.unmatched_terms == ["graph neural networks"]


def test_a_phrase_that_is_a_real_name_is_still_a_name_search(session):
    seed(session)
    person(session, "i", "Rahul Bhadja", "Soil Microbiology")
    parsed = parse(session, "rahul bhadja")
    assert parsed.name_terms == ["rahul bhadja"] and skills_of(parsed) == []


def test_a_typo_inside_a_phrase_is_repaired(session):
    """"distributed sytems" used to apply the bare word "distributed" and
    report the rest as dropped: the per-token typo pass compares a word
    against whole vocabulary values, so it cannot see a slip inside a
    phrase."""
    person(session, "j", "Jo Sample", "Distributed Systems")
    parsed = parse(session, "distributed sytems engineers")
    assert skills_of(parsed) == ["Distributed Systems"]
    assert parsed.corrections == [{"typed": "sytems", "matched": "Distributed Systems"}]
    assert parsed.unmatched_terms == []


def test_two_typos_in_a_longer_phrase_are_repaired_together(session):
    """"natual language procesing" fixed neither word alone, fell back to
    "natual language", and dropped "procesing" without saying so. With a word
    spelled right to pin the phrase down, both slips are repaired, and the
    correction names the whole phrase as typed."""
    person(session, "n", "Nell Sample", "Natural Language Processing")
    parsed = parse(session, "natual language procesing")
    assert skills_of(parsed) == ["Natural Language Processing"]
    assert [g["term"] for g in parsed.skill_groups] == ["natual language procesing"]
    assert parsed.corrections == [{"typed": "natual language procesing",
                                   "matched": "Natural Language Processing"}]
    assert parsed.unmatched_terms == []


def test_two_typos_with_nothing_spelled_right_are_not_guessed(session):
    """Two words, both misspelled: nothing pins down what was meant, so no
    pair of guesses is tried."""
    person(session, "n", "Nell Sample", "Natural Language Processing")
    person(session, "m", "Mo Sample", "Machine Learning")
    parsed = parse(session, "machin lerning")
    assert skills_of(parsed) == []


def test_a_transposition_is_repaired(session):
    """One transposition is two edits to Levenshtein and one to Damerau, and
    it is the commonest typo there is."""
    seed(session)
    person(session, "k", "Ken Sample", "Robotics")     # before the first parse:
    # the query vocabulary is cached per database, and only live search, which
    # stores people mid-request, invalidates it by hand
    assert skills_of(parse(session, "neural netowrks")) == \
        ["Neural Networks and Applications"]
    assert skills_of(parse(session, "robotcis")) == ["Robotics"]


def test_a_repair_that_matches_nothing_is_not_applied(session):
    """A guess is only used when it makes the phrase match something real."""
    seed(session)
    parsed = parse(session, "xyzzy plughs")
    assert skills_of(parsed) == [] and parsed.corrections == []


def test_singular_and_plural_reach_the_same_people(session):
    """A user typing the singular used to get nothing at all — and either form
    reached only the values spelled its way: "neural network" found "Neural
    Network" and missed "Neural Networks and Applications" beside it."""
    person(session, "l", "Lee Sample", "Advanced Neural Network Applications")
    person(session, "m", "Mo Sample", "Neural Networks and Applications")
    person(session, "n", "Nia Sample", "Distributed Systems")
    both = ["Advanced Neural Network Applications", "Neural Networks and Applications"]
    assert sorted(skills_of(parse(session, "neural network"))) == both
    assert sorted(skills_of(parse(session, "neural networks"))) == both
    assert skills_of(parse(session, "distributed system")) == ["Distributed Systems"]


def test_a_phrase_joined_by_a_connective_does_not_block_its_terms(session):
    """"people in robotics" worked and "women in robotics" did not: the three
    words were read as one unknown phrase, which blocked "robotics"."""
    person(session, "rb", "Rob Sample", "Reinforcement Learning in Robotics")
    assert skills_of(parse(session, "exoskeletons in robotics")) == \
        ["Reinforcement Learning in Robotics"]


def test_a_generic_academic_word_names_no_subject_on_its_own(session):
    """"LLM evaluation" matched "Textile materials and evaluations"."""
    person(session, "tx", "Tex Sample", "Textile materials and evaluations")
    person(session, "ct", "Cat Sample", "Cancer Treatment and Pharmacology")
    parsed = parse(session, "LLM evaluation")
    assert skills_of(parsed) == [] and "evaluation" in parsed.unmatched_terms
    # as part of a phrase it still means something
    assert skills_of(parse(session, "cancer treatment")) == ["Cancer Treatment and Pharmacology"]


def test_a_value_that_is_exactly_a_generic_word_still_matches(session):
    person(session, "ev", "Eve Sample", "Evaluation")
    assert skills_of(parse(session, "evaluation")) == ["Evaluation"]


def test_plurals_meet_their_singulars_whatever_the_ending():
    """"diseases" became "diseas" and never met "disease"."""
    from rip.textnorm import singular

    for plural, one in [("diseases", "disease"), ("databases", "database"),
                        ("analyses", "analysis"), ("hypotheses", "hypothesis"),
                        ("processes", "process"), ("approaches", "approach"),
                        ("caches", "cache"), ("boxes", "box"), ("sizes", "size"),
                        ("studies", "study"), ("responses", "response")]:
        assert singular(plural) == singular(one), (plural, one)
    assert [singular(w) for w in ("c++", "analysis", "business", "ios")] == \
        ["c++", "analysis", "business", "ios"]


def test_people_are_never_selected_by_a_protected_attribute(session):
    person(session, "rb", "Rob Sample", "Reinforcement Learning in Robotics")
    person(session, "wh", "Wendy Sample", "Women's Health")
    parsed = parse(session, "women in robotics")
    assert skills_of(parsed) == ["Reinforcement Learning in Robotics"]
    assert parsed.protected_terms == [{"term": "women", "attribute": "gender"}]
    for query, attribute in (("female engineers", "gender"), ("young founders", "age"),
                             ("muslim doctors", "religion"), ("dalit engineers", "ethnicity")):
        assert [t["attribute"] for t in parse(session, query).protected_terms] == [attribute]


def test_a_protected_word_that_does_not_describe_people_is_left_alone(session):
    person(session, "wh", "Wendy Sample", "Women's Health")
    assert skills_of(parse(session, "women's health researchers")) == ["Women's Health"]
    for query in ("Christian Rabl", "white box testing", "male infertility", "black holes"):
        assert parse(session, query).protected_terms == [], query


def test_contains_phrase_does_not_care_what_kind_of_sequence_it_is_given():
    """It compared the phrase against a LIST SLICE of the haystack, so a
    tuple never matched: `("new", "york")` returned False where
    `["new", "york"]` returned True, while a one-word tuple kept working
    because that branch uses `in`. Right on the easy case and silently wrong
    on the hard one — and every caller in the tree happened to pass lists,
    so nothing failed until a new one did not."""
    from rip.textnorm import contains_phrase

    hay = ["i", "live", "in", "new", "york", "city"]
    for phrase in (["new", "york"], ("new", "york"), iter(["new", "york"])):
        assert contains_phrase(hay, phrase), phrase
    for phrase in (["york", "new"], ("york", "new")):
        assert not contains_phrase(hay, phrase), "order still has to hold"
    assert contains_phrase(tuple(hay), ("new", "york", "city"))
    assert not contains_phrase(hay, ())
