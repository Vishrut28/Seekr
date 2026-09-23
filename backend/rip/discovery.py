"""Live discovery: asking the outside world about people the corpus lacks.

Split out of nlq.py, which had grown to 4,590 lines doing two unrelated jobs.
Everything here talks to a third-party API — searching it, throttling it when
it says no, fetching the profiles it named, and storing them. None of it is
needed to answer a query from the corpus we already hold, and nlq.py does not
call into this module; the dependency runs one way, from here back to the
parser for the handful of names below.

The split is deliberately without a compatibility re-export in nlq: a test
that still patches `rip.nlq.SUGGESTION_SEARCHERS` must fail loudly rather than
patch a name nothing reads and go on to hit the network for real.
"""

import logging
import os
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .nlq import (
    NOISE_WORDS,
    ROLE_MODIFIERS,
    NLQuery,
    _could_be_a_name,
    _looks_like_a_person,
)

logger = logging.getLogger("rip.discovery")


def _name_like_query(query: str) -> bool:
    """Is this worth asking an author-NAME index about?

    dblp and Semantic Scholar match names and nothing else, so a technology
    or a whole question produces surname collisions rather than answers.
    """
    words = query.split()
    return 1 <= len(words) <= 3 and all(_could_be_a_name(w) for w in words)


def _search_openalex(query: str, limit: int) -> list[dict]:
    from .connectors import get_connector

    conn = get_connector("openalex")
    # A one- or two-word query is a person's name far more often than a
    # subject, and works search answers it with whoever wrote a paper
    # mentioning the word — "sricharan" returned Justine S. Ko.
    # Author-NAME search is only ever right for something that could BE a
    # name. Asking it about "SaaS" returns people surnamed Saas, and about
    # "Rust" people surnamed Rust — a whole page of confident nonsense.
    name_ok = all(_could_be_a_name(t) for t in query.split()) and len(query.split()) <= 3
    order = (conn.search_authors, conn.search_authors_by_topic) if name_ok \
        else (conn.search_authors_by_topic,)
    candidates = []
    for search in order:
        candidates = [c for c in search(query, limit=limit)
                      if _looks_like_a_person(c.get("name"))]
        if candidates:
            break
    return [
        {
            "source": "openalex",
            "external_id": c["id"],
            "name": c.get("name"),
            "affiliation": c.get("affiliation"),
            "works_count": c.get("works_count"),
        }
        for c in candidates
    ]


def _search_europepmc(query: str, limit: int) -> list[dict]:
    """Europe PMC candidates: authors who publish with an ORCID.

    A second topical source, and the one that covers medicine and biology.
    It runs in the background alongside OpenAlex rather than after it, so its
    latency — which swings between half a second and several — is spent while
    the slowest provider is still working.
    """
    from .connectors import get_connector

    conn = get_connector("europepmc")
    name_ok = all(_could_be_a_name(t) for t in query.split()) and len(query.split()) <= 3
    order = (conn.search_authors, conn.search_authors_by_topic) if name_ok         else (conn.search_authors_by_topic,)
    candidates = []
    for search in order:
        candidates = [c for c in search(query, limit=limit)
                      if _looks_like_a_person(c.get("name"))]
        if candidates:
            break
    return [
        {
            "source": "europepmc",
            "external_id": c["id"],
            "name": c.get("name"),
            "affiliation": c.get("affiliation"),
            "works_count": c.get("works_count"),
        }
        for c in candidates
    ]


def _search_semanticscholar(query: str, limit: int) -> list[dict]:
    from .connectors import get_connector

    conn = get_connector("semanticscholar")
    if _name_like_query(query):
        found = conn.search_authors(query, limit=limit)
    elif (os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or "").strip():
        # Author search is a name index: asking it about "SaaS" returns people
        # named Saas. A subject goes through paper search — but only with a
        # key, since the shared pool mostly answers it with 429s.
        found = conn.search_authors_by_topic(query, limit=limit)
    else:
        return []
    return [
        {
            "source": "semanticscholar",
            "external_id": c["id"],
            "name": c.get("name"),
            "affiliation": (c.get("affiliations") or [None])[0],
            "works_count": c.get("papers"),
        }
        for c in found if _looks_like_a_person(c.get("name"))
    ]


def _search_exa(query: str, limit: int) -> list[dict]:
    """People search over professional profiles. Costs money per call, so it
    runs only when the free scholarly sources found nothing."""
    import os

    from .connectors import get_connector

    if not os.environ.get("EXA_API_KEY"):
        return []
    return [
        {
            "source": "exa",
            "external_id": c["id"],
            "name": c.get("name"),
            "affiliation": c.get("affiliation"),
            "role": c.get("role"),
            "location": c.get("location"),
            "works_count": None,
            # the full person payload we already paid for — ingesting it costs
            # nothing more, and means this query is answered locally next time
            "_raw": c.get("raw"),
            "_connector": "exa",
        }
        for c in get_connector("exa").search_people(query, limit=limit)
    ]


