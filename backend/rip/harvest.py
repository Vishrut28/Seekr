"""Bulk harvesting: page through a source's full catalogue, not one person
at a time.

Per-person API calls are how the corpus reaches hundreds. Cursor pagination
over a source's whole index is how it reaches millions — OpenAlex alone
exposes ~125M authors at 200 per request.

`harvest` writes JSONL that `rip.cli bulk-ingest` streams in, so the network
phase and the resolution phase can run separately (and the download can be
resumed or re-parsed without re-fetching).
"""

import gzip
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("rip.harvest")

OPENALEX_AUTHORS = "https://api.openalex.org/authors"
PER_PAGE = 200
# fields needed to build a NormalizedProfile; keeping the projection small
# is what makes a large harvest fast and polite
AUTHOR_SELECT = (
    "id,display_name,display_name_alternatives,orcid,"
    "last_known_institutions,topics,works_count,cited_by_count"
)


@dataclass
class HarvestResult:
    fetched: int = 0
    pages: int = 0
    out_file: str = ""
    next_cursor: str | None = None


def harvest_openalex(
    out_file: str,
    *,
    limit: int = 10_000,
    filter_expr: str | None = None,
    cursor: str = "*",
    mailto: str | None = None,
    progress=print,
) -> HarvestResult:
    """Page the OpenAlex author index into a JSONL file.

    `filter_expr` is OpenAlex filter syntax, e.g.
        "works_count:>50"
        "last_known_institutions.country_code:in"
        "topics.id:T10883"
    Passing None harvests the whole index (125M+ authors) — use `limit`.
    """
    mailto = mailto or os.environ.get("OPENALEX_MAILTO")
    result = HarvestResult(out_file=out_file)
    opener = gzip.open if out_file.endswith(".gz") else open
    headers = {"User-Agent": "resource-intelligence-platform/0.1 (research aggregator)"}

    with httpx.Client(timeout=60.0, headers=headers) as client, opener(
        out_file, "wt", encoding="utf-8"
    ) as sink:
        position: str | None = cursor
        while result.fetched < limit:
            params: dict[str, Any] = {
                "per-page": min(PER_PAGE, limit - result.fetched),
                "cursor": position,
                "select": AUTHOR_SELECT,
            }
            if filter_expr:
                params["filter"] = filter_expr
            if mailto:
                params["mailto"] = mailto  # polite pool: higher rate limits

            for attempt in range(4):
                try:
                    resp = client.get(OPENALEX_AUTHORS, params=params)
                    if resp.status_code == 429:
                        time.sleep(2**attempt)
                        continue
                    resp.raise_for_status()
                    break
                except Exception as exc:
                    if attempt == 3:
                        raise
                    logger.warning("harvest retry %s: %s", attempt, exc)
                    time.sleep(2**attempt)

            payload = resp.json()
            rows = payload.get("results") or []
            if not rows:
                break
            for author in rows:
                # shape each row the way OpenAlexConnector.renormalize expects
                sink.write(
                    json.dumps(
                        {
                            "id": author["id"].rsplit("/", 1)[-1],
                            "author": author,
                            "works": [],
                        }
                    )
                    + "\n"
                )
            result.fetched += len(rows)
            result.pages += 1
            position = (payload.get("meta") or {}).get("next_cursor")
            result.next_cursor = position
            if result.pages % 10 == 0:
                progress(f"  harvested {result.fetched:,} authors ({result.pages} pages)")
            if not position:
                break

    progress(f"harvested {result.fetched:,} authors -> {out_file}")
    return result


# Major Indian tech/research hubs. GitHub caps any single search at 1,000
# results, so a country-wide harvest must be sliced by city (and, within a
# city, by follower band) rather than paged past the cap.
INDIA_CITIES = [
    "Bangalore", "Bengaluru", "Mumbai", "Delhi", "New Delhi", "Hyderabad",
    "Chennai", "Pune", "Kolkata", "Ahmedabad", "Noida", "Gurgaon", "Gurugram",
    "Jaipur", "Kochi", "Chandigarh", "Indore", "Coimbatore", "Bhubaneswar",
    "Thiruvananthapuram", "Kanpur", "Lucknow", "Nagpur", "Surat", "India",
]
FOLLOWER_BANDS = [(1000, None), (200, 999), (50, 199), (10, 49)]


def harvest_github_india(
    out_file: str, *, limit: int = 5000, cities: list[str] | None = None,
    min_followers: int = 10, language: str | None = None, progress=print,
) -> HarvestResult:
    """Collect India-located GitHub logins into a JSONL file of identifiers.

    Writes {"external_id": login} lines; `bulk-ingest github` then fetches each
    profile. Search only yields logins, so the fetch is a separate, rate-limited
    step — which is why the two phases are kept apart.

    LANGUAGE narrows to people GitHub files under that language (its
    `language:` qualifier, from their repositories). "Python developers" found
    only Stack Overflow users with a python tag: the corpus held almost no one
    whose code shows the language.
    """
    from .connectors import get_connector

    connector = get_connector("github")
    result = HarvestResult(out_file=out_file)
    seen: set[str] = set()
    opener = gzip.open if out_file.endswith(".gz") else open

    with opener(out_file, "wt", encoding="utf-8") as sink:
        for city in cities or INDIA_CITIES:
            if result.fetched >= limit:
                break
            for low, high in FOLLOWER_BANDS:
                if result.fetched >= limit or low < min_followers:
                    break
                extra = f"followers:{low}..{high}" if high else f"followers:>={low}"
                if language:
                    extra = f"language:{language} {extra}"
                for page in range(1, 11):  # 10 pages x 100 = the 1,000 cap
                    if result.fetched >= limit:
                        break
                    try:
                        logins, total = connector.search_users(
                            location=city, query=extra, page=page, per_page=100
                        )
                    except Exception as exc:
                        progress(f"  {city} {extra}: {exc}")
                        break
                    if not logins:
                        break
                    for login in logins:
                        key = login.lower()
                        if key in seen:
                            continue
                        seen.add(key)
                        sink.write(json.dumps({"external_id": login}) + "\n")
                        result.fetched += 1
                    result.pages += 1
                    if len(logins) < 100:
                        break
            progress(f"  {city}: running total {result.fetched:,}")

    progress(f"harvested {result.fetched:,} India-located GitHub users -> {out_file}")
    return result
