"""People who work on a subject without stating it: the route through their
papers, and the two concept-map gaps the relevance labels found.

Independent labels (evaluation/relevance_labels.json) showed search never
finding people whose subject is in their papers and not their stated topics:
three Indian machine-learning researchers among them. A person now matches a
subject when at least two of their papers name it, title or the paper's own
topics -- the rule ingest.on_subject and the benchmark grader already use --
and is ranked after everyone who states it.
"""

from sqlalchemy import select

from rip import search_index as si
from rip.concepts import related_subjects
from rip.ingest import ingest_profile
from rip.models import Authorship, Person, Publication
from rip.nlq import count_matches, execute, parse, relevance_scores
from rip.normalize import EvidenceItem, PublicationData
from tests.test_resolution import make_profile


def person(session, ext, name, topics=(), papers=(), cites=0):
    ingest_profile(session, make_profile(
        source="openalex", source_type="scholarly", external_id=ext,
        url=f"https://openalex.org/{ext}", raw={"id": ext}, name=name, usernames=[],
        evidence=[EvidenceItem(attribute_type="research_interest", value=t) for t in topics],
        publications=[PublicationData(title=title, external_id=f"{ext}-{i}", citations=cites,
                                      topics=list(paper_topics))
                      for i, (title, paper_topics) in enumerate(papers)]))
    session.commit()


def names(rows):
    return [p.canonical_name for p in rows]


def crypto_corpus(session):
    person(session, "a", "Ada Stated", topics=["Cryptography"])
    person(session, "b", "Bea Papers", cites=50000, papers=[
        ("Lattice cryptography for the post-quantum era", ()),
        ("Side channels in applied cryptography", ()),
        ("A survey of garbage collectors", ())])
    person(session, "c", "Cy Once", cites=50000, papers=[
        ("Cryptography in one chapter", ()), ("Compilers", ())])


def test_two_papers_on_a_subject_let_someone_in_and_one_does_not(session):
    crypto_corpus(session)
    found = names(execute(session, parse(session, "cryptography")))
    assert "Bea Papers" in found
    assert "Cy Once" not in found


def test_people_found_through_papers_rank_after_everyone_who_states_it(session):
    """However cited they are: output counts their papers, and that alone put a
    breast pathologist second for "computational pathology"."""
    crypto_corpus(session)
    parsed = parse(session, "cryptography")
    assert names(execute(session, parsed)) == ["Ada Stated", "Bea Papers"]
    ids = {p.canonical_name: p.id for p in session.query(Person).all()}
    scores = relevance_scores(session, parsed, [ids["Ada Stated"], ids["Bea Papers"]])
    assert scores[ids["Bea Papers"]].get("papers_only") is True
    assert "papers_only" not in scores[ids["Ada Stated"]]


def test_a_paper_topic_counts_like_a_title(session):
    person(session, "a", "Ada Stated", topics=["Cryptography"])
    person(session, "t", "Tia Topics", papers=[
        ("On lattices", ("Cryptography and coding",)),
        ("On codes", ("Cryptography and coding",))])
    assert "Tia Topics" in names(execute(session, parse(session, "cryptography")))


def test_the_phrase_must_be_whole_inside_one_paper(session):
    """Pairs alone cannot say "in the same paper": "AI in healthcare" was once
    found as "ai in" from one paper's topic and "in healthcare" from another's,
    for someone who works on fairness in AI."""
    person(session, "e", "Eve Stated", topics=["Natural Language Processing"])
    person(session, "d", "Dan Halves", papers=[
        ("Natural language models one", ()), ("Natural language models two", ()),
        ("Language processing in the brain", ()), ("Language processing in birds", ())])
    found = names(execute(session, parse(session, "natural language processing")))
    assert found == ["Eve Stated"]


