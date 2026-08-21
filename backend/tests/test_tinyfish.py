"""TinyFish Search + Fetch wiring. No network — keys unset or HTTP stubbed."""

import pytest

from rip import tinyfish
from rip.connectors.web import WebConnector
from rip.nlq import PAID_SOURCES, _search_tinyfish


def test_tinyfish_is_free():
    assert "tinyfish" not in PAID_SOURCES
    src = open(tinyfish.__file__, encoding="utf-8").read()
    assert "api.search.tinyfish.ai" in src
    assert "api.fetch.tinyfish.ai" in src
    assert "agent.tinyfish.ai/v1" not in src
    assert "api.browser.tinyfish.ai" not in src


def test_no_key_means_no_http(monkeypatch):
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)

    def boom(*a, **k):
        raise AssertionError("must not call TinyFish without a key")

    monkeypatch.setattr(tinyfish.httpx, "Client", boom)
    assert tinyfish.search("python developers") == []
    assert tinyfish.fetch_html("https://example.com") == ""
    assert _search_tinyfish("python developers", 5) == []


def test_routes_hits_to_existing_connectors():
    assert tinyfish.route_hit("https://github.com/torvalds")["source"] == "github"
    assert tinyfish.route_hit("https://github.com/torvalds/linux")["external_id"] == "torvalds"
    assert tinyfish.route_hit("https://github.com/topics/python") is None
    assert tinyfish.route_hit("https://huggingface.co/julien-c")["source"] == "huggingface"
    assert tinyfish.route_hit("https://huggingface.co/models") is None
    assert tinyfish.route_hit(
        "https://orcid.org/0000-0002-1825-0097"
    )["source"] == "orcid"
    assert tinyfish.route_hit("https://dblp.org/pid/h/GeoffreyEHinton")["source"] == "dblp"
    assert tinyfish.route_hit("https://openalex.org/authors/A5023888391")["source"] == "openalex"
    assert tinyfish.route_hit("https://stackoverflow.com/users/22656/jon-skeet")["source"] == "stackoverflow"
    web = tinyfish.route_hit("https://www.cs.toronto.edu/~hinton/")
    assert web["source"] == "web"
    assert web["external_id"].startswith("https://")


def test_linkedin_and_aggregators_are_dropped():
    for url in (
        "https://www.linkedin.com/in/someone",
        "https://scholar.google.com/citations?user=1",
        "https://en.wikipedia.org/wiki/Geoffrey_Hinton",
        "https://www.researchgate.net/profile/X",
        "https://www.coursera.org/articles/product-designer",
        "https://www.indeed.com/career-advice/ml-engineer",
    ):
        assert tinyfish.route_hit(url) is None, url
    assert tinyfish.route_hit(
        "https://example.com/blog",
        title="What Is a Product Designer? Salaries, Skills, and More",
    ) is None


def test_search_uses_free_endpoint(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [
                {"url": "https://janedoe.ai", "title": "Jane Doe", "snippet": "engineer"},
                {"url": "https://github.com/janedoe", "title": "janedoe"},
            ]}

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        def get(self, url, headers=None, params=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["params"] = params
            return FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(tinyfish.httpx, "Client", FakeClient)
    hits = tinyfish.search("python", limit=5)
    assert captured["url"] == tinyfish.SEARCH_URL
    assert captured["headers"]["X-API-Key"] == "k"
    assert [h["backend"] for h in hits] == ["tinyfish", "tinyfish"]


def test_fetch_requests_html_not_agent(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"url": "https://x", "text": "<html>hi</html>"}], "errors": []}

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        def post(self, url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(tinyfish.httpx, "Client", FakeClient)
    html = tinyfish.fetch_html("https://janedoe.ai")
    assert captured["url"] == tinyfish.FETCH_URL
    assert captured["json"]["format"] == "html"
    assert captured["json"]["urls"] == ["https://janedoe.ai"]
    assert html == "<html>hi</html>"


def test_name_query_asks_for_github_profiles(monkeypatch):
    from rip.nlq import NLQuery, _search_tinyfish

    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    seen = {}

    def fake_search(q, limit=10, **k):
        seen["q"] = q
        return []

    monkeypatch.setattr(tinyfish, "search", fake_search)
    _search_tinyfish("Rahul", 5, NLQuery(raw="Rahul", name_terms=["Rahul"]))
    assert "github.com" in seen["q"]


def test_nlq_searcher_routes_github_and_web(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    monkeypatch.setattr(
        tinyfish, "search",
        lambda q, limit=10, **k: [
            {"url": "https://github.com/ada", "title": "Ada", "snippet": ""},
            {"url": "https://ada.dev", "title": "Ada Dev", "snippet": ""},
            {"url": "https://linkedin.com/in/ada", "title": "Ada LI", "snippet": ""},
        ],
    )
    found = _search_tinyfish("python engineers", 10)
    assert [(x["source"], x["external_id"]) for x in found] == [
        ("github", "ada"),
        ("web", "https://ada.dev"),
    ]


def test_web_renderer_tries_tinyfish_first(monkeypatch):
    connector = WebConnector.__new__(WebConnector)
    monkeypatch.setattr(WebConnector, "get_text",
                        lambda self, url: (_ for _ in ()).throw(RuntimeError("403")))
    for var in ("TINYFISH_API_KEY", "FIRECRAWL_API_KEY", "ZENROWS_API_KEY", "SCRAPINGBEE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="could not fetch"):
        connector._fetch_html("https://js.example")

    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    monkeypatch.setattr(WebConnector, "_via_tinyfish",
                        lambda self, url: "<html>" + "z" * 400 + "</html>")
    assert "z" * 400 in connector._fetch_html("https://js.example")
