"""A name search finds that name and nothing else; an abbreviation is its subject.

Searching "Sundar Pichai" when nobody here had that surname kept "Sundar" as
a name and set "Pichai" aside, so every Sundar answered; live search then
stored and showed twenty-one people, most of them not called Sundar Pichai --
a psychiatrist whose papers Europe PMC matched, a GitHub account called
JACKSPARROWbts. And "NLP" matched the one person who had typed "NLP" as a
topic, 26 people where "natural language processing" found 30.
"""

from rip.ingest import ingest_profile
from rip.names import carries_name
from rip.nlq import execute_progressive, parse
from rip.normalize import EvidenceItem, PublicationData
from tests.test_resolution import make_profile


def person(session, ext, name, *topics, papers=()):
    ingest_profile(session, make_profile(
        source="openalex", source_type="scholarly", external_id=ext,
        url=f"https://openalex.org/{ext}", raw={"id": ext}, name=name, usernames=[],
        evidence=[EvidenceItem(attribute_type="research_interest", value=t) for t in topics],
        publications=[PublicationData(title=t, external_id=f"{ext}-{i}")
                      for i, t in enumerate(papers)]))
    session.commit()


def found(session, query):
    return sorted(p.canonical_name for p in execute_progressive(session, parse(session, query))[0])


def test_carries_name():
    assert carries_name("Sundar Pichai", ["Sundar Pichai"])
    assert carries_name("sundar pichai", ["PICHAI, Sundar"])
    assert carries_name("Bill Gates", ["William H. Gates"])
    assert carries_name("Jose Garcia", ["José García"])
    assert carries_name("sundar pichai", ["Someone Else"], handles=["SundarPichai"])
    assert not carries_name("Sundar Pichai", ["Michael Bauer"])
    assert not carries_name("Sundar Pichai", ["Sundar Kumar"])
    assert not carries_name("Sundar Pichai", ["Sundareswaran R"])
    assert not carries_name("Sundar Pichai", ["Pichai"])


def test_a_name_nobody_fully_carries_finds_nobody(session):
    person(session, "s", "Satya Kumar")
    parsed = parse(session, "satya nadella")
    assert parsed.name_terms == ["satya", "nadella"] and parsed.unmatched_terms == []
    assert found(session, "satya nadella") == []


def test_a_name_finds_only_people_called_that(session):
    person(session, "a", "Sundar Pichai")
    person(session, "b", "Sundar Kumar")
    person(session, "c", "Ajith Kumar Pichai")
    for query in ("sundar pichai", "Sundar Pichai", "SUNDAR PICHAI", "pichai sundar"):
        assert found(session, query) == ["Sundar Pichai"], query


def test_a_name_typed_in_capitals_is_still_a_name(session):
    """Read as two acronyms, "GEOFFREY HINTON" found nobody."""
    person(session, "h", "Geoffrey E. Hinton")
    assert found(session, "GEOFFREY HINTON") == found(session, "geoffrey hinton") == ["Geoffrey E. Hinton"]
    # a short all-capital query is still an acronym
    assert parse(session, "NLP").name_terms == []


def test_live_candidates_must_carry_the_name(session, monkeypatch):
    from rip import discovery

    def fake(search_for, limit):
        return [{"external_id": "x1", "name": "Michael Bauer"},
                {"external_id": "x2", "name": "Sundar Pichai"},
                {"external_id": "x3", "name": "Sridhar Pichai"}]

    monkeypatch.setattr(discovery, "enabled_searchers", lambda: (("europepmc", fake, True),))
    person(session, "a", "Sundar Kumar")
    got = discovery.discovery_suggestions(session, parse(session, "Sundar Pichai"),
                                          allow_paid=False, persist=False)
    assert [g["name"] for g in got] == ["Sundar Pichai"]


def test_people_stored_by_a_live_search_are_shown_only_if_they_answer(session, monkeypatch):
    from rip import discovery
    from rip.api import nl_query

    person(session, "k", "Sundar Kumar")

    def fake_discovery(db, parsed, **_kw):
        out = []
        for ext, name in (("x1", "Sundar Pichai"), ("x2", "Michael Bauer")):
            stored = ingest_profile(db, make_profile(
                source="openalex", source_type="scholarly", external_id=ext,
                url=f"https://openalex.org/{ext}", raw={"id": ext}, name=name, usernames=[]))
            db.commit()
            out.append({"source": "openalex", "external_id": ext, "name": name,
                        "stored": True, "person_id": str(stored.id)})
        return out

    monkeypatch.setattr(discovery, "discovery_suggestions", fake_discovery)
    resp = nl_query(q="sundar pichai", limit=0, offset=0, discover="auto", db=session)
    assert [r["canonical_name"] for r in resp["results"]] == ["Sundar Pichai"]


def test_an_abbreviation_finds_the_same_people_as_its_subject(session):
    person(session, "s", "Sam Short", "NLP")
    person(session, "f", "Fay Full", "Natural Language Processing")
    person(session, "t", "Tess Techniques", "Natural Language Processing Techniques")
    person(session, "o", "Otto Other", "Computer Vision")
    for short, full in (("NLP", "natural language processing"),
                        ("nlp researchers", "Natural Language Processing researchers")):
        assert found(session, short) == found(session, full) == \
            ["Fay Full", "Sam Short", "Tess Techniques"], short


def test_an_abbreviation_reads_papers_as_its_subject(session):
    """"GNN" found the 6 people stating it and not the 11 who publish on it."""
    person(session, "s", "Gil Stated", "Advanced Graph Neural Networks")
    person(session, "p", "Pat Papers", papers=["Graph neural networks for molecules",
                                                "Scalable graph neural networks"])
    assert found(session, "GNN") == found(session, "graph neural networks") == ["Gil Stated", "Pat Papers"]


