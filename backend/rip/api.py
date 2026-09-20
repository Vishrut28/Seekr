"""Read API: faceted filtering, lookup, and ranked natural-language search.

Stable person UUIDs, full provenance, and a /changes feed for incremental sync.

Two search surfaces, deliberately different. `/v1/persons` filters and never
orders by fitness, so a downstream tool can apply its own scoring to a raw
match set. `/v1/query` ranks, because a natural-language question is a request
for the best answers, not for every answer in insertion order — every result
carries the score and the evidence components behind it.
"""

from datetime import datetime

import contextvars
import os
import pathlib
import re
import shutil

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import String as SAString
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from pathlib import Path

from .db import READ_ONLY, SessionLocal, init_db

# Fewer results than this is a thin answer, and thin is worth topping up from
# live sources even though it is not empty. Scaled down per applied filter —
# see _applied_filter_count and its use in nl_query.
THIN_ANSWER = 10


def _applied_filter_count(parsed) -> int:
    """How many distinct constraints this query actually applied.

    Used to judge whether a short result list is "thin" (the corpus came up
    short) or simply "precise" (the query asked for something specific and
    got exactly that). Mirrors nlq.has_filters's list of filter groups.
    """
    return sum(bool(x) for x in (
        parsed.skill_groups, parsed.organizations, parsed.locations,
        parsed.countries, parsed.name_terms, parsed.roles,
    ))
from .nlq import _word_match, place_mentioned  # whole-word matching, shared with the parser
from .models import (
    Affiliation,
    Authorship,
    ChangeLog,
    Contribution,
    Evidence,
    IdentityLink,
    IngestionRun,
    Organization,
    Person,
    Project,
    Publication,
    SourceRecord,
)

app = FastAPI(
    title="Seekr",
    description="Evidence-backed resource data layer. /v1/query ranks by evidence; /v1/persons filters without ordering.",
    version="0.1.0",
)


@app.middleware("http")
async def bearer_auth(request, call_next):
    """If RIP_API_TOKEN is set, every /v1 route requires it as a Bearer token."""
    import os

    from starlette.responses import JSONResponse

    token = os.environ.get("RIP_API_TOKEN")
    if token and request.url.path.startswith("/v1"):
        supplied = request.headers.get("authorization", "")
        if supplied != f"Bearer {token}":
            return JSONResponse({"detail": "invalid or missing bearer token"}, status_code=401)
    return await call_next(request)


@app.get("/v1/auth")
def auth_required() -> dict:
    """Whether this deployment wants a token at all.

    It sits behind the middleware above on purpose: with RIP_API_TOKEN set,
    the probe is answered with 401 before it reaches here, and the UI knows to
    ask for a token. Unset, it answers plainly and the UI does not.
    """
    return {"required": False}


