"""Foreign work: papers nothing ties to a career, about something else entirely.

The group ratio cannot see a single intruder paper, and a temporal break
cannot see two people publishing in the same decades. What both have is work
that shares no co-author, no institution and no citation with the career,
and sits elsewhere in OpenAlex's subject taxonomy. These pin down each link
that rescues a paper, and each guard against calling range a second person.
"""

from rip import api, conflation
from rip.ingest import ingest_profile
from rip.normalize import PublicationData
from tests.test_resolution import make_profile

# topic -> (subfield, field, domain), as OpenAlex places them
PLACE = {
    "Superconductivity": ("Condensed Matter Physics", "Physics and Astronomy",
                          "Physical Sciences"),
    "Magnetic Materials": ("Condensed Matter Physics", "Physics and Astronomy",
                           "Physical Sciences"),
    "Laser Optics": ("Atomic and Molecular Physics, and Optics",
                     "Physics and Astronomy", "Physical Sciences"),
    "Thin Film Growth": ("Materials Chemistry", "Materials Science", "Physical Sciences"),
    "Bird Phylogeny": ("Ecology, Evolution, Behavior and Systematics",
                       "Agricultural and Biological Sciences", "Life Sciences"),
}


def work(title, topic, coauthors=("Bob Physicist",), institution="Uni Physics",
         year=2010, cites=()):
    topics = [topic] if isinstance(topic, str) else list(topic)
    return {"title": title, "topics": topics, "coauthors": list(coauthors),
            "institution": institution, "year": year, "cites": list(cites)}


def career(n=8, **kw):
    return [work(f"Paper {i} on superconductors", "Superconductivity",
                 year=2005 + i, **kw) for i in range(n)]


def openalex_person(session, tag, name, works):
    """A person as OpenAlex hands them over: per work, the topics with their
    place in the taxonomy, THIS author's institution, and what it cites.
    CITES holds indexes into WORKS, or ids of works outside the record."""
    ids = [f"https://openalex.org/W{tag}{i}" for i in range(len(works))]
    payload = {
        "author": {"id": f"https://openalex.org/A{tag}", "display_name": name},
        "works": [{
            "id": ids[i],
            "topics": [{"display_name": t,
                        "subfield": {"display_name": PLACE[t][0]},
                        "field": {"display_name": PLACE[t][1]},
                        "domain": {"display_name": PLACE[t][2]}} for t in w["topics"]],
            "referenced_works": [ids[j] if isinstance(j, int) else j for j in w["cites"]],
            "authorships": [
                {"author": {"id": f"https://openalex.org/A{tag}"},
                 "institutions": [{"display_name": w["institution"]}]},
                # a co-author somewhere else: their employer is not this
                # person's, and reading it as such would join strangers
                {"author": {"id": "https://openalex.org/A-collaborator"},
                 "institutions": [{"display_name": "Everybody's Collaborator Institute"}]},
            ],
        } for i, w in enumerate(works)],
    }
    person = ingest_profile(session, make_profile(
        source="openalex", source_type="scholarly", external_id=f"A{tag}",
        url=f"https://openalex.org/A{tag}", raw=payload, name=name,
        usernames=[f"openalex:A{tag}"],
        publications=[
            PublicationData(title=w["title"], external_id=ids[i], topics=w["topics"],
                            raw_authors=[name, *w["coauthors"]],
                            published_date=str(w["year"]))
            for i, w in enumerate(works)
        ]))
    session.commit()
    return person


def intruder(**kw):
    base = {"coauthors": ("Cal Birder",), "institution": "Uni Zoology", "year": 2009}
    base.update(kw)
    # each its own title: one title under one person is one work (rip.papers)
    return work(f"Phylogeny of the shorebirds, {'-'.join(base['coauthors'])}",
                "Bird Phylogeny", **base)


def found(session, who):
    return who.id in [s.person_id for s in conflation.candidates(session)]


def test_one_intruder_in_another_domain_is_found_where_nothing_else_looks(session):
    """Jes Olesen's one ecology paper among thirty years of headache science:
    a singleton, so the ratio scores 0.00, and in the middle of the career,
    so there is no hole in time. It is found only because nothing ties it to
    the career and it is not about anything the career is about."""
    who = openalex_person(session, "olesen", "Jes Olesen", career() + [intruder()])
    split = conflation.split_of(session, who.id)
    assert split.score == 0.0 and split.break_years == 0
    assert list(split.foreign.values()) == ["domain"]
    assert split.reasons() == ["foreign"]
    assert found(session, who)