def _search_github(query: str, limit: int, parsed: "NLQuery | None" = None) -> list[dict]:
    """Working engineers — the population the scholarly indexes cannot see.

    Nobody publishes a paper about maintaining Kubernetes, so queries about
    tools and open-source work come back empty from OpenAlex and dblp while
    GitHub answers them directly.
    """
    from .connectors import get_connector

    conn = get_connector("github")
    # GitHub matches the literal string against login, name and bio, so a whole
    # question finds nobody. Try the phrase, then its individual words, most
    # distinctive first — "Kubernetes" is what identifies these people.
    # Query grammar must not become the search: "designers" matched the users
    # designerSejinOH and DESIGNERSWITHOUTBORDERS. Only distinctive words are
    # worth asking about, and a question with none of them has no answer here.
    terms = [
        t for t in re.split(r"[^A-Za-z0-9.+#-]+", query)
        if len(t) > 2 and t.lower() not in NOISE_WORDS and t.lower() not in ROLE_MODIFIERS
    ]
    if not terms:
        return []
    attempts = [query] if len(terms) > 1 else []
    attempts += sorted(set(terms), key=len, reverse=True)
    # GitHub can filter by location itself, so "community managers in
    # hyderabad" stops returning people anywhere in the world.
    place = None
    if parsed is not None and parsed.locations:
        place = str(parsed.locations[0]).split(",")[0].strip()

    logins: list[str] = []
    for attempt in attempts[:3]:
        for loc in ([place, None] if place else [None]):
            try:
                logins, _total = conn.search_users(
                    location=loc, query=attempt, per_page=limit
                )
            except Exception:
                continue    # a throttled search is not a query failure
            if logins:
                break
        if logins:
            break
    return [
        {"source": "github", "external_id": login, "name": login,
         "affiliation": None, "works_count": None}
        for login in logins
    ]


def _search_dblp(query: str, limit: int) -> list[dict]:
    from .connectors import get_connector

    # same as Semantic Scholar: dblp searches author names, nothing else
    if not _name_like_query(query):
        return []
    return [
        {
            "source": "dblp",
            "external_id": c["pid"],
            "name": c.get("name"),
            "affiliation": None,
            "works_count": None,
        }
        for c in get_connector("dblp").search_authors(query, limit=limit)
        if c.get("pid")
    ]


def _search_orcid(query: str, limit: int) -> list[dict]:
    from .connectors import get_connector

    if not _name_like_query(query):
        return []
    return [
        {
            "source": "orcid",
            "external_id": c["id"],
            "name": c.get("name"),
            "affiliation": c.get("affiliation"),
            "works_count": None,
        }
        for c in get_connector("orcid").search_authors(query, limit=limit)
        if c.get("id")
    ]


def _search_wikidata(query: str, limit: int) -> list[dict]:
    from .connectors import get_connector

    if not _name_like_query(query):
        return []
    return [
        {
            "source": "wikidata",
            "external_id": c["id"],
            "name": c.get("name"),
            "affiliation": None,
            "works_count": None,
        }
        for c in get_connector("wikidata").search_people(query, limit=limit)
        if c.get("id") and _looks_like_a_person(c.get("name"))
    ]


def _search_huggingface(query: str, limit: int) -> list[dict]:
    from .connectors import get_connector

    terms = [
        t for t in re.split(r"[^A-Za-z0-9.+#-]+", query)
        if len(t) > 2 and t.lower() not in NOISE_WORDS and t.lower() not in ROLE_MODIFIERS
    ]
    if not terms:
        return []
    q = terms[0] if _name_like_query(query) else max(terms, key=len)
    return [
        {
            "source": "huggingface",
            "external_id": c["id"],
            "name": c.get("name") or c["id"],
            "affiliation": None,
            "works_count": None,
        }
        for c in get_connector("huggingface").search_users(q, limit=limit)
        if c.get("id")
    ]


def _search_stackoverflow(query: str, limit: int) -> list[dict]:
    from .connectors import get_connector

    if not _name_like_query(query):
        return []
    return [
        {
            "source": "stackoverflow",
            "external_id": c["id"],
            "name": c.get("name"),
            "affiliation": None,
            "works_count": None,
        }
        for c in get_connector("stackoverflow").search_users(query, limit=limit)
        if c.get("id")
    ]