@app.on_event("startup")
def _startup() -> None:
    try:
        init_db()
    except Exception:
        # read-only deployments serve a pre-built snapshot; if the DB is
        # missing/immutable, let routes fail individually instead of
        # killing the whole app at startup
        import logging

        logging.getLogger("rip").exception("init_db failed at startup")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _person_summary(p: Person) -> dict:
    return {
        "id": p.id,
        "merged_into": p.merged_into,
        "canonical_name": p.canonical_name,
        "aliases": p.aliases,
        "location": p.location or place_mentioned(p.summary),
        "summary": p.summary,
        "current_role": p.current_role,
        "current_organization": p.current_organization,
        "profile_urls": p.profile_urls,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def _evidence_dict(e: Evidence) -> dict:
    return {
        "id": e.id,
        "attribute_type": e.attribute_type,
        "value": e.value,
        "extracted_info": e.extracted_info,
        "source": e.source,
        "url": e.url,
        "source_record_id": e.source_record_id,
        "observed_at": e.observed_at,
        "published_at": e.published_at,
        "confidence": e.confidence,
        "verification_state": e.verification_state,
    }


FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

if FRONTEND_DIR.is_dir():
    # styles.css, app.js and the logo, straight off disk: editing the frontend
    # means editing those files, with no Python involved
    from starlette.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/ui")
def ui():
    """The exploration UI. Its files live in frontend/, not in this package."""
    from starlette.responses import HTMLResponse

    index = FRONTEND_DIR / "index.html"
    if not index.exists():
        raise HTTPException(500, f"frontend not found at {FRONTEND_DIR}")
    return HTMLResponse(index.read_text())


@app.get("/")
def root():
    return {
        "service": "Seekr",
        "version": "0.1.0",
        "docs": "/docs",
        "ui": "/ui",
        "endpoints_prefix": "/v1",
        "auth": "Authorization: Bearer <token> required on /v1 routes",
    }



@app.get("/v1/persons")
def list_persons(
    q: str | None = Query(None, description="name / alias substring"),
    skill: str | None = Query(None, description="skill, interest or specialization (substring)"),
    organization: str | None = Query(None, description="any affiliation, current or past (substring)"),
    current_organization: str | None = Query(None, description="present employer only"),
    education: str | None = Query(None, description="where they studied (substring)"),
    role: str | None = Query(None, description="job title (substring)"),
    country: str | None = Query(None, description="ISO-3166 alpha-2, e.g. IN, US, DE"),
    location: str | None = Query(None, description="free-text place (substring)"),
    source: str | None = Query(None, description="has a record from this source"),
    technology: str | None = Query(None, description="technology used in a project"),
    min_publications: int | None = Query(None, ge=0),
    min_citations: int | None = Query(None, ge=0),
    min_sources: int | None = Query(None, ge=1, description="corroborated across N sources"),
    active_since: str | None = Query(None, description="published or pushed since YYYY or YYYY-MM-DD"),
    updated_since: datetime | None = Query(None, description="record changed since this time"),
    has_cv: bool | None = Query(None, description="has a published CV/résumé link"),
    has_email: bool | None = Query(None, description="has a public email"),
    sort: str = Query("relevance", description="relevance (insertion order) | recent | name"),
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Faceted people search.

    Every parameter is a *filter*, never a score: `sort` only reorders by a
    factual field (recency, name) and never by fitness for a role. Ranking
    stays in the downstream tool.
    """
    from sqlalchemy import and_, exists as sa_exists

    from . import search_index as si

    stmt = select(Person).where(Person.merged_into.is_(None))

    # Text filters answered by the search index: one seek-driven lookup for all
    # of them instead of a LIKE scan each. Same whole-word semantics as the
    # SQL below, which still handles anything the index cannot express — the
    # loose "go*" form — and every database whose index is not built yet.
    indexed: set[str] = set()
    if si.is_ready(db):
        text_filters = (
            ("q", q, ("n", "na")), ("skill", skill, ("s",)),
            ("organization", organization, ("ow",)), ("education", education, ("ew",)),
            ("current_organization", current_organization, ("co",)),
            ("role", role, ("r",)), ("location", location, ("l",)),
            ("technology", technology, ("k",)),
        )
        constraints = []
        for name, value, fields in text_filters:
            if not value or value.strip().endswith("*"):
                continue
            alts = [a for a in (si.phrase_alt(f, value) for f in fields) if a]
            if alts:
                constraints.append(si.Constraint(name, alts))
                indexed.add(name)
        if country and country.strip():
            constraints.append(si.Constraint("country", [si.exact_alt("c", country.strip().lower())]))
            indexed.add("country")
        if constraints:
            matched = si.match_select(db, constraints)
            stmt = stmt.where(Person.id.in_(matched) if matched is not None else Person.id.is_(None))

    if q and "q" not in indexed:
        stmt = stmt.where(
            _word_match(Person.canonical_name, q)
            | func.lower(func.cast(Person.aliases, SAString)).like(f'%"{q.lower()}%')
        )
    if skill and "skill" not in indexed:
        stmt = stmt.where(sa_exists().where(and_(
            Evidence.person_id == Person.id,
            Evidence.attribute_type.in_(["skill", "research_interest", "specialization"]),
            _word_match(Evidence.value, skill),
        )))
    for value, relation, name in ((organization, None, "organization"),
                                  (education, "studied_at", "education")):
        if not value or name in indexed:
            continue
        conds = [
            Affiliation.person_id == Person.id,
            Organization.id == Affiliation.organization_id,
            _word_match(Organization.name, value),
        ]
        if relation:
            conds.append(Affiliation.relation == relation)
        stmt = stmt.where(sa_exists().where(and_(*conds)))
    if current_organization and "current_organization" not in indexed:
        stmt = stmt.where(_word_match(Person.current_organization, current_organization))
    if role and "role" not in indexed:
        stmt = stmt.where(
            _word_match(Person.current_role, role)
            | sa_exists().where(and_(
                Affiliation.person_id == Person.id,
                _word_match(Affiliation.role, role),
            ))
        )
    if country and "country" not in indexed:
        # A stated country, or a location that names it. Without the second
        # half, country=IN missed every GitHub developer whose location reads
        # "Bangalore, India" — the source gave a place, not an ISO code.
        from .nlq import names_for_country

        clauses = [func.upper(Person.country) == country.upper()]
        for name in names_for_country(country):
            clauses.append(_word_match(Person.location, name))
        stmt = stmt.where(or_(*clauses))
    if location and "location" not in indexed:
        stmt = stmt.where(_word_match(Person.location, location))
    if source:
        stmt = stmt.where(sa_exists().where(and_(
            IdentityLink.person_id == Person.id,
            SourceRecord.id == IdentityLink.source_record_id,
            SourceRecord.source == source.lower(),
        )))
    if technology and "technology" not in indexed:
        stmt = stmt.where(sa_exists().where(and_(
            Contribution.person_id == Person.id,
            Project.id == Contribution.project_id,
            _word_match(func.cast(Project.technologies, SAString), technology),
        )))
    if has_cv is not None:
        cv = sa_exists().where(and_(
            Evidence.person_id == Person.id, Evidence.attribute_type == "cv_url"
        ))
        stmt = stmt.where(cv if has_cv else ~cv)
    if has_email is not None:
        mail = sa_exists().where(and_(
            PersonKey.person_id == Person.id, PersonKey.key_type == "email"
        ))
        stmt = stmt.where(mail if has_email else ~mail)
    if updated_since:
        stmt = stmt.where(Person.updated_at >= updated_since)
    if active_since:
        stmt = stmt.where(sa_exists().where(and_(
            Authorship.person_id == Person.id,
            Publication.id == Authorship.publication_id,
            Publication.published_date >= active_since,
        )))
    if min_publications:
        stmt = stmt.where(
            select(func.count(Authorship.id))
            .where(Authorship.person_id == Person.id)
            .scalar_subquery() >= min_publications
        )
    if min_citations:
        stmt = stmt.where(
            select(func.coalesce(func.sum(Publication.citations), 0))
            .select_from(Authorship)
            .join(Publication, Publication.id == Authorship.publication_id)
            .where(Authorship.person_id == Person.id)
            .scalar_subquery() >= min_citations
        )
    if min_sources:
        stmt = stmt.where(
            select(func.count(IdentityLink.id))
            .where(IdentityLink.person_id == Person.id)
            .scalar_subquery() >= min_sources
        )

    total = db.execute(
        select(func.count()).select_from(
            stmt.with_only_columns(Person.id).distinct().subquery()
        )
    ).scalar_one()

    order = {
        "recent": Person.updated_at.desc(),
        "name": Person.canonical_name.asc(),
    }.get(sort)
    # `sort="relevance"` deliberately means "no fitness ranking" here — that
    # judgement belongs to /v1/query, not this endpoint — but paging still
    # has to be reproducible: without ANY order_by, which rows land on which
    # page is left to the engine and isn't guaranteed stable across two
    # identical requests. Person.id costs nothing extra and makes
    # offset/limit paging stable without expressing an opinion about who the
    # "best" match is. It also breaks ties within "recent"/"name" sort, where
    # two people can otherwise share a sort key.
    #
    # GROUP BY only when actually needed: Postgres rejects an ORDER BY
    # expression that isn't in the SELECT list when DISTINCT is used, which
    # "recent" (Person.updated_at) and "name" (Person.canonical_name) both
    # violate — GROUP BY has no such restriction. But ordering by Person.id
    # while selecting Person.id is always legal DISTINCT + ORDER BY (the
    # order column IS the select-list column), and measurably cheaper:
    # GROUP BY forces SQLite into "USE TEMP B-TREE FOR GROUP BY" — a full
    # materialize-and-sort of every matching row before LIMIT can apply —
    # even for the common case with no join/fan-out risk at all. Confirmed
    # via EXPLAIN QUERY PLAN and a real benchmark (roughly 2x slower on a
    # 10k-person corpus for the default sort) before narrowing this back
    # down to only the two sorts that actually require it.
    if order is not None:
        page = stmt.with_only_columns(Person.id).group_by(Person.id).order_by(order, Person.id)
    else:
        page = stmt.with_only_columns(Person.id).distinct().order_by(Person.id)
    ids = db.execute(page.limit(limit).offset(offset)).scalars().all()
    rows = {p.id: p for p in db.execute(
        select(Person).where(Person.id.in_(ids))).scalars()} if ids else {}
    persons = [rows[i] for i in ids if i in rows]

    response = {
        "count": len(persons),
        "total_matches": total,
        "has_more": offset + len(persons) < total,
        "next_offset": offset + len(persons) if offset + len(persons) < total else None,
        "results": [_person_summary(p) for p in persons],
    }
    # The reader chose these on a form, where each one is a labelled field —
    # "dropping q would return 70" names a parameter nobody saw, and q is the
    # least guessable of them all: on this endpoint it filters by name.
    labels = {
        "q": "name", "current_organization": "current employer",
        "education": "studied at", "min_publications": "min publications",
        "min_citations": "min citations", "min_sources": "min sources",
        "active_since": "active since", "updated_since": "updated since",
        "has_cv": "has a CV", "has_email": "has an email",
    }

    def label(name: str) -> str:
        return labels.get(name, name)

    if total == 0 and not _DIAGNOSING.get():
        # Which filter emptied it? Combining five filters and getting nothing
        # says nothing about which one to relax, so each is measured on its
        # own and each is also dropped in turn.
        active = {
            "q": q, "skill": skill, "organization": organization,
            "current_organization": current_organization, "education": education,
            "role": role, "country": country, "location": location,
            "source": source, "technology": technology,
            "min_publications": min_publications, "min_citations": min_citations,
            "min_sources": min_sources, "active_since": active_since,
            "updated_since": updated_since, "has_cv": has_cv, "has_email": has_email,
        }
        active = {k: v for k, v in active.items() if v is not None and v != ""}

        def _count(subset: dict) -> int | None:
            reset = _DIAGNOSING.set(True)
            try:
                return list_persons(
                    **{**_ALL_FILTERS_NONE, **subset, "db": db}
                )["total_matches"]
            except Exception:
                return None
            finally:
                _DIAGNOSING.reset(reset)

        alone, blockers = [], []
        for name, value in active.items():
            on_its_own = _count({name: value})
            alone.append({"filter": name, "value": value, "matches": on_its_own})
            if on_its_own == 0:
                continue
            if len(active) > 1:
                without = _count({k: v for k, v in active.items() if k != name})
                if without:
                    blockers.append({"filter": name, "value": value, "without_it": without})

        dead = [a for a in alone if a["matches"] == 0]
        if dead:
            message = "Nothing matches " + ", ".join(
                f"{label(a['filter'])}={a['value']}" for a in dead
            ) + " at all — check the spelling, or add * for a loose match."
        elif blockers:
            message = "No one matches every filter at once. " + "; ".join(
                f"dropping {label(b['filter'])} would return {b['without_it']:,}"
                for b in blockers
            )
        else:
            message = (
                "Each filter matches people on its own, but no one satisfies them "
                "all — at least two have to be relaxed."
            )
        response["empty_reason"] = {
            "message": message,
            "each_filter_alone": alone,
            "relaxing": blockers,
        }
    return response


# The diagnostic below re-runs list_persons with fewer filters, and those runs
# can be empty too. Without this guard each one would diagnose itself and the
# recursion never bottoms out. A ContextVar rather than a plain flag so
# concurrent requests do not switch each other's diagnostics off.
_DIAGNOSING = contextvars.ContextVar("rip_diagnosing", default=False)


# every filter name defaulted to None, so a diagnostic call can pass a subset
_ALL_FILTERS_NONE = {
    "q": None, "skill": None, "organization": None, "current_organization": None,
    "education": None, "role": None, "country": None, "location": None,
    "source": None, "technology": None, "min_publications": None,
    "min_citations": None, "min_sources": None, "active_since": None,
    "updated_since": None, "has_cv": None, "has_email": None,
    "sort": "relevance", "limit": 1, "offset": 0,
}


@app.get("/v1/facets")
def facets(
    field: str = Query(..., description="country | source | organization | skill | role | technology"),
    limit: int = Query(30, le=200),
    db: Session = Depends(get_db),
):
    """Available filter values and how many people carry each.

    Counts describe the corpus; they are not a ranking of people.

    Every call is a GROUP BY over a whole table (technology flattens every
    project in Python), and the UI asks for several on each page load. The
    answer only changes when people are written, so it is cached per
    database: invalidated at once by writes in this process, and after
    RIP_VOCAB_TTL seconds for writes made by a separate ingest process.
    """
    import time as _time

    from . import search_index as si

    per_db = _FACET_CACHE.setdefault(si._bind_key(db), {})
    key = (si.generation(db), field, limit)
    hit = per_db.get(key)
    if hit is not None and _time.monotonic() - hit[0] < _FACET_TTL:
        return hit[1]
    result = _facets_uncached(field, limit, db)
    if len(per_db) > 256:
        per_db.clear()
    per_db[key] = (_time.monotonic(), result)
    return result


import weakref as _weakref

_FACET_CACHE: "_weakref.WeakKeyDictionary" = _weakref.WeakKeyDictionary()
_FACET_TTL = float(os.environ.get("RIP_VOCAB_TTL", "60"))


def _facets_uncached(field: str, limit: int, db: Session) -> dict:
    if field == "country":
        # A menu entry is a promise about what picking it returns, so it has
        # to count the people the FILTER matches. Counting the stated country
        # column alone said "IN - 48" over a filter that returns 70: the
        # filter also accepts a location that names the country, because a
        # source often gives "Bangalore, India" and no ISO code at all.
        from . import search_index as si

        if si.is_ready(db):
            rows = db.execute(
                select(si.SearchTerm.term, func.count(si.SearchTerm.person_id))
                .join(Person, Person.id == si.SearchTerm.person_id)
                .where(si.SearchTerm.field == "c", Person.merged_into.is_(None))
                .group_by(si.SearchTerm.term)
                .order_by(func.count(si.SearchTerm.person_id).desc()).limit(limit)
            ).all()
            rows = [(code.upper(), n) for code, n in rows if code]
        else:
            rows = db.execute(
                select(Person.country, func.count(Person.id))
                .where(Person.country.isnot(None), Person.merged_into.is_(None))
                .group_by(Person.country).order_by(func.count(Person.id).desc()).limit(limit)
            ).all()
    elif field == "source":
        from .connectors import CONNECTORS
        from .nlq import PAID_SOURCES, enabled_searchers

        rows = db.execute(
            select(SourceRecord.source, func.count(func.distinct(IdentityLink.person_id)))
            .join(IdentityLink, IdentityLink.source_record_id == SourceRecord.id)
            .group_by(SourceRecord.source).order_by(func.count(IdentityLink.id).desc())
        ).all()
        counts = {v: n for v, n in rows if v}
        # Always list every free connector this deployment runs, even when a
        # source has nobody stored yet — the sidebar was counting only
        # ingested sources and dropped to 8 until web/HF/SO had a person.
        catalog = [n for n, _fn, _full in enabled_searchers() if n not in PAID_SOURCES]
        for name in CONNECTORS:
            if name not in PAID_SOURCES and name not in catalog:
                catalog.append(name)
        values = [{"value": s, "people": int(counts.get(s, 0))} for s in catalog]
        values.extend(
            {"value": s, "people": int(counts[s])}
            for s in counts if s not in CONNECTORS
        )
        return {"field": field, "values": values}
    elif field == "organization":
        rows = db.execute(
            select(Organization.name, func.count(func.distinct(Affiliation.person_id)))
            .join(Affiliation, Affiliation.organization_id == Organization.id)
            .group_by(Organization.id)
            .order_by(func.count(func.distinct(Affiliation.person_id)).desc()).limit(limit)
        ).all()
    elif field in ("skill", "role"):
        types = (["skill", "research_interest", "specialization"] if field == "skill"
                 else ["role"])
        rows = db.execute(
            select(Evidence.value, func.count(func.distinct(Evidence.person_id)))
            .where(Evidence.attribute_type.in_(types))
            .group_by(Evidence.value)
            .order_by(func.count(func.distinct(Evidence.person_id)).desc()).limit(limit)
        ).all()
    elif field == "technology":
        # The `technology` PERSON filter searches Project.technologies (see
        # list_persons), not Evidence — a different vocabulary entirely from
        # "skill" (project tech stacks like "Rust"/"Kubernetes" versus
        # research/skill descriptions like "Distributed Systems"). Without
        # its own facet here, the only autocomplete available for this
        # filter field was the skill facet — real, but the wrong list,
        # meaning a suggested value could return zero results and the
        # actual matching values were never surfaced at all.
        #
        # Project.technologies is a JSON array column, not a plain string
        # column — there is no single SQL expression that unnests and
        # counts it identically on SQLite and Postgres (SQLite's json_each()
        # and Postgres's jsonb_array_elements are unrelated functions), so
        # this is flattened and counted in Python instead, matching how
        # _output_signals elsewhere in this file already prefers a plain
        # Python aggregation over engine-specific SQL for the same reason.
        from .models import Contribution, Project

        rows = db.execute(
            select(Project.technologies, Contribution.person_id)
            .join(Contribution, Contribution.project_id == Project.id)
        ).all()
        by_tech: dict[str, set[str]] = {}
        for techs, pid in rows:
            for t in (techs or []):
                t = str(t).strip()
                if t:
                    by_tech.setdefault(t, set()).add(pid)
        values = sorted(
            ({"value": t, "people": len(pids)} for t, pids in by_tech.items()),
            key=lambda v: -v["people"],
        )[:limit]
        return {"field": field, "values": values}
    else:
        raise HTTPException(422, "field must be country, source, organization, skill, role or technology")
    return {"field": field, "values": [{"value": v, "people": n} for v, n in rows if v]}


@app.get("/v1/persons/{person_id}")
def get_person(person_id: str, db: Session = Depends(get_db)):
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(404, "person not found")
    if person.merged_into:
        # tombstone: old IDs stay resolvable and point at the canonical person
        canonical = db.get(Person, person.merged_into)
        out = _person_summary(canonical)
        out["requested_id"] = person_id
        return out
    summary = _person_summary(person)
    # aggregated attribute view: value + how many sources attest it
    rows = db.execute(
        select(
            Evidence.attribute_type,
            Evidence.value,
            func.count(Evidence.id),
            func.max(Evidence.confidence),
        )
        .where(Evidence.person_id == person_id)
        .group_by(Evidence.attribute_type, Evidence.value)
    ).all()
    sources_by_claim: dict = {}
    for e in db.execute(select(Evidence).where(Evidence.person_id == person_id)).scalars():
        sources_by_claim.setdefault((e.attribute_type, e.value), set()).add(e.source)
    summary["attributes"] = [
        {
            "attribute_type": at,
            "value": val,
            "evidence_count": n,
            "max_confidence": conf,
            "sources": sorted(sources_by_claim.get((at, val), [])),
        }
        for at, val, n, conf in rows
    ]
    return summary


@app.get("/v1/persons/{person_id}/evidence")
def get_evidence(
    person_id: str,
    attribute_type: str | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(Evidence).where(Evidence.person_id == person_id)
    if attribute_type:
        stmt = stmt.where(Evidence.attribute_type == attribute_type)
    rows = db.execute(stmt).scalars().all()
    return {"person_id": person_id, "evidence": [_evidence_dict(e) for e in rows]}


@app.get("/v1/persons/{person_id}/publications")
def get_publications(person_id: str, db: Session = Depends(get_db)):
    rows = db.execute(
        select(Publication, Authorship.author_position)
        .join(Authorship, Authorship.publication_id == Publication.id)
        .where(Authorship.person_id == person_id)
    ).all()
    return {
        "person_id": person_id,
        "publications": [
            {
                "id": pub.id,
                "title": pub.title,
                "venue": pub.venue,
                "published_date": pub.published_date,
                "url": pub.url,
                "doi": pub.doi,
                "citations": pub.citations,
                "topics": pub.topics,
                "authors": pub.raw_authors,
                "author_position": pos,
            }
            for pub, pos in rows
        ],
    }


@app.get("/v1/persons/{person_id}/projects")
def get_projects(person_id: str, db: Session = Depends(get_db)):
    rows = db.execute(
        select(Project, Contribution.role)
        .join(Contribution, Contribution.project_id == Project.id)
        .where(Contribution.person_id == person_id)
    ).all()
    return {
        "person_id": person_id,
        "projects": [
            {
                "id": proj.id,
                "name": proj.name,
                "description": proj.description,
                "url": proj.url,
                "technologies": proj.technologies,
                "organization": proj.organization,
                "activity": proj.activity,
                "started_at": proj.started_at,
                "last_active_at": proj.last_active_at,
                "role": role,
            }
            for proj, role in rows
        ],
    }


@app.get("/v1/persons/{person_id}/organizations")
def get_organizations(person_id: str, db: Session = Depends(get_db)):
    rows = db.execute(
        select(Affiliation, Organization)
        .join(Organization, Organization.id == Affiliation.organization_id)
        .where(Affiliation.person_id == person_id)
    ).all()
    return {
        "person_id": person_id,
        "affiliations": [
            {
                "organization": org.name,
                "org_type": org.org_type,
                "website": org.website,
                "relation": aff.relation,
                "role": aff.role,
                "start_date": aff.start_date,
                "end_date": aff.end_date,
                "is_current": aff.is_current,
                "url": aff.url,
            }
            for aff, org in rows
        ],
    }


@app.get("/v1/persons/{person_id}/documents")
def get_documents(person_id: str, db: Session = Depends(get_db)):
    """CVs/resumes and profile pages this person publishes.

    Every link was found on a page or API record we fetched — Seekr never
    constructs, guesses or hosts a document. `found_on` is the page the link
    was taken from, so any link can be traced back to its source.
    """
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(404, "person not found")

    cvs = db.execute(
        select(Evidence).where(
            Evidence.person_id == person_id, Evidence.attribute_type == "cv_url"
        )
    ).scalars().all()

    def kind(u: str) -> str:
        host = (u.split("//")[-1].split("/")[0] or "").lower().removeprefix("www.")
        for domain, label in (
            ("github.com", "code"), ("orcid.org", "researcher id"),
            ("dblp.org", "bibliography"), ("openalex.org", "scholarly profile"),
            ("semanticscholar.org", "scholarly profile"),
            ("scholar.google.com", "scholarly profile"),
            ("huggingface.co", "models"), ("stackoverflow.com", "q&a"),
            ("wikidata.org", "knowledge base"), ("wikipedia.org", "encyclopedia"),
            ("linkedin.com", "professional profile"),
        ):
            if host == domain or host.endswith("." + domain):
                return label
        return "web page"

    return {
        "person_id": person_id,
        "note": "links are published by the person or their institution; none are generated by Seekr",
        "cvs": [
            {
                "url": e.value,
                "found_on": e.url,
                "evidence": e.extracted_info,
                "source": e.source,
                "confidence": e.confidence,
                "observed_at": e.observed_at,
            }
            for e in cvs
        ],
        "profiles": [
            {"url": u, "kind": kind(u)} for u in (person.profile_urls or [])
        ],
    }


@app.get("/v1/persons/{person_id}/graph")
def get_graph(
    person_id: str,
    depth: int = Query(1, ge=1, le=3, description="co-author hops from the person (1-3)"),
    limit_coauthors: int = Query(20, ge=1, le=100,
                                 description="strongest collaborators followed per person"),
    max_nodes: int = Query(200, ge=1, le=1000, description="stop adding people past this"),
    db: Session = Depends(get_db),
):
    """Neighborhood of one person: organizations, and co-authors out to DEPTH hops.

    Each person contributes at most LIMIT_COAUTHORS edges, their strongest by
    shared publications, and the walk stops adding people at MAX_NODES
    (`truncated` says when it did). Both caps bound the payload; the
    shared-publication count is a factual edge weight, not a ranking of people.
    Organizations are drawn for the person asked about only.
    """
    from .graph import neighborhood

    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(404, "person not found")
    return neighborhood(db, person, depth=depth, limit_coauthors=limit_coauthors,
                        max_nodes=max_nodes)


@app.get("/v1/persons/{person_id}/conflicts")
def get_conflicts(person_id: str, db: Session = Depends(get_db)):
    """Attribute disagreements between sources, both sides with provenance."""
    from .models import AttributeConflict

    rows = db.execute(
        select(AttributeConflict).where(AttributeConflict.person_id == person_id)
    ).scalars().all()
    return {
        "person_id": person_id,
        "conflicts": [
            {
                "id": c.id,
                "attribute": c.attribute_name,
                "side_a": {"value": c.value_a, "source": c.source_a,
                           "source_record_id": c.source_record_a_id},
                "side_b": {"value": c.value_b, "source": c.source_b,
                           "source_record_id": c.source_record_b_id},
                "status": c.status,
                "created_at": c.created_at,
            }
            for c in rows
        ],
    }


@app.get("/v1/persons/{person_id}/provenance")
def get_provenance(person_id: str, db: Session = Depends(get_db)):
    """Answers: where did we get this person's data?"""
    rows = db.execute(
        select(IdentityLink, SourceRecord)
        .join(SourceRecord, SourceRecord.id == IdentityLink.source_record_id)
        .where(IdentityLink.person_id == person_id)
    ).all()
    return {
        "person_id": person_id,
        "sources": [
            {
                "source": rec.source,
                "source_type": rec.source_type,
                "external_id": rec.external_id,
                "url": rec.url,
                "first_observed": rec.first_observed,
                "last_observed": rec.last_observed,
                "extracted_at": rec.extracted_at,
                "match_method": link.match_method,
                "match_confidence": link.match_confidence,
                "match_signals": link.signals,
                "review_state": link.review_state,
            }
            for link, rec in rows
        ],
    }


@app.get("/v1/changes")
def get_changes(
    since_id: int = Query(0, description="integer cursor: changes with id > this (preferred)"),
    since: datetime | None = Query(None, description="legacy ISO-timestamp cursor"),
    limit: int = Query(500, le=5000),
    db: Session = Depends(get_db),
):
    """Incremental sync feed. Poll with next_cursor; the integer cursor is
    monotonic and has no clock-skew/tie issues (prefer it over `since`)."""
    stmt = select(ChangeLog).order_by(ChangeLog.id)
    if since_id:
        stmt = stmt.where(ChangeLog.id > since_id)
    elif since:
        stmt = stmt.where(ChangeLog.changed_at > since)
    # over-fetch one row to compute has_more without a count query
    rows = db.execute(stmt.limit(limit + 1)).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "changes": [
            {
                "id": c.id,
                "person_id": c.person_id,
                "field": c.field,
                "old_value": c.old_value,
                "new_value": c.new_value,
                "changed_at": c.changed_at,
                "source_record_id": c.source_record_id,
            }
            for c in rows
        ],
        "next_cursor": rows[-1].id if rows else since_id,
        "has_more": has_more,
        "next_since": rows[-1].changed_at if rows else since,  # legacy
    }


@app.get("/v1/review/merges")
def review_merges(db: Session = Depends(get_db)):
    """Suspicious resolution decisions awaiting human review."""
    from .review import list_suspicious

    return list_suspicious(db)


@app.post("/v1/review/merges/{link_id}/approve")
def review_approve(link_id: int, db: Session = Depends(get_db)):
    from .review import approve_link

    try:
        link = approve_link(db, link_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"link_id": link.id, "review_state": link.review_state}


@app.post("/v1/review/merges/{link_id}/split")
def review_split(link_id: int, db: Session = Depends(get_db)):
    from .review import split_link

    try:
        person = split_link(db, link_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"new_person_id": person.id, "canonical_name": person.canonical_name}


@app.get("/v1/review/conflations")
def review_conflations(
    above: float = Query(0.5, ge=0.0, le=1.0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Person records whose papers look like more than one person's work.

    Its own route, not part of /v1/review/merges: this reads stored source
    payloads and takes a moment, and the merge queue should not wait for it.

    Half of what it returns is one person with a wide career — the detector is
    measured at 60% precision — so every entry carries the groups themselves,
    with years, subjects and titles, because that is what the decision is
    actually made on.
    """
    from .conflation import candidates, describe, shares_an_employer
    from .models import ConflationReview, PersonSplit

    seen = {
        row.person_id: row.verdict
        for row in db.execute(select(ConflationReview)).scalars()
    }
    # Somebody has already pulled papers off these. A record mid-way through
    # being taken apart usually still holds more than one person, and the
    # employer check is only 60% precise — it dropped one of these the moment
    # its remaining halves both listed King Khalid University, while the
    # record still mixed agricultural economics with hydrology and petroleum
    # recovery. Work in progress outweighs a heuristic, so these stay until
    # somebody says they are done.
    started = {
        person_id for (person_id,) in db.execute(select(PersonSplit.from_person_id))
    }
    out = []
    # The employer check is applied per candidate below rather than inside
    # candidates(), so a record being worked on can opt out of it.
    for split in candidates(db, above=above, check_employer=False):
        if split.person_id in seen:
            continue                     # somebody has already ruled on this one
        employer = shares_an_employer(db, split)
        if employer is True and split.person_id not in started:
            continue                     # probably one person with a wide career
        out.append({
            "person_id": split.person_id,
            "person_name": split.name,
            "score": round(split.score, 2),
            "papers": split.papers,
            "shares_an_employer": employer,
            "split_already": split.person_id in started,
            # the ids are what a split acts on; the titles are what a reader
            # decides on, so both go
            "group_ids": [sorted(g) for g in split.groups if len(g) >= 2],
            "groups": describe(db, split, per_group=4),
        })
        if len(out) >= limit:
            break
    return {"conflations": out, "reviewed": len(seen)}


@app.post("/v1/review/conflations/{person_id}")
def review_conflation(person_id: str, payload: dict, db: Session = Depends(get_db)):
    """Record a judgement so the record stops being offered.

    The detector recomputes from the papers every time and cannot remember
    anything, so without this a record somebody has already cleared comes back
    for ever. Neither verdict changes a Person: "several_people" marks work for
    a splitting tool that does not exist yet, and nothing here pretends to do
    it.
    """
    from .models import ConflationReview, Person

    verdict = str(payload.get("verdict") or "")
    if verdict not in ("one_person", "several_people"):
        raise HTTPException(400, "verdict must be one_person or several_people")
    if db.get(Person, person_id) is None:
        raise HTTPException(404, "person not found")
    row = db.execute(
        select(ConflationReview).where(ConflationReview.person_id == person_id)
    ).scalar_one_or_none()
    if row is None:
        row = ConflationReview(person_id=person_id)
        db.add(row)
    row.verdict = verdict
    row.note = (payload.get("note") or None)
    db.commit()
    return {"person_id": person_id, "verdict": verdict, "recorded": True}


@app.post("/v1/review/conflations/{person_id}/split")
def split_person(person_id: str, payload: dict, db: Session = Depends(get_db)):
    """Move a group of papers off this person onto a new one.

    Takes publication ids rather than a group number: a group is recomputed
    from the papers on every request, so its position is not a name for
    anything and would silently point at a different set later.

    The source record is frozen afterwards — it describes both people and
    re-ingesting it would put them back together. See rip.split.
    """
    from .split import split_off

    ids = payload.get("publication_ids")
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        raise HTTPException(400, "publication_ids must be a list of integers")
    try:
        out = split_off(db, person_id, ids,
                        name=(payload.get("name") or None),
                        note=(payload.get("note") or None))
    except ValueError as exc:
        # every refusal here is "that is not a split", not a server fault
        raise HTTPException(400, str(exc))
    return {
        "from_person_id": out.from_person_id,
        "to_person_id": out.to_person_id,
        "papers_moved": out.papers_moved,
        "subjects_rewritten": out.evidence_rewritten,
        "affiliations_moved": out.affiliations_moved,
        "frozen_source_record": out.frozen_record_id,
    }


@app.post("/v1/review/duplicates/{candidate_id}/merge")
def review_duplicate_merge(candidate_id: int, db: Session = Depends(get_db)):
    from .review import resolve_duplicate

    try:
        return resolve_duplicate(db, candidate_id, "merge")
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post("/v1/review/duplicates/{candidate_id}/reject")
def review_duplicate_reject(candidate_id: int, db: Session = Depends(get_db)):
    from .review import resolve_duplicate

    try:
        return resolve_duplicate(db, candidate_id, "reject")
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/v1/query")
def nl_query(
    q: str = Query(..., min_length=2, description="natural-language query"),
    limit: int = Query(0, le=500, description="override the page size (0 = query default)"),
    offset: int = Query(0, ge=0, description="skip this many matches (paging)"),
    discover: str = Query(
        "auto",
        description="auto (default) = search the FREE live sources when the "
        "corpus cannot answer, and keep what they return; true = also allow "
        "metered providers; false = local corpus only; queue = also add "
        "candidates to the discovery-lead queue for a worker.",
    ),
    db: Session = Depends(get_db),
):
    """Natural-language search. Read-only, ranked by evidence.

    The response is explicit about which terms became filters and which
    could not be applied; never assume an unlisted constraint was enforced.
    With `discover=true`, a query the corpus cannot answer additionally
    returns `discovery_suggestions` — candidates from live searches across
    OpenAlex, Semantic Scholar and dblp that an operator may choose to
    ingest. `discover=queue` also adds them to the discovery-lead queue so a
    worker ingests them later. Neither mode ingests during the request.
    Suggestions are not results and are not ranked.
    """
    from .nlq import (count_matches, diagnose_empty, execute, execute_progressive,
                      has_filters, parse, query_understanding, subjects_asked)

    # tolerate direct calls (tests) where FastAPI has not resolved the params
    limit = limit if isinstance(limit, int) else 0
    offset = offset if isinstance(offset, int) else 0
    asked = parse(db, q)
    if limit:
        asked.limit = limit
    asked.offset = offset
    persons, parsed, not_found = execute_progressive(db, asked)
    matched_nothing = not has_filters(asked)
    total = count_matches(db, parsed) if has_filters(parsed) else 0
    # Per-person evidence/affiliation cache, shared across every
    # build_results() call in this request — not just within one call. When
    # live discovery fires, execute_progressive() re-runs and build_results()
    # is called a second time; most person ids overlap with the first call
    # (the same corpus matches, now alongside anyone newly stored), and
    # re-querying their evidence/affiliations is pure duplicate work — an
    # EXISTING person's evidence/affiliations do not change mid-request,
    # only new people are ingested by discovery. Keyed on person_id so the
    # second call only queries the delta (new/extra people), not everyone.
    _attr_cache: dict = {}
    _org_cache: dict = {}

    def build_results(rows):
        """Summaries plus a small evidence-count attribute sample per person."""
        ids = [r.id for r in rows]
        new_ids = [pid for pid in ids if pid not in _attr_cache]
        for pid in new_ids:
            _attr_cache[pid] = {}
            _org_cache[pid] = []
        if new_ids:
            for pid, at, val, src in db.execute(
                select(Evidence.person_id, Evidence.attribute_type, Evidence.value,
                       Evidence.source)
                .where(Evidence.person_id.in_(new_ids),
                       Evidence.attribute_type.in_(["skill", "research_interest"]))
            ).all():
                entry = _attr_cache[pid].setdefault(
                    (at, val),
                    {"attribute_type": at, "value": val, "evidence_count": 0, "sources": set()},
                )
                entry["evidence_count"] += 1
                if src:
                    entry["sources"].add(src)
            for pid, org_name in db.execute(
                select(Affiliation.person_id, Organization.name)
                .join(Organization, Organization.id == Affiliation.organization_id)
                .where(Affiliation.person_id.in_(new_ids))
            ).all():
                if org_name not in _org_cache[pid]:
                    _org_cache[pid].append(org_name)

        wanted_orgs = {o.lower() for o in parsed.organizations}
        out = []
        for person in rows:
            summary = _person_summary(person)
            attrs = [
                {**a, "sources": sorted(a["sources"])}
                for a in _attr_cache.get(person.id, {}).values()
            ]
            summary["attributes"] = sorted(attrs, key=lambda a: -a["evidence_count"])[:6]
            summary["organizations"] = _org_cache.get(person.id, [])
            # the affiliation that satisfied the org filter — often NOT the
            # current one, so showing only current_organization looks wrong
            summary["matched_organization"] = next(
                (o for o in summary["organizations"] if o.lower() in wanted_orgs), None
            )
            # Why this person ranks where they do. A score with no breakdown is
            # an assertion; the components name the evidence behind it.
            # a partial match says which constraints it does not meet
            partial = getattr(person, "partial_match", None)
            summary["match"] = "partial" if partial else "full"
            if partial:
                summary["missing"] = partial["missing"]
            relevance = getattr(person, "relevance", None)
            if relevance:
                summary["score"] = relevance.get("score")
                summary["score_components"] = relevance.get("components")
                summary["matched_evidence"] = relevance.get("matched_evidence")
            out.append(summary)
        return out

    results = build_results(persons)

    response = {
        "query": q,
        "applied_filters": {
            # what was asked for; `skills` is what it resolved to
            "subjects": subjects_asked(parsed),
            "skills": parsed.skills,
            "skill_patterns": parsed.skill_patterns,
            "organizations": parsed.organizations,
            "locations": parsed.locations,
            "countries": parsed.countries,
            "name_terms": parsed.name_terms, "roles": parsed.roles,
            "limit": parsed.limit,
            "offset": parsed.offset,
        },
        "applied_clauses": [
            {"term": c["token"], "as": c["label"]} for c in (parsed.clause_order or [])
        ],
        "not_found": not_found,
        "unmatched_terms": parsed.unmatched_terms,
        # asked to select people by gender, religion, age, ...: never applied
        "protected_terms": parsed.protected_terms,
        # "not at Google", "both X and Y", "at least 20 papers"
        **query_understanding(parsed),
        # what we searched for instead of what was typed, so a corrected
        # query never silently answers a different question
        "corrections": parsed.corrections,
        # count = this page; total_matches = everything the filters match
        "count": len(persons),
        "total_matches": total,
        "has_more": parsed.offset + len(persons) < total,
        "next_offset": parsed.offset + len(persons) if parsed.offset + len(persons) < total else None,
        "results": results,
        # nothing in the query matched the corpus vocabulary, so no filter was
        # applied; returning rows here would be arbitrary, not an answer
        "matched_nothing": matched_nothing,
        "explanation": (
            "None of these terms exist in the corpus, so no filter could be "
            "applied and no results are returned. Try discover=true to search "
            "live sources."
            if matched_nothing
            else None
        ),
        "empty_reason": (diagnose_empty(db, parsed) if (not persons and not matched_nothing) else None),
        # Worth going live whenever the corpus could not answer fully: nothing
        # found, a constraint we had to drop, or a thin answer. "Nothing found"
        # alone was the old test, and it quietly stopped firing as the graph
        # grew — at 50,000 people almost every query returns something, so the
        # corpus stopped growing from searches exactly when it looked healthy.
        #
        # "Thin" is judged relative to how constrained the query was, not
        # against one flat number. A query with several applied filters is
        # SUPPOSED to return a short, precise list — "5 filters, 4 results" is
        # a correct, specific answer, not a weak one, and treating it as thin
        # sent every precise query through a live-search round trip it didn't
        # need. Each extra applied constraint halves what counts as thin
        # enough to top up, down to a floor of 1 (still worth going live if
        # even a tightly-filtered query comes back completely empty-handed
        # after one match — but 2+ is left alone).
        "discover_available": bool(
            not persons or asked.unmatched_terms or not_found
            or len(persons) < max(1, THIN_ANSWER >> max(0, _applied_filter_count(parsed) - 1))
        ),
        # on the deployed read-only snapshot a live search still answers the
        # question, but nothing it finds can be kept — say so rather than
        # letting the corpus look mysteriously frozen
        "storage": "read-only" if READ_ONLY else "writable",
    }
    mode = str(discover).lower()
    # "auto" is the default: a question the corpus cannot answer is exactly
    # when live search is worth doing, and the free sources cost nothing.
    # Metered providers stay opt-in.
    allow_paid = mode in ("true", "queue", "1")
    explicit = mode in ("true", "queue", "1")
    # An explicit request is a request. discover_available is a hint for the
    # UI about whether the button is worth pressing, not a veto over someone
    # who already pressed it.
    if explicit or (mode == "auto" and response["discover_available"]):
        from .nlq import discovery_suggestions, queue_suggestions

        # Live results are persisted when the provider returned a full person
        # payload: we already paid for that data, so keeping it means the same
        # query is answered from the graph next time instead of being re-bought.
        suggestions = discovery_suggestions(db, parsed, allow_paid=allow_paid)
        stored = sum(1 for s in suggestions if s.get("stored"))
        # id-only entries replayed from a cached search: they belong in the
        # results, not in the list of candidates a user can add
        replayed = [s for s in suggestions if s.get("replayed")]
        suggestions = [s for s in suggestions if not s.get("replayed")]
        for s in suggestions:
            s.pop("_raw", None)
            s.pop("_connector", None)
        response["discovery_suggestions"] = suggestions
        response["stored_from_live"] = stored
        response["replayed_from_cache"] = len(replayed)
        if stored or replayed:
            # The corpus just grew, and so did the vocabulary: terms that were
            # unmatched a moment ago (a company name we had never seen) are now
            # real filters. Re-parse before re-running, or the people we just
            # stored stay invisible to the very query that fetched them.
            from .nlq import invalidate_vocab

            invalidate_vocab()  # the cache predates the people we just stored
            # A "stored" suggestion is not always a brand-new person:
            # ingest_profile() runs identity resolution, which can attach a
            # freshly-discovered profile's evidence to someone who ALREADY
            # existed in the corpus (and so may already be sitting in
            # _attr_cache/_org_cache from the FIRST build_results() call
            # above). Evict exactly those ids before the second call, so the
            # cache-reuse optimization only ever skips a query for someone
            # whose evidence provably did not change in between — never for
            # someone discovery may have just added to.
            for s_ in suggestions:
                touched_pid = s_.get("person_id")
                if touched_pid:
                    _attr_cache.pop(touched_pid, None)
                    _org_cache.pop(touched_pid, None)
            asked = parse(db, q)
            if limit:
                asked.limit = limit
            asked.offset = offset
            persons, parsed, not_found = execute_progressive(db, asked)
            response["not_found"] = not_found
            response["applied_clauses"] = [
                {"term": c["token"], "as": c["label"]} for c in (parsed.clause_order or [])
            ]
            # People the live provider returned FOR THIS QUERY are answers in
            # their own right. The corpus filter can only express what the
            # corpus already knows, so a freshly fetched person often fails it
            # ("Rust" is nobody's stored topic yet) — appending them keeps the
            # results the user actually paid a round-trip for.
            # Where the NEXT page of the corpus starts. Paging walks the
            # corpus, so the cursor counts corpus rows only — the live rows
            # appended below are already on this page and sit at no offset.
            # It has to be recomputed here at all because the values set
            # before discovery ran describe a corpus that has since grown.
            corpus_page = len(persons)
            seen = {p.id for p in persons}
            live_ids = [
                s_["person_id"]
                for s_ in suggestions + replayed
                if s_.get("person_id")
            ]
            if live_ids:
                from .models import Person as _Person

                extra = db.execute(
                    select(_Person).where(
                        _Person.id.in_(live_ids), _Person.merged_into.is_(None)
                    )
                ).scalars().all()
                persons = persons + [p for p in extra if p.id not in seen]
            rows = build_results(persons)
            live_set = set(live_ids)
            for row in rows:
                # so a caller can tell a corpus match from a just-fetched one
                row["from_live_search"] = row["id"] in live_set
            response["results"] = rows
            response["count"] = len(persons)
            # People the live search returned are results, so the total has to
            # count them. Reporting only the corpus count printed "1 of 0
            # matching" — a row on screen that the total said did not exist.
            corpus_total = count_matches(db, parsed)
            response["total_matches"] = max(corpus_total, len(persons))
            more = parsed.offset + corpus_page < corpus_total
            response["has_more"] = more
            response["next_offset"] = parsed.offset + corpus_page if more else None
            response["unmatched_terms"] = parsed.unmatched_terms
            response["protected_terms"] = parsed.protected_terms
            response.update(query_understanding(parsed))
            response["applied_filters"] = {
                "subjects": subjects_asked(parsed),
                "skills": parsed.skills, "skill_patterns": parsed.skill_patterns,
                "organizations": parsed.organizations, "locations": parsed.locations,
                "countries": parsed.countries, "name_terms": parsed.name_terms, "roles": parsed.roles,
                "limit": parsed.limit, "offset": parsed.offset,
            }
        if mode == "queue":
            response["queued_leads"] = queue_suggestions(db, suggestions, q)
    return response


@app.get("/v1/shortlists")
def list_shortlists(owner: str = Query("anonymous"), db: Session = Depends(get_db)):
    """Every shortlist and how many people are on it."""
    from .models import Shortlist, ShortlistMember

    rows = db.execute(
        select(Shortlist, func.count(ShortlistMember.id))
        .outerjoin(ShortlistMember, ShortlistMember.shortlist_id == Shortlist.id)
        .where(Shortlist.owner == owner)
        .group_by(Shortlist.id)
        .order_by(Shortlist.name)
    ).all()
    return {
        "count": len(rows),
        "shortlists": [
            {"id": sl.id, "name": sl.name, "note": sl.note,
             "members": n, "created_at": sl.created_at}
            for sl, n in rows
        ],
    }


@app.post("/v1/shortlists")
def create_shortlist(payload: dict, db: Session = Depends(get_db)):
    """Create a shortlist, or return the existing one with that name."""
    from .models import Shortlist

    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    owner = str(payload.get("owner") or "anonymous")[:128]
    row = db.execute(
        select(Shortlist).where(Shortlist.name == name, Shortlist.owner == owner)
    ).scalar_one_or_none()
    created = row is None
    if row is None:
        row = Shortlist(name=name[:200], owner=owner, note=payload.get("note"))
        db.add(row)
        db.commit()
    return {"id": row.id, "name": row.name, "owner": row.owner, "created": created}


@app.get("/v1/shortlists/{shortlist_id}")
def get_shortlist(shortlist_id: int, db: Session = Depends(get_db)):
    """The people on a shortlist, with why each was added."""
    from .models import Person, Shortlist, ShortlistMember

    sl = db.get(Shortlist, shortlist_id)
    if sl is None:
        raise HTTPException(404, "no such shortlist")
    rows = db.execute(
        select(ShortlistMember, Person)
        .join(Person, Person.id == ShortlistMember.person_id)
        .where(ShortlistMember.shortlist_id == shortlist_id)
        .order_by(ShortlistMember.added_at.desc())
    ).all()
    return {
        "id": sl.id, "name": sl.name, "note": sl.note, "count": len(rows),
        "members": [
            {
                "person_id": str(person.id),
                "canonical_name": person.canonical_name,
                "location": person.location,
                "profile_urls": person.profile_urls,
                "found_by_query": m.found_by_query,
                "note": m.note,
                "added_at": m.added_at,
            }
            for m, person in rows
        ],
    }


@app.post("/v1/shortlists/{shortlist_id}/members")
def add_to_shortlist(shortlist_id: int, payload: dict, db: Session = Depends(get_db)):
    """Put a person on a shortlist. Idempotent."""
    from .models import Person, Shortlist, ShortlistMember

    if db.get(Shortlist, shortlist_id) is None:
        raise HTTPException(404, "no such shortlist")
    person_id = str(payload.get("person_id") or "").strip()
    if db.get(Person, person_id) is None:
        raise HTTPException(404, "no such person")
    row = db.execute(
        select(ShortlistMember).where(
            ShortlistMember.shortlist_id == shortlist_id,
            ShortlistMember.person_id == person_id,
        )
    ).scalar_one_or_none()
    added = row is None
    if row is None:
        row = ShortlistMember(
            shortlist_id=shortlist_id, person_id=person_id,
            found_by_query=(payload.get("query") or None),
            note=(payload.get("note") or None),
        )
        db.add(row)
        db.commit()
    return {"shortlist_id": shortlist_id, "person_id": person_id, "added": added}


@app.delete("/v1/shortlists/{shortlist_id}/members/{person_id}")
def remove_from_shortlist(shortlist_id: int, person_id: str, db: Session = Depends(get_db)):
    """Take a person off a shortlist. The person themselves is untouched."""
    from .models import ShortlistMember

    row = db.execute(
        select(ShortlistMember).where(
            ShortlistMember.shortlist_id == shortlist_id,
            ShortlistMember.person_id == person_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "not on this shortlist")
    db.delete(row)
    db.commit()
    return {"shortlist_id": shortlist_id, "person_id": person_id, "removed": True}


@app.delete("/v1/shortlists/{shortlist_id}")
def delete_shortlist(shortlist_id: int, db: Session = Depends(get_db)):
    """Delete a shortlist and its membership rows.

    A list could be created but never removed, so a name typed wrong was
    permanent. Only the list goes: a membership row records that somebody was
    shortlisted, not anything about the person, and every Person the list
    pointed at is left exactly as it was.
    """
    from .models import Shortlist, ShortlistMember

    row = db.get(Shortlist, shortlist_id)
    if row is None:
        raise HTTPException(404, "shortlist not found")
    members = db.execute(
        select(ShortlistMember).where(ShortlistMember.shortlist_id == shortlist_id)
    ).scalars().all()
    for member in members:
        db.delete(member)
    db.delete(row)
    db.commit()
    return {"shortlist_id": shortlist_id, "name": row.name,
            "removed_members": len(members), "deleted": True}


@app.post("/v1/feedback")
def post_feedback(payload: dict, db: Session = Depends(get_db)):
    """Record that a person was a good or bad match for a query.

    Stored, never applied. Seekr does not reorder anything by these votes —
    that would be ranking, which belongs to the downstream tool. This is the
    labelled data that tool can train on, exposed at GET /v1/feedback.
    """
    from .models import MatchFeedback, Person
    from .nlq import _norm_query

    person_id = str(payload.get("person_id") or "").strip()
    verdict = str(payload.get("verdict") or "").strip().lower()
    query = str(payload.get("query") or "").strip()
    if verdict not in ("good", "bad"):
        raise HTTPException(400, "verdict must be 'good' or 'bad'")
    if not person_id or not query:
        raise HTTPException(400, "person_id and query are required")
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(404, "no such person")

    norm = _norm_query(query)
    voter = str(payload.get("voter") or "anonymous")[:128]
    row = db.execute(
        select(MatchFeedback).where(
            MatchFeedback.person_id == person_id,
            MatchFeedback.query_norm == norm,
            MatchFeedback.voter == voter,
        )
    ).scalar_one_or_none()
    if row is None:
        row = MatchFeedback(person_id=person_id, query_norm=norm, voter=voter)
        db.add(row)
    # a voter changing their mind replaces their vote rather than stacking
    row.query_raw, row.verdict = query[:2000], verdict
    row.note = (payload.get("note") or None)
    db.commit()
    return {"person_id": person_id, "query": query, "verdict": verdict, "voter": voter}


@app.get("/v1/feedback")
def list_feedback(
    since_id: int = Query(0, ge=0, description="cursor: return rows after this id"),
    limit: int = Query(200, ge=1, le=1000),
    person_id: str = Query("", description="only this person's judgements"),
    db: Session = Depends(get_db),
):
    """The judgement log, for the ranking tool to train on."""
    from .models import MatchFeedback

    stmt = select(MatchFeedback).where(MatchFeedback.id > since_id)
    if person_id:
        stmt = stmt.where(MatchFeedback.person_id == person_id)
    rows = db.execute(stmt.order_by(MatchFeedback.id).limit(limit)).scalars().all()
    return {
        "count": len(rows),
        "next_since_id": rows[-1].id if rows else since_id,
        "has_more": len(rows) == limit,
        "feedback": [
            {
                "id": r.id, "person_id": r.person_id, "query": r.query_raw,
                "verdict": r.verdict, "note": r.note, "voter": r.voter,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


@app.get("/v1/persons/{person_id}/dossier")
def person_dossier(person_id: str, db: Session = Depends(get_db)):
    """A readable, evidence-linked report on one person, as HTML."""
    from starlette.responses import HTMLResponse

    from .dossier import collect, render_html

    person = db.get(Person, person_id)
    if person is None or person.merged_into:
        raise HTTPException(404, "no such person")
    return HTMLResponse(render_html(collect(db, person)))


def _find_chrome_binary() -> str | None:
    """Locate a Chromium-based browser for headless PDF rendering, across
    platforms.

    The original path list here was Mac/Linux only (/Applications/...,
    /usr/bin/...) — on Windows this endpoint answered 501 unconditionally,
    regardless of whether a browser was actually installed, because none of
    the hardcoded paths could ever match. Microsoft Edge is included
    specifically for Windows: it is Chromium-based, supports the same
    --headless --print-to-pdf flags Chrome does, and ships pre-installed on
    every modern Windows system — so this works out of the box there even
    without a separate Chrome install.
    """
    if os.environ.get("CHROME_BINARY"):
        return os.environ["CHROME_BINARY"]

    # Bare command names first: catches anything already on PATH, on any
    # platform, without guessing an install location at all.
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome", "msedge", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found

    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app_data = os.environ.get("LocalAppData", "")
    candidates = [
        # macOS
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        # Linux
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable",
        # Windows — both Program Files locations (installers vary), plus a
        # per-user AppData install that needs no admin rights to have made
        os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(program_files_x86, "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for c in candidates:
        if c and pathlib.Path(c).exists():
            return c
    return None


@app.get("/v1/persons/{person_id}/dossier.pdf")
def person_dossier_pdf(person_id: str, db: Session = Depends(get_db)):
    """The same dossier as a PDF.

    Rendered by headless Chrome (or Edge — see _find_chrome_binary), which
    is how the screener produces its reports too. Answers 501 when no
    Chromium-based browser can be found rather than failing obscurely — the
    HTML dossier above always works regardless, and a browser can print it.
    """
    import subprocess
    import tempfile

    from starlette.responses import Response

    from .dossier import collect, render_html

    person = db.get(Person, person_id)
    if person is None or person.merged_into:
        raise HTTPException(404, "no such person")

    chrome = _find_chrome_binary()
    if not chrome:
        raise HTTPException(501, "no Chrome available to render a PDF; use the "
                                 "HTML dossier at /dossier and print it")

    with tempfile.TemporaryDirectory() as tmp:
        src = pathlib.Path(tmp) / "dossier.html"
        out = pathlib.Path(tmp) / "dossier.pdf"
        src.write_text(render_html(collect(db, person)), encoding="utf-8")
        # Chrome writes the PDF and then takes tens of seconds to shut down,
        # so waiting for the process to exit turns a two-second render into a
        # minute-long request. Wait for the file to appear and settle instead,
        # then stop it.
        import time as _time

        proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={tmp}/profile", "--no-pdf-header-footer",
             "--no-first-run", "--no-default-browser-check", "--disable-extensions",
             "--disable-background-networking", "--disable-component-update",
             "--disable-sync", "--disable-default-apps", "--disable-dev-shm-usage",
             "--virtual-time-budget=3000",
             f"--print-to-pdf={out}", src.as_uri()],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            size, stable, deadline = -1, 0, _time.time() + 45
            while _time.time() < deadline:
                if out.exists():
                    now = out.stat().st_size
                    # two identical readings means the write has finished
                    stable = stable + 1 if now == size and now > 0 else 0
                    size = now
                    if stable >= 2:
                        break
                if proc.poll() is not None and out.exists():
                    break
                _time.sleep(0.25)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        if not out.exists():
            raise HTTPException(500, "Chrome produced no PDF")
        name = re.sub(r"[^A-Za-z0-9]+", "_", person.canonical_name or "person").strip("_")
        return Response(
            out.read_bytes(), media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{name}_seekr_dossier.pdf"'},
        )


@app.get("/v1/query/stream")
def query_stream(
    q: str = Query(..., description="the question, in plain language"),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    """The same search as /v1/query, reported as it happens.

    A live search asks several sources in turn and can take tens of seconds.
    Returning only the finished answer makes that look like a hang; this emits
    an event per source as it is reached, so a caller can show what is being
    asked and what came back, then the finished result set.

    Server-sent events. Each line is `data: {json}`.
    """
    import json as _json
    import queue as _queue
    import threading

    from starlette.responses import StreamingResponse

    from .nlq import (count_matches, discovery_suggestions, enabled_searchers,
                      execute, execute_progressive, parse, query_understanding,
                      relevance_scores, subjects_asked)

    events: "_queue.Queue[dict | None]" = _queue.Queue()

    def run():
        session = SessionLocal()
        try:
            parsed = parse(session, q)
            if limit:
                parsed.limit = limit
            events.put({"type": "parsed", "applied_filters": {
                "subjects": subjects_asked(parsed),
                "skills": parsed.skills, "organizations": parsed.organizations,
                "locations": parsed.locations, "countries": parsed.countries,
                "roles": parsed.roles, "name_terms": parsed.name_terms,
            }, "applied_clauses": [
                {"term": c["token"], "as": c["label"]} for c in (parsed.clause_order or [])
            ], "unmatched_terms": parsed.unmatched_terms,
                "protected_terms": parsed.protected_terms,
                **query_understanding(parsed),
                "corrections": parsed.corrections})

            def on_source(name, state, **facts):
                events.put({"type": "source", "source": name, "state": state, **facts})

            suggestions = discovery_suggestions(
                session, parsed, allow_paid=True, on_source=on_source
            )
            stored = sum(1 for s_ in suggestions if s_.get("stored"))
            if stored:
                from .nlq import invalidate_vocab
                invalidate_vocab()
                parsed = parse(session, q)
                if limit:
                    parsed.limit = limit
            persons, applied, not_found = execute_progressive(session, parsed)
            parsed = applied
            # People a live source just returned are answers in their own
            # right. The corpus filter can only express what the corpus
            # already knows, so someone fetched seconds ago usually fails it —
            # a search for experts in Hyderabad stored fifteen people and then
            # showed none, because none of them carry a Hyderabad location
            # yet. /v1/query already appends them; without this the cards said
            # "10 kept" over an empty table.
            corpus_page = len(persons)
            seen = {p.id for p in persons}
            live_ids = [s_["person_id"] for s_ in suggestions if s_.get("person_id")]
            if live_ids:
                extra = session.execute(
                    select(Person).where(
                        Person.id.in_(live_ids), Person.merged_into.is_(None)
                    )
                ).scalars().all()
                persons = persons + [p for p in extra if p.id not in seen]

            # the same ranking /v1/query applies, so the stream and the plain
            # endpoint cannot disagree about the order
            scores = relevance_scores(session, parsed, [p.id for p in persons])
            rows = []
            for person in persons:
                row = _person_summary(person)
                # _person_summary does not carry the score — /v1/query attaches
                # it in its own builder — so the stream attaches it here rather
                # than reporting a different order to the same question
                rel = scores.get(person.id)
                if rel:
                    row["score"] = rel.get("score")
                    row["score_components"] = rel.get("components")
                    row["matched_evidence"] = rel.get("matched_evidence")
                row["from_live_search"] = row["id"] in set(live_ids)
                rows.append(row)
            rows.sort(key=lambda r: (r.get("score") is None, -(r.get("score") or 0)))
            # A stream that says nothing about paging leaves the page holding
            # the PREVIOUS query's answer: the merge keeps old keys, so a
            # "Load 50 more" button could belong to a question already gone.
            corpus_total = count_matches(session, parsed)
            more = parsed.offset + corpus_page < corpus_total
            events.put({
                "type": "results",
                "count": len(rows),
                "stored_from_live": stored,
                "not_found": not_found,
                "total_matches": max(corpus_total, len(rows)),
                "has_more": more,
                "next_offset": parsed.offset + corpus_page if more else None,
                "results": rows,
            })
        except Exception as exc:                     # the stream must always end
            events.put({"type": "error", "detail": f"{type(exc).__name__}: {exc}"})
        finally:
            session.close()
            events.put(None)

    threading.Thread(target=run, daemon=True).start()

    def emit():
        # the cards exist before any of them resolve, so the UI can lay them
        # out at once rather than popping them in one at a time
        yield "data: " + _json.dumps({
            "type": "plan",
            "sources": [name for name, _fn, _full in enabled_searchers()],
        }) + "\n\n"
        while True:
            item = events.get()
            if item is None:
                break
            yield "data: " + _json.dumps(item, default=str) + "\n\n"

    return StreamingResponse(emit(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@app.post("/v1/webhooks")
def create_webhook(payload: dict, db: Session = Depends(get_db)):
    """Subscribe to change events. The signing secret is returned ONCE."""
    from .webhooks import create_subscription

    url = (payload or {}).get("url")
    if not url:
        raise HTTPException(422, "url is required")
    try:
        sub, secret = create_subscription(
            db, url, payload.get("event_types"), payload.get("description")
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {
        "id": sub.id,
        "url": sub.url,
        "event_types": sub.event_types,
        "signing_secret": secret,
        "note": "store this secret now; it is not shown again",
    }


@app.get("/v1/webhooks")
def list_webhooks(db: Session = Depends(get_db)):
    from .models import WebhookSubscription

    subs = db.execute(
        select(WebhookSubscription).where(WebhookSubscription.is_active.is_(True))
    ).scalars().all()
    return {
        "subscriptions": [
            {"id": s.id, "url": s.url, "event_types": s.event_types,
             "description": s.description, "created_at": s.created_at}
            for s in subs
        ]
    }


@app.get("/v1/webhooks/health")
def webhook_health(db: Session = Depends(get_db)):
    """Delivery backlog. A rising `pending` count means the delivery cron
    (`rip.cli deliver-webhooks`) is not running."""
    from .webhooks import health

    return health(db)


@app.delete("/v1/webhooks/{subscription_id}")
def delete_webhook(subscription_id: int, db: Session = Depends(get_db)):
    from .models import WebhookSubscription

    sub = db.get(WebhookSubscription, subscription_id)
    if sub is None:
        raise HTTPException(404, "subscription not found")
    sub.is_active = False
    db.commit()
    return {"id": sub.id, "status": "deactivated"}


@app.post("/v1/leads")
def queue_lead(payload: dict, db: Session = Depends(get_db)):
    """Queue one source record for a worker to ingest later.

    Used by the UI's live-search results. Ingestion still happens in the
    worker, under the normal rate limits — never inside this request.
    """
    from .models import DiscoveryLead

    source = (payload or {}).get("source")
    external_id = (payload or {}).get("external_id")
    if not source or not external_id:
        raise HTTPException(422, "source and external_id are required")
    from .connectors import CONNECTORS

    if source not in CONNECTORS:
        raise HTTPException(422, f"unknown source '{source}'")

    already = db.execute(
        select(SourceRecord.id).where(
            SourceRecord.source == source, SourceRecord.external_id == external_id
        )
    ).first()
    if already:
        return {"status": "already_ingested", "source": source, "external_id": external_id}
    existing = db.execute(
        select(DiscoveryLead).where(
            DiscoveryLead.source == source, DiscoveryLead.identifier == external_id
        )
    ).scalar_one_or_none()
    if existing:
        return {"status": "already_queued", "lead_id": existing.id}
    lead = DiscoveryLead(
        source=source,
        identifier=external_id,
        reason=(payload.get("reason") or "queued from live search")[:1000],
    )
    db.add(lead)
    db.commit()
    return {"status": "queued", "lead_id": lead.id}


@app.get("/v1/health/sources")
def source_health(db: Session = Depends(get_db)):
    rows = db.execute(
        select(
            IngestionRun.source,
            IngestionRun.status,
            func.count(IngestionRun.id),
            func.max(IngestionRun.finished_at),
        ).group_by(IngestionRun.source, IngestionRun.status)
    ).all()
    return {
        "sources": [
            {"source": s, "status": st, "runs": n, "last_finished_at": last}
            for s, st, n, last in rows
        ]
    }