def test_a_shared_co_author_ties_the_paper_to_the_career(session):
    who = openalex_person(session, "coauth", "Ann Range",
                          career() + [intruder(coauthors=("Bob Physicist",))])
    assert conflation.split_of(session, who.id).foreign == {}
    assert not found(session, who)


def test_the_same_institution_on_their_own_line_ties_it(session):
    """A physicist who once wrote about birds did it from the same
    university; two people under one name never were in the same place."""
    who = openalex_person(session, "inst", "Ann Range",
                          career() + [intruder(institution="Uni Physics")])
    assert conflation.split_of(session, who.id).foreign == {}


def test_a_co_authors_institution_does_not(session):
    """Every work in the fixture also carries a collaborator at one shared
    institute. If other people's lines were read as this author's, every paper
    would be tied to every other and nothing would ever be foreign -- which is
    what the intruder test above would then fail on. This pins the reason."""
    who = openalex_person(session, "lines", "Jes Olesen", career() + [intruder()])
    works = conflation._openalex_works(session, who.id)[who.id]
    assert all(w["institutions"] <= {"Uni Physics", "Uni Zoology"} for w in works.values())


def test_citing_the_career_ties_it(session):
    works = career() + [intruder(cites=[0])]
    who = openalex_person(session, "cites", "Ann Range", works)
    assert conflation.split_of(session, who.id).foreign == {}


def test_being_cited_by_the_career_ties_it(session):
    works = career()
    works[3]["cites"] = [len(works)]         # a career paper cites the intruder
    who = openalex_person(session, "cited", "Ann Range", works + [intruder()])
    assert conflation.split_of(session, who.id).foreign == {}


def test_a_reference_in_common_ties_it(session):
    """Two papers citing the same third paper are reading the same
    literature. The shared reference is outside this record."""
    classic = "https://openalex.org/W-a-classic-nobody-here-wrote"
    works = career()
    works[2]["cites"] = [classic]
    who = openalex_person(session, "shared", "Ann Range",
                          works + [intruder(cites=[classic])])
    assert conflation.split_of(session, who.id).foreign == {}


def test_a_topic_in_common_is_not_a_link(session):
    """OpenAlex topics are broad, and one paper tagged with both subjects
    would chain two careers into one body of work -- the parasitologist and
    the medicinal chemist named William Trager are joined that way. Only
    what the same person shares links papers; a topic is what is compared."""
    both = work("A paper tagged with both", ["Superconductivity", "Bird Phylogeny"],
                coauthors=("Dee Nobody",), institution="Nowhere Particular")
    birds = [intruder(year=2005 + i, coauthors=("Cal Birder", f"Birder {i}"))
             for i in range(3)]
    who = openalex_person(session, "chain", "William Trager", career() + [both] + birds)
    split = conflation.split_of(session, who.id)
    assert len(split.foreign) == 3, split.foreign


def test_a_stray_paper_on_the_same_subject_is_not_foreign(session):
    """Sharing nothing is common -- a one-off collaboration from a visiting
    post. On the career's own subject it says nothing about identity."""
    stray = work("A one-off", "Magnetic Materials", coauthors=("Cal Visitor",),
                 institution="Somewhere Else")
    who = openalex_person(session, "stray", "Ann Range", career() + [stray])
    assert conflation.split_of(session, who.id).foreign == {}


def test_one_paper_foreign_only_by_subfield_is_enough(session):
    """Same field, different subfield. It was once held back as probably a
    mis-tagged topic; on 120 unopened records, reporting it found 10 of 17
    conflations instead of 8 for five more false alarms, and it was adopted.
    It is reported, and it sits BELOW farther foreign work in the queue."""
    optics = work("A laser paper", "Laser Optics", coauthors=("Cal Visitor",),
                  institution="Somewhere Else")
    who = openalex_person(session, "sub", "Ann Range", career() + [optics])
    split = conflation.split_of(session, who.id)
    assert list(split.foreign.values()) == ["subfield"]
    assert split.foreign_score == conflation.FOREIGN_REPORT_AT == 1
    assert found(session, who)