def test_index_and_fallback_agree_on_the_paper_route(session, monkeypatch):
    crypto_corpus(session)
    person(session, "d", "Dan Halves", papers=[
        ("Natural language models one", ()), ("Natural language models two", ()),
        ("Language processing in the brain", ()), ("Language processing in birds", ())])
    person(session, "e", "Eve Stated", topics=["Natural Language Processing"])
    queries = ["cryptography", "natural language processing"]
    indexed = {q: (names(execute(session, parse(session, q))), count_matches(session, parse(session, q)))
               for q in queries}
    monkeypatch.setattr("rip.nlq.si.is_ready", lambda s: False)
    fallback = {q: (names(execute(session, parse(session, q))), count_matches(session, parse(session, q)))
                for q in queries}
    assert indexed == fallback


def test_a_paper_added_later_reaches_the_index(session):
    """Authorships feed the index now, so they are tracked like evidence."""
    crypto_corpus(session)
    cy = session.execute(select(Person).where(Person.canonical_name == "Cy Once")).scalar_one()
    assert "cryptography" not in {t.term for t in session.query(si.SearchTerm)
                                  .filter_by(person_id=cy.id, field="w")}
    paper = Publication(title="Cryptography, the second time")
    session.add(paper)
    session.flush()
    session.add(Authorship(person_id=cy.id, publication_id=paper.id))
    session.commit()
    assert "Cy Once" in names(execute(session, parse(session, "cryptography")))


def test_a_misspelt_subject_gets_the_related_subjects_of_the_right_spelling(session):
    person(session, "n", "Nell Stated", topics=["Natural Language Processing"])
    person(session, "m", "Mo Related", topics=["Topic Modeling"])
    typo = {g["term"]: g.get("related_values") for g in parse(session, "natual language processing").skill_groups}
    assert typo == {"natual language processing": ["Topic Modeling"]}


def test_a_repaired_typo_reaches_what_the_right_spelling_reaches(session):
    """"cosmolgy" was repaired to its topic, then matched papers and the
    concept map as "cosmolgy": one person, where the right spelling found the
    dark-matter people and the ones who only publish on it."""
    person(session, "s", "Sky Stated", topics=["Cosmology and Gravitation Theories"])
    person(session, "d", "Dee Dark", topics=["Dark Matter and Cosmic Phenomena"])
    person(session, "p", "Pat Papers", papers=[
        ("Cosmology with weak lensing", ()), ("Inflationary cosmology revisited", ())])
    typo = names(execute(session, parse(session, "cosmolgy")))
    assert typo == names(execute(session, parse(session, "cosmology")))
    assert {"Sky Stated", "Dee Dark", "Pat Papers"} <= set(typo)


def test_a_repaired_typo_says_what_was_typed_and_what_was_searched(session):
    person(session, "s", "Sky Stated", topics=["Cosmology and Gravitation Theories"])
    person(session, "d", "Dee Dark", topics=["Dark Matter and Cosmic Phenomena"])
    rewrite = next(r for r in parse(session, "cosmolgy").rewrites if r["how"] == "related subjects")
    assert rewrite["typed"] == "cosmolgy"
    assert rewrite["searched"] == "cosmology + related subjects"


def test_a_term_does_not_borrow_the_related_subjects_of_a_different_topic(session):
    """"computational" resolves to "Computational Biology" too, and borrowing
    its related subjects sent "computational pathology" to genomics."""
    person(session, "b", "Bo Comp", topics=["Computational Biology"])
    person(session, "g", "Gil Genes", topics=["Genomics"])
    person(session, "p", "Pia Path", topics=["Pathology"])
    related = [v for g in parse(session, "computational pathology").skill_groups
               for v in g.get("related_values") or []]
    assert "Genomics" not in related


def test_air_quality_is_air_pollution_and_neither_reaches_hearing_research(session):
    assert related_subjects("air quality") == related_subjects("air pollution")
    person(session, "a", "Ari Air", topics=["Atmospheric chemistry and aerosols"])
    person(session, "o", "Oto Ears", topics=["Otoacoustic Emissions"])
    found = names(execute(session, parse(session, "air quality researchers")))
    assert found == ["Ari Air"]
