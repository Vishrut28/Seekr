"""SPREAD: the only conflation signal for people with no OpenAlex topic.

A quarter of the corpus is known only from sources that classify nothing,
so foreign work has no subjects to compare. Their titles are placed in one of
OpenAlex's four domains by a model trained on the corpus's own placed papers,
and a record whose titles fall steadily into three domains or more is read.
These pin down what the model learns from, who it is applied to, and the
two counts that decide whether a record is reported.
"""

from rip.ingest import ingest_profile
from rip.normalize import PublicationData
from tests.test_resolution import make_profile

from rip import api, conflation

DOMAINS = {
    "Physical Sciences": ("Physics and Astronomy", ["quantum lattice superconductor",
                                                    "spin lattice magnetism",
                                                    "superconductor phonon quantum"]),
    "Life Sciences": ("Biochemistry", ["protein enzyme gene",
                                       "gene expression enzyme",
                                       "protein folding gene"]),
    "Health Sciences": ("Medicine", ["patients clinical hospital",
                                     "clinical trial patients",
                                     "hospital admissions patients"]),
    "Social Sciences": ("Economics", ["market policy households",
                                      "households income policy",
                                      "market prices policy"]),
}


def teacher(session, tag="teach"):
    """An OpenAlex person with placed papers in every domain: what the title
    model learns from. Topic names are made up; their place is in the payload."""
    works, pubs = [], []
    for domain, (field, titles) in DOMAINS.items():
        topic = f"{field} topic"
        for i, title in enumerate(titles * 2):
            wid = f"https://openalex.org/W{tag}{domain[:3]}{i}"
            works.append({"id": wid, "topics": [{
                "display_name": topic,
                "subfield": {"display_name": f"{field} subfield"},
                "field": {"display_name": field},
                "domain": {"display_name": domain}}],
                "authorships": []})
            pubs.append(PublicationData(title=f"{title} {i}", external_id=wid,
                                        topics=[topic], raw_authors=["Tea Cher"]))
    ingest_profile(session, make_profile(
        source="openalex", source_type="scholarly", external_id=f"A{tag}",
        url=f"https://openalex.org/A{tag}", name="Tea Cher",
        raw={"author": {"id": f"https://openalex.org/A{tag}"}, "works": works},
        usernames=[f"openalex:A{tag}"], publications=pubs))
    session.commit()


def unplaced(session, tag, name, titles):
    """Somebody known only from a source with no topics at all."""
    person = ingest_profile(session, make_profile(
        source="semanticscholar", source_type="scholarly", external_id=tag,
        url=f"https://www.semanticscholar.org/author/{tag}", raw={"authorId": tag},
        name=name, usernames=[f"semanticscholar:{tag}"],
        publications=[PublicationData(title=t, external_id=f"{tag}-{i}", topics=[],
                                      raw_authors=[name, f"Coauthor {i}"])
                      for i, t in enumerate(titles)]))
    session.commit()
    return person


def two_each(*domains):
    return [f"{DOMAINS[d][1][i]} results" for d in domains for i in range(2)]


def reported(session, who):
    return who.id in [s.person_id for s in conflation.candidates(session)]


def test_titles_steadily_in_three_domains_are_reported(session):
    teacher(session)
    who = unplaced(session, "three", "S. Aman",
                   two_each("Physical Sciences", "Life Sciences", "Health Sciences"))
    split = conflation.split_of(session, who.id)
    assert split.spread_domains == 3
    assert split.reasons() == ["spread"]
    assert reported(session, who)


def test_two_domains_are_one_career(session):
    """A clinician who also does lab work is common; that is not two people."""
    teacher(session)
    titles = two_each("Life Sciences", "Health Sciences") * 2
    who = unplaced(session, "two", "Ann Clinician", titles)
    assert conflation.split_of(session, who.id).spread_domains == 2
    assert not reported(session, who)


def test_a_domain_needs_two_papers(session):
    """One stray title is often a mis-placed one; a domain has to be held."""
    teacher(session)
    titles = two_each("Physical Sciences", "Life Sciences") * 2
    titles.append(DOMAINS["Health Sciences"][1][0] + " results")
    who = unplaced(session, "stray", "Ann Stray", titles)
    split = conflation.split_of(session, who.id)
    assert len(split.spread) == 3 and split.spread_domains == 2
    assert not reported(session, who)


def test_records_with_placed_topics_are_left_to_foreign_work(session):
    """The teacher's own titles span all four domains, and it is not read by
    SPREAD at all: where OpenAlex has placed the work, foreign work compares
    it properly, with co-authors, institutions and citations."""
    from rip.models import Person

    teacher(session)
    person = session.query(Person).filter(Person.canonical_name == "Tea Cher").one()
    assert conflation.split_of(session, person.id).spread == {}


def test_a_title_the_model_cannot_place_counts_for_nothing(session):
    teacher(session)
    titles = ["zyxw qrst vbnm", "plokij uhygt", "wertyu asdfgh", "mnbvcx lkjhgf",
              "poiuyt rewqas", "zxcvbn mlkjhg"]
    who = unplaced(session, "blank", "Ann Blank", titles)
    assert conflation.split_of(session, who.id).spread == {}


def test_the_model_learns_only_from_placed_papers(session):
    """Twelve placed papers per domain in the fixture, counted once per domain
    their topics sit in; nothing from the unplaced person reaches training."""
    teacher(session)
    unplaced(session, "three", "S. Aman",
             two_each("Physical Sciences", "Life Sciences", "Health Sciences"))
    model = conflation.title_domains(session, conflation.topic_hierarchy(session))
    assert dict(model.prior) == dict.fromkeys(DOMAINS, 6)


def test_wider_spread_is_read_first(session):
    teacher(session)
    four = unplaced(session, "four", "V. Mishra", two_each(*DOMAINS))
    # more papers than FOUR, so paper count -- the last tiebreak -- would put
    # it first if spread did not
    three = unplaced(session, "three", "S. Aman",
                     two_each("Physical Sciences", "Life Sciences", "Health Sciences") * 2)
    order = [s.person_id for s in conflation.candidates(session)]
    assert order.index(four.id) < order.index(three.id)


def test_the_review_page_shows_the_domains_and_titles(session):
    teacher(session)
    who = unplaced(session, "page", "S. Karan",
                   two_each("Physical Sciences", "Life Sciences", "Health Sciences"))
    entry = next(c for c in api.review_conflations(above=None, limit=50, db=session)
                 ["conflations"] if c["person_id"] == who.id)
    assert entry["reasons"] == ["spread"]
    assert {d["domain"] for d in entry["spread"]} == {
        "Physical Sciences", "Life Sciences", "Health Sciences"}
    assert all(d["papers"] == 2 and d["titles"] for d in entry["spread"])
