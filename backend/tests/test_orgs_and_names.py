"""Organizations by the names people type, and misspelled names and phrases.
Each case is a query the evaluation measured failing."""

from rip.ingest import ingest_profile
from rip.nlq import execute_progressive, parse
from rip.normalize import EvidenceItem, OrgAffiliation
from tests.test_resolution import make_profile


def person(session, ext, name, orgs=(), topics=()):
    ingest_profile(session, make_profile(
        external_id=ext, url=f"https://github.com/{ext}", raw={"login": ext},
        name=name, usernames=[f"github:{ext}"],
        organizations=[OrgAffiliation(name=o) for o in orgs],
        evidence=[EvidenceItem(attribute_type="research_interest", value=t) for t in topics]))


def found(session, query):
    rows, _parsed, _dropped = execute_progressive(session, parse(session, query))
    return sorted(p.canonical_name for p in rows)


def test_one_word_of_an_institution_name_finds_it(session):
    """"researchers at Stanford" returned nobody with four Stanford people stored."""
    person(session, "a", "Ann Stanford-Person", orgs=["Stanford University"])
    person(session, "b", "Bob Elsewhere", orgs=["University of Oxford"])
    assert found(session, "researchers at Stanford") == ["Ann Stanford-Person"]


def test_every_stored_spelling_of_a_company_counts(session):
    person(session, "a", "Ada Us", orgs=["Google (United States)"])
    person(session, "b", "Ben Uk", orgs=["Google (United Kingdom)"])
    person(session, "c", "Cy Deep", orgs=["Google DeepMind (United Kingdom)"])
    person(session, "d", "Di Other", orgs=["Microsoft Research"])
    assert found(session, "people at Google") == ["Ada Us", "Ben Uk", "Cy Deep"]
    assert found(session, "Google DeepMind researchers") == ["Cy Deep"]


def test_a_company_word_in_skill_tags_does_not_split_the_phrase(session):
    """"google" is a word of Stack Overflow tags (google-chrome), which made the
    whole phrase "Google DeepMind" read as a subject and never match. With an
    organization called plain "Google" stored, "Google DeepMind researchers"
    then returned Google's engineers alongside DeepMind's."""
    person(session, "a", "Ada Plain", orgs=["Google"])
    person(session, "c", "Cy Deep", orgs=["Google DeepMind (United Kingdom)"])
    person(session, "t", "Tag User", topics=["google-chrome"])
    assert found(session, "Google DeepMind researchers") == ["Cy Deep"]


def test_institutions_are_found_by_their_abbreviations(session):
    person(session, "a", "Ann Mit", orgs=["Massachusetts Institute of Technology"])
    person(session, "b", "Bob Iitb", orgs=["Indian Institute of Technology Bombay"])
    person(session, "c", "Cy Iitd", orgs=["Indian Institute of Technology Delhi"])
    person(session, "d", "Di Aiims", orgs=["All India Institute of Medical Sciences"])
    assert found(session, "MIT") == ["Ann Mit"]
    assert found(session, "researchers at IIT Bombay") == ["Bob Iitb"]
    assert found(session, "AIIMS doctors") == ["Di Aiims"]


def test_a_subject_word_is_not_an_institution_unless_asked_at_one(session):
    """Otherwise the Robotics Institute would answer every robotics question."""
    person(session, "a", "Ann Robot", topics=["Robotics in Surgery"])
    person(session, "b", "Bob Lee", orgs=["Robotics Institute"])
    parsed = parse(session, "robotics")
    assert parsed.organizations == [] and found(session, "robotics") == ["Ann Robot"]
    assert parse(session, "researchers at Robotics Institute").organizations == ["Robotics Institute"]


def test_two_slips_in_a_long_word_are_repaired_when_the_rest_is_right(session):
    """"partical" is two edits from "particle"; "physics" says which was meant."""
    person(session, "a", "Ann Physics", topics=["Particle physics theoretical and experimental studies"])
    parsed = parse(session, "partical physics")
    assert parsed.corrections and parsed.corrections[0]["typed"] == "partical"
    assert found(session, "partical physics") == ["Ann Physics"]
    # on its own, two edits is too far to guess
    assert parse(session, "partical").corrections == []


def test_a_misspelled_surname_is_corrected_against_that_first_name(session):
    """"Dhruv Dixt" returned every Dhruv in the graph."""
    person(session, "a", "Dhruv Dixit")
    person(session, "b", "Dhruv Kumar")
    person(session, "c", "Rahul Agarwal")
    assert found(session, "Dhruv Dixt") == ["Dhruv Dixit"]
    parsed = parse(session, "Rahul Agarwl")
    assert sorted(parsed.name_terms, key=str.lower) == ["agarwal", "Rahul"]
    assert parsed.unmatched_terms == []


def test_both_does_not_hide_the_word_at_from_the_organization_matcher(session):
    """"at both Google and Stanford" saw no "at" before Google, so a word that
    is also subject vocabulary was read as a subject: the query came back as a
    list of google-chrome users."""
    person(session, "a", "Ann Both", orgs=["Google (United States)", "Stanford University"],
           topics=["Robotics"])
    person(session, "b", "Bob Chrome", topics=["google-chrome", "google-app-engine"])
    parsed = parse(session, "researchers at both Google and Stanford")
    assert parsed.skill_groups == []
    assert parsed.require_all_orgs
    assert found(session, "researchers at both Google and Stanford") == ["Ann Both"]


def test_an_employer_nobody_works_for_is_reported_not_reinterpreted(session):
    person(session, "b", "Bob Chrome", topics=["google-chrome", "google-app-engine"])
    parsed = parse(session, "engineers at Google")
    assert parsed.skill_groups == [] and parsed.organizations == []
    assert parsed.unmatched_terms == ["Google"]
    assert found(session, "engineers at Google") == []


def test_a_lowercase_word_after_at_is_not_an_employer(session):
    """The employer rule needs the capital letter people give a company, or
    "engineers at kubernetes" would report a subject as a missing employer."""
    # a word no topic is spelled exactly, so only containment can match it —
    # the same route "at Google" takes to google-chrome
    person(session, "a", "Ann Reef", topics=["Coral Reef Restoration"])
    person(session, "b", "Bob Vision", topics=["Computer Vision", "Robotics", "Cosmology"])
    parsed = parse(session, "researchers at coral")
    assert parsed.unmatched_terms == []
    assert [g["term"] for g in parsed.skill_groups] == ["coral"]
    assert found(session, "researchers at coral") == ["Ann Reef"]
