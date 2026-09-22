"""The search index: text normalisation, self-maintenance, and what it buys
search — worldwide text, whole-word matching, and ranking that sees the field."""

import pytest
from rip.ingest import ingest_profile
from rip.nlq import count_matches, execute, parse
from rip.normalize import EvidenceItem, OrgAffiliation, ProjectData
from rip.textnorm import fold, org_key, phrase_terms, words
from tests.test_resolution import make_profile

from rip import search_index as si


def person(login, name, **kw):
    return make_profile(external_id=login, url=f"https://github.com/{login}",
                        raw={"login": login}, name=name, usernames=[f"github:{login}"], **kw)


def names(rows):
    return [p.canonical_name for p in rows]


# --- text normalisation -----------------------------------------------------

def test_folding_is_case_and_accent_insensitive():
    assert fold("Zürich") == fold("ZURICH") == "zurich"
    assert fold("José García") == "jose garcia"
    assert fold("Straße") == "strasse"
    assert fold("Łódź") == "lodz"


def test_words_keep_every_script_and_programming_names():
    assert words("ML engineers in São Paulo") == ["ml", "engineers", "in", "sao", "paulo"]
    assert words("C++ and C# in München") == ["c++", "and", "c#", "in", "munchen"]
    # scripts without spaces become overlapping character pairs
    assert words("机器学习") == ["机器", "器学", "学习"]
    # Hangul survives the round trip rather than decomposing into jamo
    assert words("서울") == ["서울"]


def test_phrases_are_chains_of_adjacent_pairs():
    assert phrase_terms("New York City") == ["new york", "york city"]
    assert phrase_terms("Rust") == ["rust"]
    assert org_key("Deccan.AI Pvt Ltd") == org_key("deccan ai") == "deccan ai"


# --- the index keeps itself current -----------------------------------------

def _terms(session, person_id, field):
    return {t.term for t in session.query(si.SearchTerm).filter_by(person_id=person_id, field=field)}


def test_ingest_indexes_the_person_in_the_same_transaction(session):
    p = ingest_profile(session, person("ada", "Ada Lovelace", location="London, United Kingdom",
                                       evidence=[EvidenceItem(attribute_type="skill",
                                                              value="Computer Vision")]))
    assert {"ada", "lovelace", "ada lovelace"} <= _terms(session, p.id, "n")
    assert {"computer vision"} <= _terms(session, p.id, "sv")
    assert {"computer", "vision", "computer vision"} <= _terms(session, p.id, "s")
    assert _terms(session, p.id, "c") == {"gb"}
    assert session.get(si.SearchDoc, p.id) is not None
    assert si.is_ready(session)


def test_a_rolled_back_change_never_reaches_the_index(session):
    from rip.models import Evidence

    p = ingest_profile(session, person("ada", "Ada Lovelace"))
    session.add(Evidence(person_id=p.id, attribute_type="skill", value="Haskell",
                         source="github", confidence=0.9))
    session.flush()
    session.rollback()
    assert "haskell" not in _terms(session, p.id, "s")


def test_evidence_added_later_is_searchable_after_commit(session):
    from rip.models import Evidence

    p = ingest_profile(session, person("ada", "Ada Lovelace"))
    assert execute(session, parse(session, "haskell")) == []
    session.add(Evidence(person_id=p.id, attribute_type="skill", value="Haskell",
                         source="github", confidence=0.9))
    session.commit()
    assert names(execute(session, parse(session, "haskell"))) == ["Ada Lovelace"]


def test_a_merged_away_person_leaves_the_index(session):
    from rip.review import merge_persons

    keep = ingest_profile(session, person("a1", "Ada Lovelace",
                                          evidence=[EvidenceItem(attribute_type="skill", value="Rust")]))
    gone = ingest_profile(session, person("a2", "Ada Byron",
                                          evidence=[EvidenceItem(attribute_type="skill", value="Haskell")]))
    merge_persons(session, keep.id, gone.id)
    session.commit()
    assert session.query(si.SearchTerm).filter_by(person_id=gone.id).count() == 0
    # the evidence moved, and the survivor is now findable by it
    assert names(execute(session, parse(session, "haskell"))) == ["Ada Lovelace"]


