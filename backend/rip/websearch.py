"""Web search backends — finding URLs, not people.

These providers do not hold person records; they find pages. Seekr uses them
for one job: locating a person's own homepage so the `web` connector can read
what that page publishes (CV link, email, cross-profile links).

Backends are tried in order of free-tier generosity and skipped entirely when
their key is unset, so a default install makes no paid calls. TinyFish Search
is free (no credits). Tavily is 1,000 credits/month, SerpApi 250/month.
"""

import logging
import os
import re

import httpx

logger = logging.getLogger("rip.websearch")

TIMEOUT = 25.0
# hosts that are never a personal homepage — a dedicated connector covers them,
# or they are aggregators we should not treat as someone's own page
SKIP_HOSTS = (
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "researchgate.net", "academia.edu", "semanticscholar.org",
    "scholar.google.com", "orcid.org", "dblp.org", "openalex.org", "github.com",
    "wikipedia.org", "wikidata.org", "amazon.com", "crunchbase.com",
    "glassdoor.com", "indeed.com", "quora.com", "medium.com",
)


def _host(url: str) -> str:
    return re.sub(r"^www\.", "", (url.split("//")[-1].split("/")[0] or "").lower())


def _is_candidate_homepage(url: str) -> bool:
    host = _host(url)
    return bool(host) and not any(
        host == h or host.endswith("." + h) for h in SKIP_HOSTS
    )


def _tinyfish(query: str, limit: int) -> list[dict]:
    from .tinyfish import search as tf_search

    return tf_search(query, limit)


def _tavily(query: str, limit: int) -> list[dict]:
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return []
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(
            "https://api.tavily.com/search",
            json={"api_key": key, "query": query, "max_results": limit},
        )
        resp.raise_for_status()
        return [
            {"url": r["url"], "title": r.get("title"), "snippet": (r.get("content") or "")[:400],
             "backend": "tavily"}
            for r in resp.json().get("results") or []
            if r.get("url")
        ]


def _serpapi(query: str, limit: int) -> list[dict]:
    key = os.environ.get("SERPAPI_API_KEY")
    if not key:
        return []
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(
            "https://serpapi.com/search.json",
            params={"q": query, "api_key": key, "num": limit},
        )
        resp.raise_for_status()
        return [
            {"url": r["link"], "title": r.get("title"), "snippet": (r.get("snippet") or "")[:400],
             "backend": "serpapi"}
            for r in resp.json().get("organic_results") or []
            if r.get("link")
        ]


# free first: a key that costs nothing is tried before ones that draw a quota
BACKENDS = (("tinyfish", _tinyfish), ("tavily", _tavily), ("serpapi", _serpapi))


def available_backends() -> list[str]:
    return [name for name, _ in BACKENDS
            if os.environ.get(f"{name.upper()}_API_KEY")]


def search(query: str, limit: int = 5) -> list[dict]:
    """First backend that returns something wins; failures fall through."""
    for name, fn in BACKENDS:
        try:
            hits = fn(query, limit)
        except Exception as exc:
            logger.warning("websearch backend %s failed: %s", name, exc)
            continue
        if hits:
            return hits
    return []


def find_homepage(name: str, organization: str | None = None, limit: int = 5) -> list[dict]:
    """Candidate personal-homepage URLs for someone.

    Aggregators and sites with their own connector are filtered out, so what
    comes back is the person's own page — the thing worth reading.
    """
    if not name:
        return []
    query = f'"{name}" {organization or ""} homepage OR "personal page" OR CV'.strip()
    return [h for h in search(query, limit) if _is_candidate_homepage(h["url"])]