def _search_web(query: str, limit: int, parsed: "NLQuery | None" = None) -> list[dict]:
    """Public pages via TinyFish Search, routed to the connector that owns the URL."""
    from .tinyfish import configured, route_hit
    from .tinyfish import search as tf_search

    if not configured():
        return []
    bits: list[str] = []
    place = None
    if parsed is not None:
        bits.extend(parsed.roles[:2])
        bits.extend(parsed.skills[:2])
        bits.extend(parsed.name_terms[:2])
        if parsed.organizations:
            bits.append(f'"{parsed.organizations[0]}"')
        if parsed.locations:
            place = str(parsed.locations[0]).split(",")[0].strip()
            bits.append(place)
    text = " ".join(b for b in bits if b).strip() or query
    if not text:
        return []
    q = (
        f'{text} (github.com OR researcher OR engineer OR "personal website")'
        if parsed is not None and parsed.name_terms and not parsed.roles and not parsed.skills
        else (
            f'{text} (portfolio OR "personal website" OR "about me" '
            f'OR github.io OR researcher OR engineer)'
        )
    )
    hits = tf_search(q, limit=max(limit * 2, 8), location=place)
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for hit in hits:
        item = route_hit(hit["url"], hit.get("title"), hit.get("snippet"))
        if item is None:
            continue
        key = (item["source"], item["external_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


_search_tinyfish = _search_web


# tried in order; later sources only run if earlier ones found nothing useful
# (name, fn, uses_full_query). Author-name lookups want just the leftover
# terms; a semantic people search wants the whole question, because stripping
# "engineers at ... in Bengaluru" throws away the very context it matches on.
SUGGESTION_SEARCHERS = (
    ("openalex", _search_openalex, True),
    ("europepmc", _search_europepmc, True),
    ("semanticscholar", _search_semanticscholar, False),
    ("dblp", _search_dblp, False),
    ("orcid", _search_orcid, False),
    ("wikidata", _search_wikidata, False),
    ("github", _search_github, False),
    ("huggingface", _search_huggingface, False),
    ("stackoverflow", _search_stackoverflow, False),
    ("web", _search_web, True),
    # paid, and the only remaining source for closed professional graphs: last
    ("exa", _search_exa, True),
)


def enabled_searchers() -> tuple:
    """The live sources this deployment actually wants.

    RIP_SKIP_SOURCES turns one off without a code change, because what a
    source is worth differs by graph: Europe PMC adds people no other free
    source can identify, and costs a few seconds of a live search to do it.
    """
    skip = {s.strip().lower() for s in os.environ.get("RIP_SKIP_SOURCES", "").split(",")
            if s.strip()}
    return tuple(entry for entry in SUGGESTION_SEARCHERS if entry[0] not in skip)


def _wants_parsed(searcher) -> bool:
    """Does this searcher take the parsed query as a third argument?

    Kept optional so a test can still patch in a plain (query, limit) function.
    """
    import inspect

    try:
        return len(inspect.signature(searcher).parameters) >= 3
    except (TypeError, ValueError):
        return False


def _always_run(source: str) -> bool:
    """Sources that run even when earlier ones already found enough.

    GitHub covers a population the scholarly indexes structurally cannot —
    nobody publishes a paper about maintaining Kubernetes — so a query about
    tools would otherwise be answered by whichever academic wrote about the
    word. Its search is one request, so it always runs; fetching the profiles
    is what the unauthenticated 60/hour budget cannot afford.
    """
    return source in (
        # Europe PMC is here for a different reason: it is a topical source in
        # its own right, and running it beside OpenAlex rather than after it
        # hides its latency behind the slowest provider in the search.
        "europepmc",
        "github", "orcid", "wikidata", "huggingface", "stackoverflow", "web",
    )


# Generic circuit breaker for ANY external source, not just GitHub — every
# connector shares BaseConnector and so can raise the same RateLimitedError
# (see rip/connectors/base.py), which makes this detection uniform rather
# than a per-source guess at exception wording. Persisted in SourceThrottle
# (see its docstring) rather than a module-level Python global — the reason
# is multi-worker safety, not style: a plain global is per-process memory,
# invisible to sibling worker processes under a multi-worker deployment.
# GitHub was the first and, for a while, only source wired to this; the
# mechanism itself was always source-keyed and needed no schema change to
# generalize, only for every source's search/fetch failure handling to
# actually call it.
DEFAULT_THROTTLE_SECONDS = 900.0


# The longest backoff worth honouring, however long the source asks for. A
# source reporting a long window is usually reporting its quota RESET, not the
# time until the next request would work: a burst of parallel probes had
# OpenAlex answer 429 with an X-RateLimit-Reset at midnight, which took the
# graph's main scholarly source out for thirteen hours over a limit that
# clears in a second. Retrying every half hour costs one wasted request.
MAX_THROTTLE_SECONDS = 1800.0


def _source_throttled(session: Session, source: str) -> bool:
    from datetime import datetime, timezone

    from .models import SourceThrottle

    row = session.execute(
        select(SourceThrottle).where(SourceThrottle.source == source)
    ).scalar_one_or_none()
    if row is None:
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now < row.blocked_until


def _note_source_throttled(
    session: Session, source: str, seconds: float | None = None
) -> None:
    """Record that SOURCE just rate-limited us. SECONDS defaults to
    DEFAULT_THROTTLE_SECONDS when the source didn't tell us how long to
    back off (RateLimitedError.retry_after is None) — see that attribute's
    own docstring for when a real duration is available instead."""
    from datetime import datetime, timedelta, timezone

    from .models import SourceThrottle

    seconds = DEFAULT_THROTTLE_SECONDS if seconds is None else seconds
    seconds = min(seconds, MAX_THROTTLE_SECONDS)
    until = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=seconds)
    row = session.execute(
        select(SourceThrottle).where(SourceThrottle.source == source)
    ).scalar_one_or_none()
    if row is None:
        session.add(SourceThrottle(source=source, blocked_until=until))
    else:
        row.blocked_until = until
    session.commit()
    logger.warning("%s rate limited; backing off for %.0fs", source, seconds)


def _may_fetch_github(session: Session, stored_so_far: int) -> bool:
    """GitHub-specific POLICY on top of the generic throttle: with a token,
    always try (a token raises the ceiling to 5000/hour, so budget is rarely
    the constraint); without one, only when nothing else has answered yet —
    the unauthenticated 60/hour budget is too small to spend on a query the
    corpus already answered from other sources. Every OTHER source has no
    such per-request budget concern and just uses _source_throttled directly.
    """
    import os

    if _source_throttled(session, "github"):
        return False
    return bool(os.environ.get("GITHUB_TOKEN")) or stored_so_far == 0


MIN_USEFUL_SUGGESTIONS = 3


# providers that bill per call
PAID_SOURCES = ("exa",)


# A cached live search is reused for this long before the provider is asked
# again. Short enough that job changes surface within a week; long enough that
# repeating a search costs nothing.
SEARCH_TTL_DAYS = int(os.environ.get("SEEKR_SEARCH_TTL_DAYS", "7"))


def _norm_query(q: str) -> str:
    return " ".join(sorted(re.findall(r"[a-z0-9]+", q.lower())))[:512]


def _cache_lookup(session: Session, provider: str, query: str):
    """The cache row for this query if it is still fresh, else None."""
    from datetime import datetime, timedelta, timezone

    from .models import SearchCache

    row = session.execute(
        select(SearchCache).where(
            SearchCache.provider == provider,
            SearchCache.query_norm == _norm_query(query),
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    age = datetime.now(timezone.utc) - row.ran_at.replace(tzinfo=timezone.utc)
    return row if age < timedelta(days=SEARCH_TTL_DAYS) else None


def _cache_record(
    session: Session, provider: str, query: str, found: int, stored: int,
    person_ids: list[str] | None = None,
) -> None:
    from datetime import datetime, timezone

    from .models import SearchCache

    row = session.execute(
        select(SearchCache).where(
            SearchCache.provider == provider,
            SearchCache.query_norm == _norm_query(query),
        )
    ).scalar_one_or_none()
    if row is None:
        row = SearchCache(provider=provider, query_norm=_norm_query(query), query_raw=query[:512])
        session.add(row)
    row.result_count, row.stored_count = found, stored
    row.person_ids = person_ids or []
    row.ran_at = datetime.now(timezone.utc)
    session.commit()


# Sources whose full-profile fetch is a free public API, so a live search can
# be turned into real stored people without spending anything.
FREE_FETCH_SOURCES = (
    "openalex", "europepmc", "semanticscholar", "dblp", "github",
    "orcid", "wikidata", "huggingface", "stackoverflow", "web",
)


# GitHub costs two requests per profile against a 60/hour unauthenticated
# budget, so it fetches fewer than the scholarly APIs do. Web pages and
# Hub/SO profiles are similarly bounded.
PER_SOURCE_FETCH_LIMIT = {
    # Europe PMC answers an ORCID in anything from half a second to six, and
    # every record it returns merges onto a person by strong key, so a handful
    # is worth more than a page of them.
    "europepmc": 6,
    "github": 5, "web": 5, "huggingface": 5, "orcid": 5,
    "stackoverflow": 5, "wikidata": 5,
}


def _matches_only_the_name(profile, term: str) -> bool:
    """Did the query match this person's NAME and nothing else about them?

    That is the signature of a false hit. GitHub matches literal text against
    the login, so "AI researchers from Stanford" reached Intelligence247; a
    works search for "rust compiler engineers" returns papers by people
    surnamed Rust. In both cases the only thing connecting them to the query
    is what they are called.

    Demanding the reverse — that the query words appear somewhere in the
    profile — is too strict for a topical source: OpenAlex matched Xavier
    Denis on work about Rust verification, whose topics are recorded as
    program verification and formal methods. The provider did the semantic
    work; the profile need not echo the words back.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9+#.-]+", (term or "").lower()) if len(w) > 2]
    if not words:
        return False

    name_hay = " ".join(
        str(x).lower() for x in
        ([profile.name or ""] + list(profile.usernames or []) + list(profile.aliases or []))
    )
    body_hay = " ".join(
        str(x).lower() for x in (
            [profile.summary or "", profile.location or ""]
            + list(profile.organizations or [])
            + [e.value for e in (profile.evidence or [])]
            + [getattr(pr, "name", "") for pr in (profile.projects or [])]
            + [getattr(pr, "description", "") or "" for pr in (profile.projects or [])]
        )
    )
    return any(w in name_hay for w in words) and not any(w in body_hay for w in words)


MAX_FREE_FETCHES = 10


def persist_suggestions(session: Session, suggestions: list[dict]) -> int:
    """Ingest live results, so a query the corpus could not answer grows it.

    Two kinds of result arrive here. Exa returns the whole person record in the
    search response — already bought, so storing it costs nothing. The free
    scholarly sources return only an identifier, so the profile has to be
    fetched; that request is free, and bounded per query.

    Fetches run concurrently because they are independent HTTP calls and doing
    them one at a time put a live query near 30 seconds. Ingest stays on this
    thread: a SQLAlchemy session is not safe to share.
    """
    raw_items, to_fetch = _fetch_plan(suggestions)
    return _store_results(session, raw_items, _fetch_profiles(to_fetch))


# Wall-clock budget for one live search's profile fetches. A fetch that has not
# started when it runs out is skipped and reported on its item, so one slow or
# hanging source cannot hold a search open indefinitely.
LIVE_BUDGET_SECONDS = float(os.environ.get("RIP_LIVE_BUDGET_SECONDS", "30"))


# How long the SEARCH half may take. LIVE_BUDGET_SECONDS above bounds the
# fetches; nothing bounded the searches, so one provider having a bad day set
# the latency of the whole query -- measured: wikidata failing after 5.2s while
# openalex had answered at 2.3s and europepmc was already done. Sources still
# running when this expires are abandoned and reported as timed out; their
# threads touch no session, so letting them finish into nothing is safe.
LIVE_SEARCH_SECONDS = float(os.environ.get("RIP_LIVE_SEARCH_SECONDS", "8"))


def worth_searching_live(terms: list[str]) -> str:
    """Why a live search would be wasted on TERMS, or "" to go ahead.

    Only reasons that are certain. Gibberish is NOT one of them: "zqxjv
    plormbat" and "photonic metasurface" are the same shape to a parser, and
    the second is exactly the query live discovery exists for. Guessing which
    is which would refuse the novel subjects this feature is for.
    """
    if not terms:
        return "the query named nothing to search for"
    if not any(ch.isalpha() for ch in " ".join(terms)):
        return "no letter in the query: not a name or a subject"
    return ""


# Upper bound on concurrent fetches across ALL sources. Politeness toward any
# one source is enforced by its shared connector's request spacing, not here.
MAX_FETCH_WORKERS = 16


def _fetch_plan(suggestions: list[dict]) -> tuple[list[dict], list[dict]]:
    """(items carrying a full payload to store as-is, items to fetch)."""
    raw_items: list[dict] = []
    to_fetch: list[dict] = []
    per_source: dict[str, int] = {}
    overall_cap = MAX_FREE_FETCHES + sum(PER_SOURCE_FETCH_LIMIT.values())
    for item in suggestions:
        if item.get("_raw") and item.get("_connector"):
            raw_items.append(item)
            continue
        src = item.get("source")
        if src in FREE_FETCH_SOURCES and item.get("external_id"):
            cap = PER_SOURCE_FETCH_LIMIT.get(src, MAX_FREE_FETCHES)
            if per_source.get(src, 0) < cap and len(to_fetch) < overall_cap:
                per_source[src] = per_source.get(src, 0) + 1
                to_fetch.append(item)
    return raw_items, to_fetch


def _fetch_profiles(to_fetch: list[dict], deadline: float | None = None) -> list[tuple]:
    """Fetch and vet profiles concurrently. No database access — safe off-thread.

    Returns [(item, profile | None, exception | None)] in to_fetch order.
    """
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    from .connectors import get_connector

    if not to_fetch:
        return []
    if deadline is None:
        deadline = _time.monotonic() + LIVE_BUDGET_SECONDS

    def _pull(item: dict):
        try:
            if _time.monotonic() > deadline:
                raise TimeoutError("live search time budget spent before this fetch started")
            profile = get_connector(item["source"]).fetch(item["external_id"])
            if not (profile.name or "").strip():
                raise ValueError("source returned a profile with no name")
            term_words = {
                w for w in re.split(r"[^A-Za-z0-9+#.-]+", item.get("_term", "").lower())
                if len(w) > 2
            }
            term = item.get("_term", "")
            # A person named Rahul is the right answer to "Rahul". The
            # name-only check is for skill/role queries, where a surname
            # collision is not a connection.
            if not _name_like_query(term):
                name_words = {
                    w for w in re.split(r"[^A-Za-z0-9+#.-]+", (profile.name or "").lower())
                    if len(w) > 2
                }
                # A scholarly index holds records named after subjects, not
                # only after people: searching "hypertension arterial
                # stiffness researchers" stored an author called "ARTERIAl
                # STIffnESS". Every word of a name being a word of the query
                # is what gives those away — one word in common was the old
                # test, and a two-word subject walked straight through it.
                if name_words and name_words <= term_words:
                    raise ValueError(
                        f"{item['external_id']} is named after the search term, not a person"
                    )
                if _matches_only_the_name(profile, term):
                    raise ValueError(
                        f"{item['external_id']} matches '{term}' only in "
                        "their name — that is a coincidence, not a connection"
                    )
            return item, profile, None
        except Exception as exc:
            # Detection only — NOT the DB write. _pull runs on a
            # ThreadPoolExecutor worker thread (see pool.map below), and
            # a SQLAlchemy Session is not safe to share across threads
            # (the same constraint ingest_profile's own callers already
            # respect elsewhere in this file). The throttle write happens
            # in _store_results, on the calling thread.
            return item, None, exc

    # Sources that can batch part of a fetch (OpenAlex: every author record in
    # one request) do so first; a failed prefetch only means each fetch asks
    # for itself, as before.
    by_source: dict[str, list[str]] = {}
    for item in to_fetch:
        by_source.setdefault(item["source"], []).append(item["external_id"])

    def _prefetch(source: str) -> None:
        connector = get_connector(source)
        if hasattr(connector, "prefetch") and len(by_source[source]) > 1:
            try:
                connector.prefetch(by_source[source])
            except Exception:
                logger.debug("prefetch failed for %s", source, exc_info=True)

    workers = max(1, min(MAX_FETCH_WORKERS, len(to_fetch)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_prefetch, list(by_source)))
        return list(pool.map(_pull, to_fetch))


def _store_results(session: Session | None, raw_items: list[dict], fetched: list[tuple]) -> int:
    """Ingest payload items and fetched profiles, in order, on this thread."""
    from .connectors import get_connector
    from .connectors.base import RateLimitedError
    from .ingest import ingest_profile

    if session is None:
        return 0
    stored = 0

    def _keep(item: dict, profile) -> None:
        nonlocal stored
        try:
            person = ingest_profile(session, profile)
            item["stored"] = True
            item["person_id"] = str(person.id)
            stored += 1
        except Exception as exc:
            # surfaced on the item so a caller can see the record was found
            # but not kept, rather than it vanishing silently
            item["stored"] = False
            item["store_error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("could not persist %s result: %s", item.get("source"), exc)
            session.rollback()

    for item in raw_items:
        try:
            _keep(item, get_connector(item["_connector"]).normalize(item["_raw"]))
        except Exception as exc:
            item["stored"] = False
            item["store_error"] = f"{type(exc).__name__}: {exc}"

    for item, profile, exc in fetched:
        if exc is not None:
            item["stored"] = False
            item["store_error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("could not fetch %s result: %s", item.get("source"), exc)
            # Every connector shares BaseConnector and so raises the SAME
            # RateLimitedError on a genuine rate limit, so this backoff
            # applies uniformly to every source's fetch step.
            if isinstance(exc, RateLimitedError) and item.get("source"):
                _note_source_throttled(session, item["source"], exc.retry_after)
            continue
        _keep(item, profile)
    return stored


def discovery_suggestions(
    session: Session | None = None, parsed: NLQuery = None, limit: int = 10,
    allow_paid: bool = True, on_source=None, persist: bool = True,
) -> list[dict]:
    """Live author search across sources for terms the local corpus lacks.

    Returns suggestions, and by default KEEPS what a source handed over in
    full — the data is already bought and paid for, so storing it means the
    next person asking is answered from the graph rather than from the
    provider again. persist=False searches and returns without writing, for
    a caller who is not allowed to change the corpus. Sources are tried in
    order and later ones are skipped once enough candidates are found, so the
    common case still costs a single upstream call. A failing source is
    skipped rather than failing the query.

    `on_source(name, state, **facts)` is called as each source is reached, so
    a caller can show progress while the work happens rather than only once
    all of it is finished. States: searching, done, cached, skipped, failed.
    """
    from .connectors.base import RateLimitedError

    def report(name: str, state: str, **facts) -> None:
        if on_source is None:
            return
        try:
            on_source(name, state, **facts)
        except Exception:      # a progress listener must never break a search
            logger.debug("progress listener failed for %s", name, exc_info=True)

    terms = parsed.unmatched_terms + parsed.name_terms
    if not terms:
        terms = parsed.skills[:1]
    skip = worth_searching_live(terms)
    if skip:
        # "!!! ??? ***" and "the and of in" reached eight providers and cost
        # 1.5-2 seconds each to be told nothing, every time they were asked.
        logger.info("no live search: %s", skip)
        report("live", "skipped", reason=skip)
        return []
    query = " ".join(terms).strip()
    # stable across vocabulary changes, unlike the derived `query`
    cache_key = (parsed.raw or query).strip()
    if not cache_key:
        return []

    out: list[dict] = []
    total_stored = 0
    # person ids from cached searches: found before, still the right answer
    replayed: list[str] = []

    def check_cache_or_skip(source: str) -> tuple[str, str] | None:
        """Cache/paid/skip gate for one source. Returns (query, mode) to
        actually search with, or None if the source was fully handled here
        (skipped or answered from cache) and needs no network call."""
        if _source_throttled(session, source):
            report(source, "skipped", reason="rate limited, backing off")
            return None
        if len(out) >= MIN_USEFUL_SUGGESTIONS and not _always_run(source):
            report(source, "skipped", reason="enough found already")
            return None
        if not allow_paid and source in PAID_SOURCES:
            report(source, "skipped", reason="metered source, not enabled for this search")
            return None
        # Already bought this answer recently? The people are in the graph, so
        # do not pay for it again. Keyed on the USER'S query, not the derived
        # search string: once new people are stored the residual string
        # changes, and keying on that would bill for the same request twice.
        cached = _cache_lookup(session, source, cache_key) if session is not None else None
        if cached:
            # Free, but not empty: replay the people this search already found,
            # so a repeat query returns the same answers it did the first time.
            cached.hits = (cached.hits or 0) + 1
            replayed.extend(cached.person_ids or [])
            session.commit()
            report(source, "cached", found=cached.result_count or 0,
                   people=len(cached.person_ids or []))
            return None
        return "ok"

    def run_search(source: str, searcher, uses_full_query: bool):
        """The network call only — no session access, safe to run off-thread."""
        # A question made entirely of role words ("product designers who have
        # worked on developer tools") leaves no residue at all. Fall back to
        # what the user actually typed rather than searching for nothing.
        search_for = (cache_key if uses_full_query else query) or cache_key
        report(source, "searching", query=search_for)
        try:
            found = (
                searcher(search_for, limit, parsed)
                if _wants_parsed(searcher) else searcher(search_for, limit)
            )
            return search_for, found, None
        except Exception as exc:
            # a throttled or unavailable source is not a query failure
            return search_for, None, exc

    def failed(source: str, exc) -> None:
        report(source, "failed", reason=f"{type(exc).__name__}")
        # Every connector shares BaseConnector and raises the SAME
        # RateLimitedError type on a genuine rate limit (see its docstring) —
        # checking isinstance means the SEARCH step backs off uniformly for
        # every source, not just GitHub's fetch step.
        if isinstance(exc, RateLimitedError):
            _note_source_throttled(session, source, exc.retry_after)

    def annotate(source: str, search_for: str, found) -> list[dict]:
        keep = []
        for item in found or []:
            if not item.get("external_id"):
                continue
            item["reason"] = f"live {source} search for '{search_for}'"
            item["ingest_command"] = f"rip.cli ingest {source} {item['external_id']}"
            item["_term"] = search_for
            keep.append(item)
        return keep

    def github_budget_denied(source: str, keep: list[dict], stored_so_far: int) -> bool:
        if source != "github" or _may_fetch_github(session, stored_so_far):
            return False
        # keep them as candidates to add, but do not spend the request
        # budget pulling full profiles
        for item in keep:
            item["fetch_skipped"] = "github rate limit — set GITHUB_TOKEN"
        return True

    def finish(source: str, keep: list[dict], stored: int) -> None:
        nonlocal total_stored
        total_stored += stored
        report(source, "done", found=len(keep), stored=stored)
        # A free source that found nothing is not worth remembering: nothing
        # was bought, and caching the miss would block the retry that a better
        # parse or a wider corpus would have answered. Paid providers still
        # cache their misses — that is where the money is.
        if session is not None and (keep or source in PAID_SOURCES):
            _cache_record(
                session, source, cache_key, len(keep), stored,
                [i["person_id"] for i in keep if i.get("person_id")],
            )
        out.extend(keep)

    def apply_result(source: str, search_for: str, found, exc) -> None:
        """Persist + cache-record one source's search result, on this thread."""
        if exc is not None:
            failed(source, exc)
            return
        keep = annotate(source, search_for, found)
        if github_budget_denied(source, keep, total_stored):
            stored = 0
        else:
            stored = (persist_suggestions(session, keep)
                      if session is not None and persist else 0)
        finish(source, keep, stored)

    # Sources split into three phases, preserving the exact behavior of the
    # original single sequential loop:
    #
    #   A. openalex, semanticscholar, dblp — each one's "enough found
    #      already" skip decision depends on results the ones before it in
    #      this same phase found, so these stay genuinely sequential.
    #   B. orcid, wikidata, github, huggingface, stackoverflow, web — every
    #      one of these always runs regardless of how much has been found
    #      (`_always_run`), so their SEARCH calls (independent HTTP requests)
    #      can fire concurrently instead of one after another. Persisting
    #      results still happens on this thread, sequentially, in the
    #      original order — a SQLAlchemy session is not safe to share across
    #      threads, and github's rate-budget check needs total_stored to
    #      reflect earlier sources exactly as it did before.
    #   C. exa — paid and last, its skip decision depends on the total found
    #      across everything above, so it stays sequential too.
    from concurrent.futures import ThreadPoolExecutor

    searchers = enabled_searchers()
    phase_a = [(s, fn, full) for s, fn, full in searchers
               if not _always_run(s) and s not in PAID_SOURCES]
    phase_b = [(s, fn, full) for s, fn, full in searchers if _always_run(s)]
    _handled = {s for s, _fn, _full in phase_a} | {s for s, _fn, _full in phase_b}
    phase_c = [(s, fn, full) for s, fn, full in searchers if s not in _handled]

    def run_sequential_phase(phase) -> None:
        for source, searcher, uses_full_query in phase:
            # These run one after another, so a slow one spends the budget of
            # everyone behind it. Measured: OpenAlex took 70 seconds for a
            # single search while the rest of the phase waited its turn. The
            # call already running cannot be interrupted -- BaseConnector caps
            # that at request_timeout -- but nothing after it needs to start.
            if _time.monotonic() > search_deadline:
                report(source, "skipped", reason="live search budget spent")
                continue
            if check_cache_or_skip(source) is None:
                continue
            search_for, found, exc = run_search(source, searcher, uses_full_query)
            apply_result(source, search_for, found, exc)

    import time as _time

    deadline = _time.monotonic() + LIVE_BUDGET_SECONDS
    search_deadline = _time.monotonic() + LIVE_SEARCH_SECONDS

    # Phase B's searches never depended on phase A — every one of them always
    # runs — so they start NOW, in the background, and are answered while
    # phase A works. Their cache/throttle gates touch the session, so those
    # are decided here on this thread before anything is submitted.
    runnable = [(s, fn, full) for s, fn, full in phase_b if check_cache_or_skip(s) is not None]
    b_order = [s for s, _fn, _full in phase_b if s in {r[0] for r in runnable}]
    # GitHub with a token (and not backing off) is always fetched, so its
    # profiles can be pulled ahead too. Without a token its budget depends on
    # what phase A stores, which is only known later. Decided here because
    # the throttle check reads the session, which the background must not.
    github_eager = bool(os.environ.get("GITHUB_TOKEN")) and "github" in b_order \
        and not _source_throttled(session, "github")

    def phase_b_ahead():
        """Background: each phase B source searched and then fetched, on its
        own, so a slow search delays only its own profiles.

        One shared wait for every search came first, and it cost whatever the
        slowest source that query happened to hit: Europe PMC answering in
        8 seconds instead of its usual 1.4 held back five other sources'
        fetches that had nothing to do with it. No session access in here.
        """
        def pipeline(source, searcher, uses_full_query):
            search_for, found, exc = run_search(source, searcher, uses_full_query)
            if exc is not None:
                return source, (exc, [], [], []), {}
            keep = annotate(source, search_for, found)
            raw_items, to_fetch = _fetch_plan(keep) if session is not None else ([], [])
            plan = (None, keep, raw_items, to_fetch)
            if source == "github" and not github_eager:
                return source, plan, {}      # its budget depends on phase A
            fetched = _fetch_profiles(to_fetch, deadline)
            return source, plan, dict(zip(map(id, to_fetch), fetched))

        from concurrent.futures import wait as _wait

        pool = ThreadPoolExecutor(max_workers=max(1, len(runnable)))
        try:
            futures = {pool.submit(pipeline, *args): args[0] for args in runnable}
            finished, running = _wait(futures, timeout=LIVE_SEARCH_SECONDS)
            for late_future in running:
                report(futures[late_future], "timed out")
            # in the order they were declared, so results stay stable
            by_source = {futures[fut]: fut for fut in finished}
            done = [by_source[s].result() for s, _fn, _full in runnable
                    if s in by_source]
        finally:
            # abandoned pipelines touch no session; letting them finish into
            # nothing costs less than blocking the answer on them
            pool.shutdown(wait=False)
        plans = {source: plan for source, plan, _ in done}
        results: dict = {}
        for _source, _plan, fetched in done:
            results.update(fetched)
        return plans, results

    ahead_pool = ThreadPoolExecutor(max_workers=1) if runnable else None
    ahead = ahead_pool.submit(phase_b_ahead) if ahead_pool else None

    try:
        # Phase A: openalex, semanticscholar, dblp — each one's "enough found
        # already" skip decision depends on results the ones before it in
        # this same phase found, so these stay genuinely sequential. Phase B
        # searches and fetches run meanwhile.
        run_sequential_phase(phase_a)

        if ahead is not None:
            plans, results = ahead.result()
            prepared = []           # (source, keep, raw_items, to_fetch)
            planned_before = 0      # fetchable candidates from earlier sources
            late: list[dict] = []
            for s in b_order:
                if s not in plans:
                    continue        # abandoned at LIVE_SEARCH_SECONDS
                exc, keep, raw_items, to_fetch = plans[s]
                if exc is not None:
                    failed(s, exc)
                    continue
                # GitHub's unauthenticated budget is spent only when nothing
                # else answered. Earlier sources' fetches have not been stored
                # yet, so their planned candidates count as answers — never
                # more permissive than waiting for them would have been.
                if github_budget_denied(s, keep, total_stored + planned_before):
                    prepared.append((s, keep, [], []))
                    continue
                if s == "github" and not github_eager:
                    late.extend(to_fetch)
                planned_before += len(raw_items) + len(to_fetch)
                prepared.append((s, keep, raw_items, to_fetch))
            results.update(zip(map(id, late), _fetch_profiles(late, deadline)))
            # Stored on this thread, in the ORIGINAL declared order — a
            # Session is not thread-safe, and order keeps results stable.
            for s, keep, raw_items, to_fetch in prepared:
                fetched = [results[id(item)] for item in to_fetch]
                finish(s, keep, _store_results(session, raw_items, fetched)
                       if persist else 0)
    finally:
        if ahead_pool is not None:
            ahead_pool.shutdown(wait=True)

    # Phase C: exa — paid and last, its skip decision depends on the total
    # found across everything above, so it stays sequential.
    run_sequential_phase(phase_c)
    result = out[: limit * 2]
    # Replayed people carry no suggestion payload — they are already in the
    # graph. They ride along as id-only entries so the caller can include them
    # in results, and are not shown as "live candidates" to add.
    seen_ids = {i.get("person_id") for i in result}
    result.extend(
        {"person_id": pid, "replayed": True}
        for pid in dict.fromkeys(replayed)
        if pid not in seen_ids
    )
    return result


def queue_suggestions(session: Session, suggestions: list[dict], query: str) -> int:
    """Add suggestions to the discovery-lead queue for a worker to ingest.

    Still no ingest inside the request: this only records intent, and the
    normal lead pipeline (rate limits, enrichment, resolution) applies when a
    worker picks it up.
    """
    from .models import DiscoveryLead, SourceRecord

    added = 0
    for item in suggestions:
        source, external_id = item.get("source"), item.get("external_id")
        if not source or not external_id:
            continue
        exists = session.execute(
            select(DiscoveryLead.id).where(
                DiscoveryLead.source == source, DiscoveryLead.identifier == external_id
            )
        ).first()
        already = session.execute(
            select(SourceRecord.id).where(
                SourceRecord.source == source, SourceRecord.external_id == external_id
            )
        ).first()
        if exists or already:
            continue
        session.add(
            DiscoveryLead(
                source=source,
                identifier=external_id,
                reason=f"queued from search '{query}': {item.get('name')}"[:1000],
            )
        )
        added += 1
    session.commit()
    return added
