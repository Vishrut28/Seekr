"""Web-search backends and the render-fallback chain (no network)."""

import pytest

from rip import websearch
from rip.connectors.web import WebConnector


def test_no_keys_means_no_calls(monkeypatch):
    """A default install must make zero third-party calls."""
    for var in ("TINYFISH_API_KEY", "TAVILY_API_KEY", "SERPAPI_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    def boom(*a, **k):
        raise AssertionError("must not call out without a key")

    monkeypatch.setattr(websearch.httpx, "Client", boom)
    assert websearch.available_backends() == []
    assert websearch.search("anything") == []
    assert websearch.find_homepage("Ada Lovelace") == []


def test_aggregators_are_not_treated_as_homepages():
    for url in [
        "https://linkedin.com/in/someone", "https://en.wikipedia.org/wiki/X",
        "https://github.com/someone", "https://scholar.google.com/citations?user=1",
        "https://www.researchgate.net/profile/X", "https://medium.com/@someone",
    ]:
        assert not websearch._is_candidate_homepage(url), url
    for url in ["https://www.cs.toronto.edu/~hinton/", "https://simonster.com",
                "https://janedoe.ai/about"]:
        assert websearch._is_candidate_homepage(url), url


def test_backend_failure_falls_through(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "x")
    monkeypatch.setenv("TAVILY_API_KEY", "x")
    monkeypatch.setenv("SERPAPI_API_KEY", "y")
    monkeypatch.setattr(websearch, "_tinyfish",
                        lambda q, l: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(websearch, "_tavily",
                        lambda q, l: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(websearch, "_serpapi",
                        lambda q, l: [{"url": "https://ok.example", "title": "t",
                                       "snippet": "", "backend": "serpapi"}])
    monkeypatch.setattr(websearch, "BACKENDS",
                        (("tinyfish", websearch._tinyfish),
                         ("tavily", websearch._tavily),
                         ("serpapi", websearch._serpapi)))
    assert websearch.search("q")[0]["backend"] == "serpapi"


def test_render_fallback_only_when_key_set(monkeypatch):
    connector = WebConnector.__new__(WebConnector)
    monkeypatch.setattr(WebConnector, "get_text",
                        lambda self, url: (_ for _ in ()).throw(RuntimeError("403")))
    for var in ("TINYFISH_API_KEY", "FIRECRAWL_API_KEY", "ZENROWS_API_KEY", "SCRAPINGBEE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="could not fetch"):
        connector._fetch_html("https://blocked.example")

    monkeypatch.setenv("ZENROWS_API_KEY", "k")
    monkeypatch.setattr(WebConnector, "_via_zenrows",
                        lambda self, url: "<html>" + "x" * 400 + "</html>")
    assert "x" * 400 in connector._fetch_html("https://blocked.example")


def test_robots_gate_runs_before_any_renderer(monkeypatch):
    """A renderer must never be used to reach a robots-disallowed page."""
    connector = WebConnector.__new__(WebConnector)
    monkeypatch.setattr(WebConnector, "_robots_allows", lambda self, url: False)
    monkeypatch.setattr(
        WebConnector, "_fetch_html",
        lambda self, url: (_ for _ in ()).throw(AssertionError("fetched a disallowed page")),
    )
    with pytest.raises(PermissionError, match="robots.txt disallows"):
        connector.fetch("https://forbidden.example/page")


def test_thin_response_triggers_fallback(monkeypatch):
    connector = WebConnector.__new__(WebConnector)
    monkeypatch.setattr(WebConnector, "get_text", lambda self, url: "<html></html>")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    monkeypatch.setattr(WebConnector, "_via_firecrawl",
                        lambda self, url: "<html>" + "y" * 600 + "</html>")
    assert "y" * 600 in connector._fetch_html("https://js-heavy.example")
