"""Europe PMC: people are identified by ORCID or not at all, and one ORCID
pasted into two strangers' submissions must not fuse them.

The payload shapes here are trimmed copies of live responses.
"""

from rip.connectors.europepmc import EuropePmcConnector, _institution


def author(first, last, orcid=None, affiliation=None):
    entry = {"fullName": f"{last} {first[:1]}", "firstName": first, "lastName": last}
    if orcid:
        entry["authorId"] = {"type": "ORCID", "value": orcid}
    if affiliation:
        entry["authorAffiliationDetailsList"] = {
            "authorAffiliation": [{"affiliation": affiliation}]
        }
    return entry


def article(title, authors, *, cited=0, doi=None, journal="J Soil", year="2024",
            keywords=(), mesh=()):
    return {
        "id": title.replace(" ", "")[:8], "source": "MED", "title": title,
        "doi": doi, "citedByCount": cited, "pubYear": year,
        "firstPublicationDate": f"{year}-01-01",
        "journalInfo": {"journal": {"title": journal}},
        "authorList": {"author": list(authors)},
        "keywordList": {"keyword": list(keywords)},
        "meshHeadingList": {"meshHeading": [{"descriptorName": m} for m in mesh]},
    }


class FakeEuropePmc:
    """Answers like the REST API, and records every query."""

    def __init__(self, by_query):
        self.by_query = by_query
        self.calls = []

    def __call__(self, url, params=None):
        params = dict(params or {})
        self.calls.append(params)
        for fragment, articles in self.by_query.items():
            if fragment in params["query"]:
                return {"hitCount": len(articles),
                        "resultList": {"result": articles[: params["pageSize"]]}}
        return {"hitCount": 0, "resultList": {"result": []}}


def connector(fake):
    conn = EuropePmcConnector.__new__(EuropePmcConnector)
    conn.get_json = fake
    return conn


RILLIG = "0000-0003-3541-7853"
ZHU = "0000-0002-0826-6423"


def test_only_authors_with_an_orcid_are_offered():
    """A name is not an identity here: four Stéphane Laurents in the corpus
    carry no ORCID between them, so a name search finds nobody."""
    fake = FakeEuropePmc({"soil": [
        article("Biochar effects on soil biota", [
            author("Matthias", "Rillig", RILLIG, "Institute of Biology, Berlin, Germany"),
            author("Anon", "Coauthor"),                      # no ORCID: not offered
        ]),
    ]})
    found = connector(fake).search_authors_by_topic("soil microbiome")
    assert [(a["name"], a["id"]) for a in found] == [("Matthias Rillig", RILLIG)]
    assert found[0]["affiliation"] == "Institute of Biology"


def test_a_consortium_paper_is_not_evidence_of_a_subject():
    many = [author(f"A{i}", f"Author{i}", f"0000-0001-0000-{i:04d}") for i in range(30)]
    fake = FakeEuropePmc({"trial": [article("Multicentre trial", many)]})
    assert connector(fake).search_authors_by_topic("trial") == []


def test_a_name_search_does_not_return_the_co_authors_of_that_name():
    fake = FakeEuropePmc({"AUTH": [
        article("Hearing loss and hearing aids", [
            author("Stephane", "Laurent"),                   # the person, no ORCID
            author("Laurent", "Fabre", ZHU),                 # a co-author who has one
        ]),
    ]})
    assert connector(fake).search_authors("Stephane Laurent") == []


def test_one_orcid_on_two_strangers_papers_yields_one_person():
    """0000-0002-1825-0097, the ORCID documentation example, sits on papers by
    a dermatologist in Nanjing and a computer scientist in Halifax."""
    shared = "0000-0002-1825-0097"
    fake = FakeEuropePmc({"AUTHORID": [
        article("Linear hyperpigmented patches", [
            author("Cheng", "Tan", shared, "Affiliated Hospital of Nanjing University, Nanjing, China")],
            cited=4, doi="10.2340/a"),
        article("Depressed patches follow-up", [
            author("Cheng", "Tan", shared, "Affiliated Hospital of Nanjing University, Nanjing, China")],
            cited=2, doi="10.2340/b"),
        article("k-mer based breaking", [
            author("Travis", "Gagie", shared, "Dalhousie University, Halifax, Canada")],
            cited=9, doi="10.1007/c"),
    ]})
    profile = connector(fake).fetch(shared)
    assert profile.name == "Cheng Tan"                       # the two-paper majority
    assert [p.doi for p in profile.publications] == ["10.2340/a", "10.2340/b"]
    assert profile.country == "CN"
    assert [o.name for o in profile.organizations] == \
        ["Affiliated Hospital of Nanjing University"]


