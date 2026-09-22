"""Ranking across disciplines and levels of evidence: citations judged by
field norms, and a stated topic above a filing category."""

from rip.ingest import ingest_profile
from rip.nlq import execute, parse
from rip.normalize import EvidenceItem, PublicationData
from tests.test_resolution import make_profile

from rip import nlq


def researcher(session, ext, name, field, citations, topics=(), filed=()):
    evidence = [EvidenceItem(attribute_type="research_interest", value=t) for t in topics]
    evidence.append(EvidenceItem(attribute_type="research_field", value=field, confidence=0.35,
                                 extracted_info=f"OpenAlex field of '{field} topic'"))
    evidence += [EvidenceItem(attribute_type="research_field", value=f, confidence=0.45,
                              extracted_info=f"OpenAlex subfield of '{f} topic'") for f in filed]
    ingest_profile(session, make_profile(
        external_id=ext, url=f"https://github.com/{ext}", raw={"login": ext},
        name=name, usernames=[f"github:{ext}"], evidence=evidence,
        publications=[PublicationData(title=f"{name} paper", external_id=f"W-{ext}",
                                      citations=citations)]))


def test_citations_count_by_the_norms_of_the_field(session):
    """A mathematician with 300 citations stands where a biomedical researcher
    with 3,000 does: each is typical of a field cited ten times apart."""
    for i in range(6):
        researcher(session, f"m{i}", f"Math Person{i}", "Mathematics", 300)
        researcher(session, f"b{i}", f"Bio Person{i}", "Biochemistry", 3000)
    nlq._field_baseline_cache.clear()
    ids = {p.canonical_name: p.id for p in session.query(nlq.Person).all()}
    factors = nlq._field_citation_factors(session, list(ids.values()))
    math, bio = factors[ids["Math Person0"]], factors[ids["Bio Person0"]]
    assert math > 1.0 > bio
    low, high = nlq.FIELD_FACTOR_BOUNDS
    assert low <= bio and math <= high
    # the gap between them in citations shrinks rather than decides
    assert 300 * math / (3000 * bio) > 300 / 3000


def test_a_person_with_no_field_on_record_is_left_as_they_are(session):
    ingest_profile(session, make_profile(
        external_id="g", url="https://github.com/g", raw={"login": "g"},
        name="Dev Sample", usernames=["github:g"]))
    researcher(session, "m", "Math Person", "Mathematics", 300)
    nlq._field_baseline_cache.clear()
    dev = session.query(nlq.Person).filter_by(canonical_name="Dev Sample").one()
    assert dev.id not in nlq._field_citation_factors(session, [dev.id])


def test_a_stated_topic_outranks_a_filing_category(session):
    """"Global Public Health Policies and Epidemiology" is a claim the person
    makes; the Epidemiology subfield is where an index filed someone. They
    used to carry exactly the same depth of evidence."""
    researcher(session, "s", "Stated Epi", "Medicine", 100,
               topics=["Global Public Health Policies and Epidemiology"])
    researcher(session, "f", "Filed Epi", "Medicine", 100,
               topics=["Hepatitis B Virus Studies"], filed=["Epidemiology"])
    nlq._field_baseline_cache.clear()
    parsed = parse(session, "epidemiologists")
    ids = {p.canonical_name: p.id for p in session.query(nlq.Person).all()}
    per_group = nlq._score_evidence(session, parsed, list(ids.values()), {}, {}, {}, {})
    stated, filed = per_group[ids["Stated Epi"]][0], per_group[ids["Filed Epi"]][0]
    assert stated == nlq.CONTAINED_TOPIC_WEIGHT and filed == nlq.FIELD_EVIDENCE_WEIGHT
    assert stated > filed
    ranked = [p.canonical_name for p in execute(session, parsed)]
    assert ranked == ["Stated Epi", "Filed Epi"]