def test_rebuild_reproduces_the_live_index(session):
    ingest_profile(session, person("a", "Ada Lovelace", location="Bengaluru",
                                   evidence=[EvidenceItem(attribute_type="skill", value="Rust")]))
    ingest_profile(session, person("b", "Grace Hopper",
                                   organizations=[OrgAffiliation(name="Deccan.AI", role="Staff Engineer")]))
    live = sorted((t.term, t.field, t.person_id) for t in session.query(si.SearchTerm))
    si.rebuild(session)
    rebuilt = sorted((t.term, t.field, t.person_id) for t in session.query(si.SearchTerm))
    assert live == rebuilt
    assert not si.needs_rebuild(session)


# --- what search gains ---------------------------------------------------------

def test_free_text_matches_whole_words_not_fragments(session):
    ingest_profile(session, person("t", "Tess Trust",
                                   evidence=[EvidenceItem(attribute_type="bio",
                                                          value="I build trust and safety tooling")]))
    ingest_profile(session, person("r", "Rae Rustacean",
                                   evidence=[EvidenceItem(attribute_type="bio",
                                                          value="I write Rust every day")]))
    assert names(execute(session, parse(session, "rust"))) == ["Rae Rustacean"]


def test_accents_and_case_never_hide_a_place_or_a_name(session):
    ingest_profile(session, person("j", "José García", location="Zürich, Switzerland",
                                   evidence=[EvidenceItem(attribute_type="skill", value="Robotics")]))
    assert names(execute(session, parse(session, "robotics researchers in zurich"))) == ["José García"]
    assert names(execute(session, parse(session, "robotics in Zürich"))) == ["José García"]
    assert names(execute(session, parse(session, "find jose garcia"))) == ["José García"]


def test_endonyms_and_non_latin_place_names_are_real_filters(session):
    ingest_profile(session, person("m", "Max Weber", location="München",
                                   evidence=[EvidenceItem(attribute_type="skill", value="Robotics")]))
    ingest_profile(session, person("l", "Li Wei", location="北京",
                                   evidence=[EvidenceItem(attribute_type="skill", value="Robotics")]))
    assert names(execute(session, parse(session, "robotics in Munich"))) == ["Max Weber"]
    parsed = parse(session, "robotics in Deutschland")
    assert parsed.countries == ["DE"]
    assert names(execute(session, parsed)) == ["Max Weber"]
    assert names(execute(session, parse(session, "robotics 北京"))) == ["Li Wei"]


def test_a_city_location_satisfies_its_country(session):
    """"researchers in India" must reach someone whose location just says Bengaluru."""
    ingest_profile(session, person("b", "Asha Rao", location="Bengaluru",
                                   evidence=[EvidenceItem(attribute_type="skill", value="Rust")]))
    ingest_profile(session, person("x", "Ben Cole", location="Cambridge",   # ambiguous: no guess
                                   evidence=[EvidenceItem(attribute_type="skill", value="Rust")]))
    assert names(execute(session, parse(session, "rust developers in India"))) == ["Asha Rao"]


def test_skills_in_unspaced_scripts_are_searchable(session):
    ingest_profile(session, person("z", "Zhang San",
                                   evidence=[EvidenceItem(attribute_type="skill", value="机器学习与计算机视觉")]))
    # four characters, and only part of the stored value — both used to drop it
    parsed = parse(session, "机器学习")
    assert parsed.skills == ["机器学习与计算机视觉"]
    assert names(execute(session, parsed)) == ["Zhang San"]
    assert count_matches(session, parsed) == 1