def test_one_paper_in_another_field_is_enough(session):
    film = work("Growing oxide films", "Thin Film Growth", coauthors=("Cal Chemist",),
                institution="Somewhere Else")
    who = openalex_person(session, "field", "Ann Range", career() + [film])
    split = conflation.split_of(session, who.id)
    assert list(split.foreign.values()) == ["field"]
    assert found(session, who)


def test_two_careers_in_the_same_decades_are_found(session):
    """William Trager the parasitologist and William F. Trager the medicinal
    chemist were both publishing 1974-2005. No gap exists; the second career
    is a body of work of its own, tied to itself and to nothing else."""
    birds = [intruder(year=2005 + i, coauthors=("Cal Birder", f"Birder {i}"))
             for i in range(5)]
    who = openalex_person(session, "twin", "William Trager", career() + birds)
    split = conflation.split_of(session, who.id)
    assert split.break_years == 0
    assert len(split.foreign) == 5 and set(split.foreign.values()) == {"domain"}
    assert found(session, who)


def test_consortium_papers_are_not_foreign_work(session):
    """A paper with hundreds of names says nothing about who works with whom,
    so it links to nothing by construction, and Global Burden of Disease
    papers span every disease. Measured on the design labels, they were the
    commonest reason a single person looked like several."""
    crowd = tuple(f"Consortium Member{i}" for i in range(40))
    gbd = intruder(coauthors=crowd, institution="Somewhere Else")
    who = openalex_person(session, "gbd", "Ann Range", career() + [gbd])
    assert conflation.split_of(session, who.id).foreign == {}


def test_the_ratio_reports_only_when_asked(session):
    """Two halves with no taxonomy to compare: the ratio sees them, and it
    no longer reports by default."""
    from tests.test_conflation import one_field, researcher

    papers = one_field(6, "Made-up Subject A", "Colleague A")
    papers += one_field(6, "Made-up Subject B", "Colleague B")
    who = researcher(session, "ratio", "Ann Double", papers)
    assert conflation.candidates(session) == []
    assert [s.person_id for s in conflation.candidates(
        session, above=conflation.REPORT_ABOVE)] == [who.id]


def test_a_shared_employer_does_not_excuse_foreign_work(session, monkeypatch):
    """The employer check compares the ratio's two largest groups, and it
    answers the ratio only. A record reported for foreign work stays."""
    who = openalex_person(session, "emp", "Jes Olesen", career() + [intruder()])
    monkeypatch.setattr(conflation, "shares_an_employer", lambda *_a: True)
    assert found(session, who)
    assert who.id in [c["person_id"] for c in
                      api.review_conflations(above=None, limit=50, db=session)["conflations"]]


def test_the_review_page_is_told_why_and_shown_the_papers(session):
    who = openalex_person(session, "page", "Jes Olesen", career() + [intruder()])
    entry = api.review_conflations(above=None, limit=50, db=session)["conflations"][0]
    assert entry["person_id"] == who.id
    assert entry["reasons"] == ["foreign"]
    assert entry["foreign_score"] == conflation.FOREIGN_WEIGHTS["domain"]
    [paper] = entry["foreign"]
    assert paper["title"].startswith("Phylogeny of the shorebirds")
    assert paper["distance"] == "domain" and paper["year"] == 2009


def test_the_farthest_foreign_work_is_read_first(session):
    optics = [work(f"Laser {i}", "Laser Optics", coauthors=("Cal Visitor",),
                   institution="Somewhere Else") for i in range(2)]
    near = openalex_person(session, "near", "Ann Near", career() + optics)
    far = openalex_person(session, "far", "Ann Far", career() + [intruder()])
    order = [s.person_id for s in conflation.candidates(session)]
    assert order == [far.id, near.id]


def test_topics_are_placed_from_anybody_s_payload(session):
    """Publication rows keep topic names only. A person known through a
    source with no taxonomy still has their topics placed, by any payload
    that mentions them."""
    openalex_person(session, "teach", "Ann Teacher", career(2) + [intruder()])
    hierarchy = conflation.topic_hierarchy(session)
    assert hierarchy["Bird Phylogeny"][2] == "Life Sciences"
    assert hierarchy["Superconductivity"][1] == "Physics and Astronomy"
