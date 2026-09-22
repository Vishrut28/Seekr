"""Bulk harvesting, which nothing exercised at all.

rip/harvest.py was at 0% coverage — 97 statements that page a source's whole
catalogue into JSONL for `bulk-ingest` to stream back in. It is how the corpus
is supposed to reach millions rather than hundreds, and it is the one path
where a mistake is not visible until a long download has finished.

The claim worth checking is the one in its own comment: that it shapes each
row "the way OpenAlexConnector.renormalize expects". Nothing verified that,
and the two sides are edited in different files.

No network: a fake client and a fake connector throughout.
"""

import gzip
import json

import httpx
import pytest

from rip import harvest


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


class FakeClient:
    """Answers with the pages it was given, remembering what was asked."""

    def __init__(self, pages, calls):
        self._pages = list(pages)
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get(self, _url, params=None):
        self.calls.append(dict(params or {}))
        return self._pages.pop(0) if self._pages else FakeResponse({"results": []})


def author(n):
    return {"id": f"https://openalex.org/A{n}", "display_name": f"Ada {n}",
            "works_count": n, "cited_by_count": n * 10,
            "last_known_institutions": [{"display_name": "Somewhere",
                                         "country_code": "GB"}]}


def page(authors, cursor):
    return FakeResponse({"results": [author(a) for a in authors],
                         "meta": {"next_cursor": cursor}})


@pytest.fixture
def client_pages(monkeypatch):
    calls = []

    def install(pages):
        monkeypatch.setattr(harvest.httpx, "Client",
                            lambda **_kw: FakeClient(pages, calls))
        return calls

    return install


def read(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_it_pages_by_cursor_until_the_source_runs_out(tmp_path, client_pages):
    calls = client_pages([page([1, 2], "c2"), page([3], None)])
    out = tmp_path / "authors.jsonl"
    result = harvest.harvest_openalex(str(out), limit=100, progress=lambda _m: None)

    assert result.fetched == 3
    assert result.pages == 2
    assert [c["cursor"] for c in calls] == ["*", "c2"]
    assert [row["id"] for row in read(out)] == ["A1", "A2", "A3"]


def test_it_stops_at_the_limit_rather_than_downloading_the_index(tmp_path, client_pages):
    """Passing no filter harvests 125 million authors; `limit` is the brake."""
    calls = client_pages([page([1, 2, 3], "c2"), page([4, 5, 6], "c3")])
    out = tmp_path / "authors.jsonl"
    result = harvest.harvest_openalex(str(out), limit=4, progress=lambda _m: None)

    assert result.fetched <= 6            # the last page is taken whole
    assert calls[0]["per-page"] == 4      # asked for no more than the limit
    assert calls[1]["per-page"] == 1      # and less once some are in hand
    assert len(calls) == 2, "kept paging past the limit"


def test_a_harvested_row_is_what_renormalize_expects(tmp_path, client_pages):
    """The two halves of the bulk pipeline live in different files and this
    is the only thing that holds them together."""
    from rip.connectors.openalex import OpenAlexConnector

    client_pages([page([7], None)])
    out = tmp_path / "authors.jsonl"
    harvest.harvest_openalex(str(out), limit=10, progress=lambda _m: None)

    row = read(out)[0]
    profile = OpenAlexConnector().renormalize(row["id"], row)
    assert profile.name == "Ada 7"
    assert profile.external_id == "A7"
    assert profile.country == "GB"


def test_it_writes_gzip_when_asked(tmp_path, client_pages):
    client_pages([page([1], None)])
    out = tmp_path / "authors.jsonl.gz"
    harvest.harvest_openalex(str(out), limit=10, progress=lambda _m: None)
    assert out.read_bytes()[:2] == b"\x1f\x8b"
    assert read(out)[0]["id"] == "A1"


def test_a_rate_limit_is_retried_rather_than_raised(tmp_path, client_pages, monkeypatch):
    monkeypatch.setattr(harvest.time, "sleep", lambda _s: None)
    client_pages([FakeResponse({}, status_code=429), page([1], None)])
    out = tmp_path / "authors.jsonl"
    result = harvest.harvest_openalex(str(out), limit=10, progress=lambda _m: None)
    assert result.fetched == 1


def test_the_mailto_is_only_sent_when_there_is_one(tmp_path, client_pages, monkeypatch):
    """OPENALEX_MAILTO is empty here on purpose, and an empty one must not be
    sent as a parameter."""
    monkeypatch.delenv("OPENALEX_MAILTO", raising=False)
    calls = client_pages([page([1], None)])
    harvest.harvest_openalex(str(tmp_path / "a.jsonl"), limit=5, progress=lambda _m: None)
    assert "mailto" not in calls[0]

    calls2 = client_pages([page([1], None)])
    harvest.harvest_openalex(str(tmp_path / "b.jsonl"), limit=5,
                             mailto="someone@example.org", progress=lambda _m: None)
    assert calls2[-1]["mailto"] == "someone@example.org"


class FakeGitHub:
    def __init__(self, by_city):
        self.by_city = by_city
        self.asked = []

    def search_users(self, location=None, query="", page=1, per_page=100):
        self.asked.append((location, query, page))
        if location == "explodes":
            raise RuntimeError("search is unavailable")
        logins = self.by_city.get(location, []) if page == 1 else []
        return logins, len(logins)


@pytest.fixture
def github(monkeypatch):
    def install(by_city):
        fake = FakeGitHub(by_city)
        monkeypatch.setattr("rip.connectors.get_connector", lambda _s: fake)
        return fake

    return install


def test_the_same_person_in_two_cities_is_written_once(tmp_path, github):
    """A login found in Bangalore and again in Bengaluru is one person, and
    the identifier is not case-sensitive."""
    github({"Bangalore": ["Ada", "bob"], "Bengaluru": ["ada", "Cy"]})
    out = tmp_path / "logins.jsonl"
    result = harvest.harvest_github_india(
        str(out), limit=100, cities=["Bangalore", "Bengaluru"], progress=lambda _m: None)

    written = [row["external_id"] for row in read(out)]
    assert written == ["Ada", "bob", "Cy"]
    assert result.fetched == 3


def test_a_city_that_will_not_answer_does_not_end_the_harvest(tmp_path, github):
    github({"explodes": [], "Pune": ["dee"]})
    out = tmp_path / "logins.jsonl"
    said = []
    result = harvest.harvest_github_india(
        str(out), limit=100, cities=["explodes", "Pune"], progress=said.append)

    assert [row["external_id"] for row in read(out)] == ["dee"]
    assert result.fetched == 1
    assert any("unavailable" in m for m in said)


def test_it_does_not_search_below_the_follower_floor(tmp_path, github):
    fake = github({"Pune": ["dee"]})
    harvest.harvest_github_india(str(tmp_path / "l.jsonl"), limit=100,
                                 cities=["Pune"], min_followers=200,
                                 progress=lambda _m: None)
    bands = {asked[1] for asked in fake.asked}
    assert all("followers:>=1000" in b or "followers:200..999" in b for b in bands), bands
