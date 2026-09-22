"""TinyFish Search + Fetch only.

Search finds candidate URLs; Fetch renders a page and returns HTML. Both are
free (no wallet/credits). Agent and Browser are paid and are never called
from this module — there is no function that can reach those endpoints.
"""

from __future__ import annotations

import logging
import os
import re

import httpx

logger = logging.getLogger("rip.tinyfish")

SEARCH_URL = "https://api.search.tinyfish.ai"
FETCH_URL = "https://api.fetch.tinyfish.ai"
# stated in TinyFish's own docs: Search and Fetch never draw from the wallet
TIMEOUT = 45.0
PURPOSE_PEOPLE = (
    "Find personal websites and public profiles of individual people matching "
    "this name or role, anywhere in the world. Exclude job boards, career-advice "
    "articles, resume templates, and listicles."
)
PURPOSE_FETCH = (
    "Extract a person's public profile: name, role, organization, skills, "
    "and any CV or resume link the page itself publishes."
)


def configured() -> bool:
    return bool(os.environ.get("TINYFISH_API_KEY"))


def _headers() -> dict:
    key = os.environ.get("TINYFISH_API_KEY") or ""
    return {"X-API-Key": key, "Content-Type": "application/json"}


def search(
    query: str,
    limit: int = 10,
    *,
    purpose: str | None = PURPOSE_PEOPLE,
    location: str | None = None,
    client: httpx.Client | None = None,
) -> list[dict]:
    """Ranked web results. Empty when the key is unset ΓÇö never a paid fallback."""
    if not configured() or not (query or "").strip():
        return []
    params: dict = {"query": query.strip()}
    if purpose:
        params["purpose"] = purpose[:2000]
    if location:
        params["location"] = location
    own = client is None
    if own:
        client = httpx.Client(timeout=TIMEOUT)
    try:
        resp = client.get(SEARCH_URL, headers=_headers(), params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        rows = (resp.json() or {}).get("results") or []
    finally:
        if own:
            client.close()
    out = []
    for row in rows:
        url = row.get("url")
        if not url:
            continue
        out.append({
            "url": url,
            "title": row.get("title"),
            "snippet": (row.get("snippet") or "")[:400],
            "site_name": row.get("site_name"),
            "backend": "tinyfish",
        })
        if len(out) >= limit:
            break
    return out


def fetch_html(url: str, client: httpx.Client | None = None) -> str:
    """Rendered HTML for one URL. Empty string if the key is unset or fetch fails."""
    if not configured() or not url:
        return ""
    own = client is None
    if own:
        client = httpx.Client(timeout=TIMEOUT)
    try:
        resp = client.post(
            FETCH_URL,
            headers=_headers(),
            json={"urls": [url], "format": "html", "purpose": PURPOSE_FETCH},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json() or {}
    finally:
        if own:
            client.close()
    errors = data.get("errors") or []
    if errors:
        logger.info("tinyfish fetch errors for %s: %s", url, errors[0])
    results = data.get("results") or []
    if not results:
        return ""
    page = results[0]
    html = page.get("text") or page.get("html") or page.get("content") or ""
    if isinstance(html, dict):
        return ""
    return str(html)


# hosts we never ingest as a "web" person page (aggregators, or we have a
# dedicated connector and parse the URL into that connector instead)
_SKIP_WEB = (
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "researchgate.net", "academia.edu", "wikipedia.org",
    "wikidata.org", "amazon.com", "crunchbase.com", "glassdoor.com",
    "indeed.com", "quora.com", "medium.com", "reddit.com",
    "scholar.google.com", "semanticscholar.org",
    # career-advice / job-board hosts: they match the role words, not a person
    "coursera.org", "udemy.com", "codecademy.com", "princetonreview.com",
    "productplan.com", "monster.com", "ziprecruiter.com", "naukri.com",
    "reed.co.uk", "stepstone.de", "levels.fyi", "teamblind.com",
    "themuse.com", "flexjobs.com", "wellfound.com", "angel.co",
)
_ADVICE_TITLE = re.compile(
    r"\b(what is a|what does a|how to become|how to get started|"
    r"job description|salaries|salary|glossary|resume examples?|"
    r"cv examples?|portfolio examples?|we're hiring|job posting|"
    r"apply now|website builder|jobs in|how i became)\b",
    re.I,
)
_GITHUB_RESERVED = {
    "orgs", "topics", "settings", "explore", "marketplace", "features", "about",
    "login", "signup", "new", "notifications", "issues", "pulls", "codespaces",
    "sponsors", "enterprise", "team", "collections", "events", "trending",
    "stars", "search", "apps", "pricing", "security", "blog", "site",
}
_HF_RESERVED = {
    "models", "datasets", "spaces", "docs", "pricing", "login", "join",
    "organizations", "tasks", "papers", "learn", "blog",
}


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def _path_parts(url: str) -> list[str]:
    from urllib.parse import unquote, urlparse

    path = unquote(urlparse(url).path or "").strip("/")
    return [p for p in path.split("/") if p]


def route_hit(url: str, title: str | None = None, snippet: str | None = None) -> dict | None:
    """Turn a search URL into a connector ingest target, or None to skip.

    LinkedIn and other disallowed aggregators are dropped. Profile hosts with
    their own connector (GitHub, Hugging Face, ORCID, dblp) are routed there
    so we store a structured record instead of a scraped page.
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    host = _host(url)
    parts = _path_parts(url)
    name = (title or "").strip() or None
    extra = {"affiliation": None, "works_count": None, "url": url}

    if any(host == h or host.endswith("." + h) for h in _SKIP_WEB):
        return None
    hay = f"{title or ''} {snippet or ''}"
    if _ADVICE_TITLE.search(hay):
        return None
    if host == "github.com" and parts:
        login = parts[0]
        if login.lower() not in _GITHUB_RESERVED and login[0] != ".":
            return {"source": "github", "external_id": login, "name": name or login, **extra}
        return None
    if host in ("huggingface.co", "hf.co") and parts:
        user = parts[0]
        if user.lower() not in _HF_RESERVED:
            return {"source": "huggingface", "external_id": user, "name": name or user, **extra}
        return None
    if host == "orcid.org" and parts:
        orcid = parts[0]
        if len(orcid) >= 19:
            return {"source": "orcid", "external_id": orcid, "name": name, **extra}
        return None
    if host in ("dblp.org", "dblp.uni-trier.de") and len(parts) >= 2 and parts[0] == "pid":
        pid = "/".join(parts[1:]).split(".html")[0]
        return {"source": "dblp", "external_id": pid, "name": name, **extra}
    if host == "openalex.org" and parts:
        ext = parts[-1]
        if ext.startswith("A"):
            return {"source": "openalex", "external_id": ext, "name": name, **extra}
        return None
    if host == "stackoverflow.com" and len(parts) >= 2 and parts[0] == "users":
        return {"source": "stackoverflow", "external_id": parts[1], "name": name, **extra}
    # leftover public page: personal site, lab page, portfolio
    return {"source": "web", "external_id": url, "name": name, **extra}