def test_balanced_evidence_beats_one_sided_depth_on_a_two_concept_query(session):
    """A sum of evidence could not tell someone deep in one concept from
    someone solid in both; a per-concept mean can."""
    one_sided = [EvidenceItem(attribute_type="skill", value="Robotics", confidence=0.8)]
    one_sided += [EvidenceItem(attribute_type="research_interest", value=f"Robotics {i}", confidence=0.8)
                  for i in range(8)]
    one_sided += [EvidenceItem(attribute_type="bio", value="dabbles in computer vision", confidence=0.8)]
    ingest_profile(session, person("o", "One Sided", evidence=one_sided))
    ingest_profile(session, person("b", "Both Solid", evidence=[
        EvidenceItem(attribute_type="skill", value="Robotics", confidence=0.8),
        EvidenceItem(attribute_type="skill", value="Computer Vision", confidence=0.8),
    ]))
    rows = execute(session, parse(session, "robotics and computer vision"))
    assert names(rows)[0] == "Both Solid"


def test_index_and_fallback_paths_agree(session, monkeypatch):
    """The SQL fallback (for a database whose index is not built yet) and the
    index must return the same people for the same question."""
    ingest_profile(session, person("a", "Ada Example", location="Toronto, Canada",
                                   organizations=[OrgAffiliation(name="Acme Labs", is_current=True)],
                                   evidence=[EvidenceItem(attribute_type="skill", value="Rust"),
                                             EvidenceItem(attribute_type="research_interest",
                                                          value="Distributed Systems")],
                                   projects=[ProjectData(name="fastdb", url="https://x/1",
                                                         technologies=["Rust"])]))
    ingest_profile(session, person("g", "Grace Sample", location="Berlin, Germany",
                                   evidence=[EvidenceItem(attribute_type="skill", value="Python")]))
    queries = ["rust at Acme Labs", "distributed systems in Toronto", "python", "find Grace"]
    indexed = {q: (sorted(names(execute(session, parse(session, q)))),
                   count_matches(session, parse(session, q))) for q in queries}
    in_germany = names(execute(session, parse(session, "people in Germany")))
    monkeypatch.setattr("rip.nlq.si.is_ready", lambda s: False)
    fallback = {q: (sorted(names(execute(session, parse(session, q)))),
                    count_matches(session, parse(session, q))) for q in queries}
    assert indexed == fallback
    # No source stated Grace's country, but her location says Germany outright.
    # The index reads it that way, and so does the fallback: this used to be a
    # documented difference, and a Postgres run showed what it costs — "machine
    # learning researchers in India" answering with nobody at all.
    assert in_germany == ["Grace Sample"]
    assert names(execute(session, parse(session, "people in Germany"))) == ["Grace Sample"]


def test_an_exact_name_is_exact_whatever_the_punctuation_or_accents(session):
    """"Karan P. Singh" and "Karan P Singh" are the same name when typed."""
    from rip.nlq import relevance_scores

    exact = ingest_profile(session, person("k1", "Karan P. Singh"))
    other = ingest_profile(session, person("k2", "Karan Singh"))
    for typed in ("Karan P Singh", "karan p. singh"):
        parsed = parse(session, typed)
        fits = {pid: s["components"]["name_fit"]
                for pid, s in relevance_scores(session, parsed, [exact.id, other.id]).items()}
        assert fits[exact.id] == 1.0 and fits[other.id] < 1.0, typed
    accented = ingest_profile(session, person("j1", "José García"))
    parsed = parse(session, "jose garcia")
    assert relevance_scores(session, parsed, [accented.id])[accented.id]["components"]["name_fit"] == 1.0


