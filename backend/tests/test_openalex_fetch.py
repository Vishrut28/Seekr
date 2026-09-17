"""OpenAlex: author records batched across a live search's fetches, and
topical discovery reading authors from small-team papers."""

from rip.connectors.openalex import OpenAlexConnector


def author(aid, name="Ada Example"):
    return {"id": f"https://openalex.org/{aid}", "display_name": name,
            "last_known_institutions": [], "topics": []}


class FakeOpenAlex:
    """Answers like the API, and records every request."""

    def __init__(self, small_team_results=True):
        self.calls = []
        self.small_team_results = small_team_results

    def __call__(self, url, params=None):
        params = dict(params or {})
        self.calls.append((url, params))
        if "/authors/" in url:
            return author(url.rsplit("/", 1)[-1])
        if url.endswith("/authors"):
            if "filter" not in params:                      # name search
                return {"results": [author("A1", params["search"])]}
            ids = params["filter"].split(":", 1)[1].split("|")
            return {"results": [author(i) for i in ids]}
        if "search" in params:
            if "authors_count" in params.get("filter", "") and not self.small_team_results:
                return {"results": []}
            return {"results": [{"authorships": [
                {"author": {"id": "https://openalex.org/A7", "display_name": "Soil Scientist"},
                 "institutions": [{"display_name": "Wageningen"}]}]}]}
        return {"results": []}


def connector(fake):
    conn = OpenAlexConnector()
    conn.get_json = fake
    return conn


def test_prefetch_loads_many_author_records_in_one_request():
    fake = FakeOpenAlex()
    conn = connector(fake)
    conn.prefetch(["A1", "A2", "A3"])
    for aid in ("A1", "A2", "A3"):
        assert conn.fetch(aid).name == "Ada Example"
    assert [u for u, _ in fake.calls if "/authors/" in u] == []   # no per-author lookups
    assert sum(1 for u, _ in fake.calls if u.endswith("/authors")) == 1
    assert conn._prefetched_authors == {}                         # consumed, not kept


def test_fetch_without_prefetch_still_works():
    fake = FakeOpenAlex()
    assert connector(fake).fetch("A9").external_id == "A9"
    assert any(u.endswith("/authors/A9") for u, _ in fake.calls)


def test_topical_discovery_reads_small_team_papers_first():
    fake = FakeOpenAlex()
    found = connector(fake).search_authors_by_topic("soil microbiome")
    assert [a["name"] for a in found] == ["Soil Scientist"]
    search_calls = [p for _, p in fake.calls if "search" in p]
    assert len(search_calls) == 1 and "authors_count:<26" in search_calls[0]["filter"]


def test_topical_discovery_falls_back_when_every_paper_is_a_big_collaboration():
    fake = FakeOpenAlex(small_team_results=False)
    found = connector(fake).search_authors_by_topic("neutrino oscillation")
    assert found and len([p for _, p in fake.calls if "search" in p]) == 2


def test_every_request_asks_only_for_the_fields_normalize_reads():
    """`select` keeps the request count the same and the payload small."""
    fake = FakeOpenAlex()
    conn = connector(fake)
    conn.prefetch(["A1"])
    conn.fetch("A1")
    conn.fetch("A9")                                   # no prefetch: single author route
    conn.search_authors("Ada Example")
    conn.search_authors_by_topic("soil microbiome")
    for url, params in fake.calls:
        assert params.get("select"), f"{url} asks for the full record"
    works = [p["select"] for u, p in fake.calls if u.endswith("/works")]
    assert all("authorships" in s for s in works)      # co-authors are evidence
    assert not any("abstract" in s or "referenced_works" in s for s in works)


def test_a_trimmed_author_record_still_normalizes_completely():
    """Guards the select list: a field dropped from it arrives as None."""
    trimmed = {
        "id": "https://openalex.org/A55",
        "display_name": "Rekha Iyer",
        "display_name_alternatives": ["R. Iyer"],
        "orcid": "https://orcid.org/0000-0002-1825-0097",
        "works_count": 1,
        "cited_by_count": 12,
        "last_known_institutions": [
            {"display_name": "IISc", "country_code": "IN", "type": "education"}
        ],
        "topics": [{"display_name": "Soil microbiology", "count": 4}],
    }
    work = {
        "id": "https://openalex.org/W9",
        "title": "Soil carbon",
        "display_name": "Soil carbon",
        "doi": "https://doi.org/10.1/abc",
        "publication_date": "2021-04-02",
        "cited_by_count": 12,
        "primary_location": {"source": {"display_name": "Nature Soils"}},
        "topics": [{"display_name": "Soil microbiology"}],
        "authorships": [
            {"author": {"id": "https://openalex.org/A55", "display_name": "Rekha Iyer"},
             "institutions": [{"display_name": "IISc"}]},
            {"author": {"id": "https://openalex.org/A56", "display_name": "Arun Mehta"},
             "institutions": []},
        ],
    }
    profile = OpenAlexConnector.__new__(OpenAlexConnector).normalize(trimmed, [work])
    assert profile.name == "Rekha Iyer" and profile.aliases == ["R. Iyer"]
    assert profile.country == "IN" and profile.orcid.endswith("1825-0097")
    assert [o.name for o in profile.organizations] == ["IISc"]
    assert [e.value for e in profile.evidence] == ["Soil microbiology"]
    pub = profile.publications[0]
    assert pub.venue == "Nature Soils" and pub.citations == 12
    assert pub.doi == "10.1/abc" and pub.author_position == 1
    assert pub.raw_authors == ["Rekha Iyer", "Arun Mehta"]   # co-author evidence kept


def test_an_author_with_no_works_skips_the_works_request():
    fake = FakeOpenAlex()
    conn = connector(fake)
    conn._prefetched_authors["A3"] = {**author("A3"), "works_count": 0}
    assert conn.fetch("A3").publications == []
    assert not [u for u, _ in fake.calls if u.endswith("/works")]


def test_a_placeholder_address_is_never_sent_to_openalex(monkeypatch):
    """OPENALEX_MAILTO travels in every request URL. The real .env had the
    template's you@example.com in it, which claims the polite pool under an
    address nobody reads."""
    conn = OpenAlexConnector.__new__(OpenAlexConnector)
    monkeypatch.setenv("OPENALEX_MAILTO", "you@example.com")
    assert "mailto" not in conn._params()
    monkeypatch.setenv("OPENALEX_MAILTO", "not-an-address")
    assert "mailto" not in conn._params()
    monkeypatch.setenv("OPENALEX_MAILTO", "data@seekr.example-lab.org")
    assert conn._params()["mailto"] == "data@seekr.example-lab.org"