def test_the_topics_that_matched_are_listed_first(session):
    from rip.api import nl_query

    person(session, "a", "Ann Many", "Bandits", "Games", "Compression", "Planning",
           "Search", "Control", "Topic Modeling")
    ingest_profile(session, make_profile(
        source="openalex", source_type="scholarly", external_id="a2",
        url="https://openalex.org/a2", raw={"id": "a2"}, name="Ann Many", usernames=[],
        evidence=[EvidenceItem(attribute_type="research_interest", value=t)
                  for t in ("Bandits", "Games", "Compression", "Planning", "Search", "Control")]))
    session.commit()
    resp = nl_query(q="topic modeling", limit=0, offset=0, discover="false", db=session)
    attrs = resp["results"][0]["attributes"]
    assert attrs[0]["value"] == "Topic Modeling" and attrs[0]["matched"] is True
    assert not any(a["matched"] for a in attrs[1:])


def test_an_alias_that_is_someone_elses_name_does_not_find_them(session):
    """OpenAlex filed "Aman Sharma" under Poonam Sharma, and a search for
    Aman Sharma listed her."""
    from rip.names import alias_fits

    assert not alias_fits("Poonam Sharma", "Aman Sharma")
    assert not alias_fits("Poonam Sharma", "A. Sharma")
    assert alias_fits("Poonam Sharma", "Sharma, P.")
    assert alias_fits("Geoffrey Hinton", "Geoffrey Everest Hinton")
    assert alias_fits("William H. Gates", "Bill Gates")
    ingest_profile(session, make_profile(
        source="openalex", source_type="scholarly", external_id="p",
        url="https://openalex.org/p", raw={"id": "p"}, name="Poonam Sharma",
        aliases=["Aman Sharma", "P. Sharma"], usernames=[]))
    person(session, "a", "Aman Sharma")
    session.commit()
    assert found(session, "aman sharma") == ["Aman Sharma"]
    assert found(session, "poonam sharma") == ["Poonam Sharma"]


def test_rows_carry_what_tells_namesakes_apart(session):
    from rip.api import nl_query

    ingest_profile(session, make_profile(
        source="openalex", source_type="scholarly", external_id="o",
        url="https://openalex.org/o", raw={"id": "o"}, name="Aman Sharma", usernames=[],
        orcid="0000-0001-7122-1095",
        publications=[PublicationData(title="On turbines", external_id="o-1")]))
    person(session, "g", "Aman Sharma")
    session.commit()
    rows = nl_query(q="aman sharma", limit=0, offset=0, discover="false", db=session)["results"]
    got = sorted(((r.get("orcid") or ""), r.get("papers")) for r in rows)
    assert got == [("", 0), ("0000-0001-7122-1095", 1)]


def test_a_live_person_is_kept_only_if_they_answer(session, monkeypatch):
    """"NLP researchers in India" stored 23 people and showed 4; the rest --
    microbiome researchers in Trento among them -- stayed in the corpus."""
    from rip import discovery
    from rip.models import Person
    from rip.normalize import NormalizedProfile

    person(session, "seed", "Nell Seed", "Natural Language Processing")

    def fake_search(search_for, limit):
        return [{"source": "openalex", "external_id": "fit", "name": "Ira Fit"},
                {"source": "openalex", "external_id": "off", "name": "Tom Off"}]

    def fake_fetch(to_fetch, deadline=None):
        out = []
        for item in to_fetch:
            topic = "Natural Language Processing" if item["external_id"] == "fit" else "Gut Microbiome"
            out.append((item, NormalizedProfile(
                source="openalex", source_type="scholarly", external_id=item["external_id"],
                url=f"https://openalex.org/{item['external_id']}", raw={"id": item["external_id"]},
                name=item["name"], usernames=[],
                evidence=[EvidenceItem(attribute_type="research_interest", value=topic)]), None))
        return out

    monkeypatch.setattr(discovery, "enabled_searchers", lambda: (("openalex", fake_search, True),))
    monkeypatch.setattr(discovery, "_fetch_profiles", fake_fetch)
    asked = parse(session, "natural language processing")
    from rip.nlq import satisfying
    got = discovery.discovery_suggestions(session, asked,
                                          allow_paid=False, persist=True,
                                          answers=lambda pid: pid in satisfying(session, asked, [pid], use_index=False))
    names = {p.canonical_name for p in session.query(Person).all()}
    assert "Ira Fit" in names and "Tom Off" not in names
    assert [g["name"] for g in got] == ["Ira Fit"]


def test_a_name_people_carry_is_not_respelled_into_a_subject(session):
    """"Virat" became "viral" and answered with virologists."""
    person(session, "v", "Virat Agarwal")
    person(session, "x", "Vera Virologist", "Viral Infections and Outbreaks Research")
    parsed = parse(session, "Virat")
    assert parsed.name_terms == ["Virat"] and not parsed.skill_groups
    assert found(session, "virat") == ["Virat Agarwal"]


def test_a_keyword_that_is_the_holders_own_name_is_not_a_topic(session):
    """An ORCID record listed "Vivek Mishra" among its keywords, and "vivek"
    then matched that topic instead of the people called Vivek."""
    person(session, "m", "Vivek Mishra", "Vivek Mishra", "V. Mishra", "Rust")
    person(session, "g", "Vivek Gupta")
    from rip.models import Evidence

    assert {e.value for e in session.query(Evidence).filter(
        Evidence.attribute_type == "research_interest")} == {"Rust"}
    assert found(session, "vivek") == ["Vivek Gupta", "Vivek Mishra"]