def test_faceted_filters_on_the_index_match_the_sql_filters(session, monkeypatch):
    """/v1/persons answers text filters from the index; it must return exactly
    what its whole-word SQL filters return."""
    from rip.api import _ALL_FILTERS_NONE, list_persons

    ingest_profile(session, person("a", "Asha Rao", location="Berlin, Germany",
                                   organizations=[OrgAffiliation(name="Acme Labs", role="Principal Engineer",
                                                                 is_current=True),
                                                  OrgAffiliation(name="IIT Madras", relation="studied_at")],
                                   evidence=[EvidenceItem(attribute_type="skill", value="Distributed Systems")],
                                   projects=[ProjectData(name="fastdb", url="https://x/1", technologies=["Rust"])]))
    ingest_profile(session, person("b", "Ben Cole", location="Toronto, Canada",
                                   organizations=[OrgAffiliation(name="Globex", role="Data Engineer",
                                                                 is_current=True)],
                                   evidence=[EvidenceItem(attribute_type="skill", value="Go")]))
    cases = [dict(skill="distributed"), dict(skill="go"), dict(skill="systems distributed"),
             dict(organization="acme"), dict(education="iit"), dict(education="acme"),
             dict(current_organization="globex"), dict(role="engineer"), dict(role="principal engineer"),
             dict(location="berlin"), dict(technology="rust"), dict(q="rao"), dict(country="CA"),
             dict(skill="go", location="toronto", role="data engineer"), dict(skill="co*")]

    def run():
        return [
            (lambda r: (r["total_matches"], sorted(x["canonical_name"] for x in r["results"])))(
                list_persons(**{**_ALL_FILTERS_NONE, "limit": 50, **c, "db": session}))
            for c in cases
        ]

    indexed = run()
    monkeypatch.setattr("rip.search_index.is_ready", lambda s: False)
    assert run() == indexed
    assert indexed[0] == (1, ["Asha Rao"]) and indexed[5] == (0, [])


@pytest.mark.parametrize("pool", [1, 3])
def test_a_small_pool_keeps_the_strongest_candidates(session, monkeypatch, pool):
    """With more matches than the pool, the cut keeps the best, not arbitrary rows."""
    for i in range(6):
        ingest_profile(session, person(f"p{i}", f"Person {i}", evidence=[
            EvidenceItem(attribute_type="skill", value="Rust", confidence=0.7)],
            projects=[ProjectData(name=f"repo{i}", url=f"https://x/{i}", technologies=["Rust"],
                                  # under OUTPUT_SATURATION, so they do not tie
                                  activity={"stars": 5 + 700 * i, "forks": 0},
                                  last_active_at="2026-06-01")]))
    monkeypatch.setattr("rip.nlq.CANDIDATE_POOL", pool)
    parsed = parse(session, "rust")
    parsed.limit = 1
    assert names(execute(session, parsed)) == ["Person 5"]


def test_a_bio_is_found_whichever_number_the_query_uses(session):
    """Number insensitivity used to stop at the vocabulary: a bio reading
    "recommender systems" was invisible to a search for "recommender
    system", because index terms were literal."""
    ingest_profile(session, person("ava", "Ava Example", evidence=[
        EvidenceItem(attribute_type="bio", value="builds recommender systems at scale"),
    ]))
    ingest_profile(session, person("bo", "Bo Sample", evidence=[
        EvidenceItem(attribute_type="role", value="Research Engineers, Robotics"),
    ]))
    assert names(execute(session, parse(session, "recommender system"))) == ["Ava Example"]
    assert names(execute(session, parse(session, "recommender systems"))) == ["Ava Example"]
    assert si.any_match(session, si.phrase_alt("r", "research engineer"))
    assert si.any_match(session, si.phrase_alt("r", "research engineers"))


def test_a_surname_is_not_stemmed(session):
    """"Rogers" is not "Roger", and a name field must never fold them."""
    ingest_profile(session, person("ro", "Kim Rogers"))
    ingest_profile(session, person("rg", "Ana Roger"))
    assert names(execute(session, parse(session, "Rogers"))) == ["Kim Rogers"]
    assert names(execute(session, parse(session, "Roger"))) == ["Ana Roger"]
