"""Records that are not people never become people; page titles become names."""

import pytest

from rip.ingest import ingest_profile
from rip.normalize import EvidenceItem, NormalizedProfile
from rip.nlq import execute, parse
from rip.personhood import NotAPerson, assess


@pytest.mark.parametrize("title, name", [
    ("Dhruv Dixit's Profile | YMGrad", "Dhruv Dixit"),
    ("Rahul Jain Portfolio", "Rahul Jain"),
    ("Rahul's | Portfolio Website", "Rahul"),
    ("My Fieldwire Journey: Rahul Joshi", "Rahul Joshi"),
    ("Amina Diallo — Mytrova", "Amina Diallo"),
    ("Rahul Ratakonda – Forensic Structural Engineering Expert", "Rahul Ratakonda"),
    ("Neha J. - Online Tutor in Whitefield, Bangalore for Python Training", "Neha J."),
    ("Andrew Yuan", "Andrew Yuan"),
])
def test_page_titles_of_real_people_become_their_names(title, name):
    verdict = assess(title, "web")
    assert verdict.is_person and verdict.name == name


@pytest.mark.parametrize("title", [
    "20+ Deep Learning Projects for Beginners with Source Code",
    "20 Machine Learning Projects to Boost Your Portfolio (2026) | Udacity",
    "Top 10 C++ Language Classes near Manayata Tech Park, Bangalore - UrbanPro.com",
    "Senior Software Engineer - C/C++ Networking & Security in Bangalore, India | Pro",
    "Careers at Millennium",
    "GCSAYN Annual Forum",
    "Machine Learning Professional Portfolio",
    "Automated Candidate Screening Software for Recruiting Agencies | Jarvi ATS",
    "Python Developer Portfolio Guide: Make Your Python Developer Portfolio",
])
def test_pages_about_things_are_not_people(title):
    assert not assess(title, "web").is_person


@pytest.mark.parametrize("name, source", [
    ("Deep Learning Türkiye", "github"),
    ("Awesome machine learning deep learning libraries", "github"),
    ("D. .", "semanticscholar"),
    ("p", "huggingface"),
])
def test_communities_and_degenerate_names_are_not_people(name, source):
    assert not assess(name, source).is_person


@pytest.mark.parametrize("name", [
    # real names that collide with ordinary words must survive a source that
    # states a name — rejecting a real person silently loses them
    "Deep Singh", "Learning Chen", "Ravi Community", "Grace Hopper", "DJ Patil",
    "Jean-Luc Godard", "Mary O'Neil", "A. Zisserman", "José García", "Zhang San",
    "Still Active", "Person 5",
])
def test_unusual_real_names_are_kept_verbatim(name):
    verdict = assess(name, "github")
    assert verdict.is_person and verdict.name == name


def test_decoration_is_removed_from_stated_names():
    assert assess("SAHAYA MERCY A PhD (Full Time Research Scholar) St. Joseph's College",
                  "semanticscholar").name == "Sahaya Mercy A"
    assert assess("Rahul Gupta - Humanitarian • Social Worker • Helping People", "orcid").name == "Rahul Gupta"


def _page(external_id, title):
    return NormalizedProfile(source="web", source_type="web", external_id=external_id,
                             url=external_id, raw={"title": title}, name=title,
                             evidence=[EvidenceItem(attribute_type="bio", value="deep learning")])


def test_ingest_refuses_a_non_person_before_writing_anything(session):
    from rip.models import Person, SourceRecord

    with pytest.raises(NotAPerson):
        ingest_profile(session, _page("https://x/list", "20+ Deep Learning Projects for Beginners"))
    assert session.query(Person).count() == 0
    assert session.query(SourceRecord).count() == 0


def test_ingest_stores_a_page_title_as_the_persons_name(session):
    person = ingest_profile(session, _page("https://ymgrad.com/dd", "Dhruv Dixit's Profile | YMGrad"))
    assert person.canonical_name == "Dhruv Dixit"
    assert "profile" not in " ".join(person.aliases).lower()


def test_a_legacy_community_record_cannot_turn_a_topic_into_a_name_filter(session):
    """Records stored before the gate existed must not hijack the parser."""
    from rip.models import Person

    ingest_profile(session, NormalizedProfile(
        source="github", source_type="code", external_id="real", url="https://github.com/real",
        raw={}, name="Mahmoud Badry",
        evidence=[EvidenceItem(attribute_type="skill", value="Deep Learning")]))
    # simulate an old row that the gate would now refuse
    legacy = Person(canonical_name="Deep Learning Türkiye")
    session.add(legacy)
    session.commit()
    parsed = parse(session, "deep learning researchers")
    assert parsed.name_terms == []
    assert [p.canonical_name for p in execute(session, parsed)] == ["Mahmoud Badry"]


@pytest.mark.parametrize("name", [
    # the record that prompted this: OpenAlex credits the publisher as an
    # author, with 73 works and a conflation score of 0.40
    "Verlag Hans Huber",
    "Verlag Hans Huber (Bern)",
    "Springer Publishing",
    "Karger Publishers",
    "Wiley Publishers",
    "Hogrefe Publications",
    "Oxford University Press",          # caught by "university", not "press"
    "MIT Press Ltd",                    # caught by "ltd", not "press"
])
def test_publishers_credited_as_authors_are_not_people(name):
    assert not assess(name, "openalex").is_person


@pytest.mark.parametrize("name", [
    # "press" is a surname and must survive. William H. Press wrote Numerical
    # Recipes; rejecting him to catch a publisher is the wrong trade, because
    # a rejected person is lost silently and a kept publisher is visible.
    "William H. Press", "Frank Press", "S. Press",
])
def test_a_person_called_press_is_still_a_person(name):
    verdict = assess(name, "openalex")
    assert verdict.is_person and verdict.name == name


def test_a_publisher_is_not_offered_as_an_author():
    """The publisher is already in the corpus with 25 papers, and the gate
    only guards new ingests. nlq screens author results through the same
    has_entity_signal, so adding the word closes both doors at once and the
    record can no longer be returned as a person.

    A real name inside the string is still a real name -- searching "verlag
    hans huber" does find Hans Huber, correctly -- so this pins the narrower
    thing that has to hold: the publisher's own name never passes as an
    author's, while the name it is built around still does.

    Not asserted here: nlq's own NOT_A_PERSON pattern has always listed
    "press", so _looks_like_a_person already rejects William H. Press. That
    is a separate and stricter policy for filtering live index results, and
    personhood.assess -- which decides what gets STORED -- correctly keeps
    him. See test_a_person_called_press_is_still_a_person.
    """
    from rip.nlq import _looks_like_a_person

    assert not _looks_like_a_person("Verlag Hans Huber")
    assert not _looks_like_a_person("Verlag Hans Huber (Bern)")
    assert _looks_like_a_person("Hans Huber")


def test_ingest_now_refuses_the_publisher_that_got_through(session):
    """The exact record and external id that reached the corpus."""
    from rip.personhood import NotAPerson

    with pytest.raises(NotAPerson) as caught:
        ingest_profile(session, NormalizedProfile(
            source="openalex", source_type="scholarly", external_id="A5081967324",
            url="https://openalex.org/A5081967324", raw={},
            name="Verlag Hans Huber",
            evidence=[EvidenceItem(attribute_type="research_interest",
                                   value="Haemochromatosis")]))
    assert "verlag" in str(caught.value)