def test_a_fetch_carries_topics_publications_and_the_orcid_key():
    fake = FakeEuropePmc({"AUTHORID": [
        article("Biochar effects on soil biota", [
            author("Matthias", "Rillig", RILLIG, "Institute of Biology, Berlin, Germany"),
            author("Dong", "Zhu", ZHU)],
            cited=1227, doi="10.1016/j.soilbio.2011.04.022", journal="Soil Biol Biochem",
            keywords=["Global change"], mesh=["Soil Microbiology", "Biodiversity"]),
    ]})
    conn = connector(fake)
    profile = conn.fetch(f"https://orcid.org/{RILLIG}")      # a URL resolves too
    assert profile.external_id == RILLIG and profile.orcid == RILLIG
    assert profile.linked_urls == [f"https://orcid.org/{RILLIG}"]
    assert profile.country == "DE"
    assert sorted(e.value for e in profile.evidence) == \
        ["Biodiversity", "Global change", "Soil Microbiology"]
    pub = profile.publications[0]
    assert pub.citations == 1227 and pub.venue == "Soil Biol Biochem"
    assert pub.author_position == 1
    assert pub.raw_authors == ["Matthias Rillig", "Dong Zhu"]   # co-author evidence
    assert conn.get_json.calls[-1]["sort"] == "CITED desc"      # best work first


def test_an_identifier_that_is_not_an_orcid_is_refused():
    conn = connector(FakeEuropePmc({}))
    try:
        conn.fetch("A5023888391")
    except ValueError as exc:
        assert "ORCID" in str(exc)
    else:                                                    # pragma: no cover
        raise AssertionError("an OpenAlex id was accepted as a person")


def test_an_email_in_an_affiliation_is_not_collected():
    fake = FakeEuropePmc({"AUTHORID": [
        # the contact address shares its comma-part with the institution,
        # which is how it reaches an organisation name at all
        article("Case report", [author(
            "Cheng", "Tan", RILLIG,
            "Department of Dermatology, Affiliated Hospital of Nanjing "
            "University tancheng@yeah.net, Nanjing, China.")]),
    ]})
    profile = connector(fake).fetch(RILLIG)
    assert profile.emails == []
    assert "@" not in " ".join(o.name for o in profile.organizations)


def test_the_institution_is_read_out_of_an_affiliation_string():
    assert _institution(
        "Department of Cardiology, Kailuan Hospital, North China University of "
        "Science and Technology, Tangshan, China") == "Kailuan Hospital"
    assert _institution("Tangshan, China") is None


def test_a_source_can_be_switched_off_without_a_code_change(monkeypatch):
    """What a source is worth differs by graph: Europe PMC adds people no
    other free source can identify, and costs a few seconds to do it."""
    from rip import discovery

    assert "europepmc" in [s for s, _f, _u in discovery.enabled_searchers()]
    monkeypatch.setenv("RIP_SKIP_SOURCES", "europepmc")
    names = [s for s, _f, _u in discovery.enabled_searchers()]
    assert "europepmc" not in names and "openalex" in names
    monkeypatch.setenv("RIP_SKIP_SOURCES", " Europe PMC ")      # not a source name
    assert "europepmc" in [s for s, _f, _u in discovery.enabled_searchers()]


def test_europe_pmc_is_searched_beside_openalex_not_after_it():
    """It runs in the background phase, so its latency — a median of 1.4s and
    a tail measured at 14s — is spent while OpenAlex is still working."""
    from rip import discovery

    assert discovery._always_run("europepmc")
    assert "europepmc" in discovery.FREE_FETCH_SOURCES
