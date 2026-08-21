"""Read API for the downstream ranking tool.

Filters decide who matches. Search results are ordered by evidence-backed
relevance (corroboration and query fit), not by hiring fitness. Stable person
UUIDs, full provenance, and a /changes feed for incremental sync.
"""

from datetime import datetime

import contextvars

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import String as SAString
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from pathlib import Path

from .db import READ_ONLY, SessionLocal, init_db

# Fewer results than this is a thin answer, and thin is worth topping up from
# live sources even though it is not empty.
THIN_ANSWER = 10
from .nlq import _word_match, location_anywhere, place_mentioned  # whole-word matching, shared with the parser
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
    description="Evidence-backed resource data layer. Search orders by corroboration, not hiring fitness.",
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
    response = await call_next(request)
    # The UI is edited on disk; a cached app.js is why an empty search box
    # can keep showing yesterday's results.
    if request.url.path == "/ui" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.on_event("startup")
def _startup() -> None:
    try:
        init_db()
        if not READ_ONLY:
            from .ingest import backfill_blank_locations

            db = SessionLocal()
            try:
                backfill_blank_locations(db)
            except Exception:
                import logging

                logging.getLogger("rip").exception("location backfill failed")
            finally:
                db.close()
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
    html = index.read_text()
    stamp = str(int((FRONTEND_DIR / "app.js").stat().st_mtime))
    html = html.replace('href="/static/styles.css"', f'href="/static/styles.css?v={stamp}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={stamp}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})


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
    sort: str = Query("relevance", description="relevance (evidence score) | recent | name"),
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Faceted people search.

    Every parameter except `sort` is a *filter*. `sort=relevance` orders by
    corroboration and query fit; `recent` and `name` stay factual. This is
    not a hiring-fitness score.
    """
    from sqlalchemy import and_, exists as sa_exists

    stmt = select(Person).where(Person.merged_into.is_(None))

    if q:
        stmt = stmt.where(
            _word_match(Person.canonical_name, q)
            | func.lower(func.cast(Person.aliases, SAString)).like(f'%"{q.lower()}%')
        )
    if skill:
        stmt = stmt.where(sa_exists().where(and_(
            Evidence.person_id == Person.id,
            Evidence.attribute_type.in_(["skill", "research_interest", "specialization"]),
            _word_match(Evidence.value, skill),
        )))
    for value, relation in ((organization, None), (education, "studied_at")):
        if not value:
            continue
        conds = [
            Affiliation.person_id == Person.id,
            Organization.id == Affiliation.organization_id,
            _word_match(Organization.name, value),
        ]
        if relation:
            conds.append(Affiliation.relation == relation)
        stmt = stmt.where(sa_exists().where(and_(*conds)))
    if current_organization:
        stmt = stmt.where(_word_match(Person.current_organization, current_organization))
    if role:
        stmt = stmt.where(
            _word_match(Person.current_role, role)
            | sa_exists().where(and_(
                Affiliation.person_id == Person.id,
                _word_match(Affiliation.role, role),
            ))
        )
    if country:
        # A stated country, or a location that names it. Without the second
        # half, country=IN missed every GitHub developer whose location reads
        # "Bangalore, India" — the source gave a place, not an ISO code.
        from .nlq import names_for_country

        clauses = [func.upper(Person.country) == country.upper()]
        for name in names_for_country(country):
            clauses.append(_word_match(Person.location, name))
        stmt = stmt.where(or_(*clauses))
    if location:
        stmt = stmt.where(location_anywhere(location))
    if source:
        stmt = stmt.where(sa_exists().where(and_(
            IdentityLink.person_id == Person.id,
            SourceRecord.id == IdentityLink.source_record_id,
            SourceRecord.source == source.lower(),
        )))
    if technology:
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
    page = stmt.with_only_columns(Person.id).distinct()
    if order is not None:
        page = page.order_by(order)
    else:
        from .rank import relevance_order_by

        # DISTINCT + ORDER BY extra expressions is invalid on Postgres; score
        # against the de-duplicated id set instead.
        inner = page.subquery()
        page = (
            select(inner.c.id)
            .select_from(inner.join(Person, Person.id == inner.c.id))
            .order_by(*relevance_order_by(
                skill=skill, organization=organization,
                current_organization=current_organization, role=role, q=q,
            ))
        )
    ids = db.execute(page.limit(limit).offset(offset)).scalars().all()
    rows = {p.id: p for p in db.execute(
        select(Person).where(Person.id.in_(ids))).scalars()} if ids else {}
    persons = [rows[i] for i in ids if i in rows]

    results = [_person_summary(p) for p in persons]
    if sort not in ("recent", "name"):
        from .rank import attach_match_ranking

        attach_match_ranking(
            db, results, skill=skill, organization=organization,
            current_organization=current_organization, role=role, q=q,
        )
    response = {
        "count": len(persons),
        "total_matches": total,
        "has_more": offset + len(persons) < total,
        "next_offset": offset + len(persons) if offset + len(persons) < total else None,
        "results": results,
    }
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
                f"{a['filter']}={a['value']}" for a in dead
            ) + " at all — check the spelling, or add * for a loose match."
        elif blockers:
            message = "No one matches every filter at once. " + "; ".join(
                f"dropping {b['filter']} would return {b['without_it']:,}" for b in blockers
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
    field: str = Query(..., description="country | source | organization | skill | role"),
    limit: int = Query(30, le=200),
    db: Session = Depends(get_db),
):
    """Available filter values and how many people carry each.

    Counts describe the corpus; they are not a ranking of people.
    """
    if field == "country":
        rows = db.execute(
            select(Person.country, func.count(Person.id))
            .where(Person.country.isnot(None), Person.merged_into.is_(None))
            .group_by(Person.country).order_by(func.count(Person.id).desc()).limit(limit)
        ).all()
    elif field == "source":
        rows = db.execute(
            select(SourceRecord.source, func.count(func.distinct(IdentityLink.person_id)))
            .join(IdentityLink, IdentityLink.source_record_id == SourceRecord.id)
            .group_by(SourceRecord.source).order_by(func.count(IdentityLink.id).desc())
        ).all()
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
    else:
        raise HTTPException(422, "field must be country, source, organization, skill or role")
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
    depth: int = Query(1, ge=1, le=1, description="hops from the person (1 supported)"),
    limit_coauthors: int = Query(20, le=100),
    db: Session = Depends(get_db),
):
    """Neighborhood of one person: organizations and co-authors.

    Co-authors are capped by shared-publication count purely to bound the
    payload; the count is a factual edge weight, not a ranking of people.
    """
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(404, "person not found")

    nodes = [{"id": person.id, "type": "person", "label": person.canonical_name}]
    edges = []

    org_rows = db.execute(
        select(Organization, Affiliation)
        .join(Affiliation, Affiliation.organization_id == Organization.id)
        .where(Affiliation.person_id == person_id)
    ).all()
    for org, aff in org_rows:
        node_id = f"org-{org.id}"
        if not any(n["id"] == node_id for n in nodes):
            nodes.append({"id": node_id, "type": "organization", "label": org.name})
        edges.append({
            "from": person.id, "to": node_id, "type": aff.relation,
            "role": aff.role, "is_current": aff.is_current,
        })

    my_pub_ids = [
        pid for (pid,) in db.execute(
            select(Authorship.publication_id).where(Authorship.person_id == person_id)
        ).all()
    ]
    if my_pub_ids:
        coauthor_rows = db.execute(
            select(
                Person.id, Person.canonical_name,
                func.count(Authorship.publication_id).label("shared"),
                func.min(Authorship.publication_id),
            )
            .join(Authorship, Authorship.person_id == Person.id)
            .where(
                Authorship.publication_id.in_(my_pub_ids),
                Authorship.person_id != person_id,
                Person.merged_into.is_(None),
            )
            .group_by(Person.id)
            .order_by(func.count(Authorship.publication_id).desc())
            .limit(limit_coauthors)
        ).all()
        for other_id, other_name, shared, via_pub in coauthor_rows:
            nodes.append({"id": other_id, "type": "person", "label": other_name})
            edges.append({
                "from": person.id, "to": other_id, "type": "coauthor",
                "shared_publications": shared, "via_publication_id": via_pub,
            })

    return {"person_id": person_id, "depth": depth, "nodes": nodes, "edges": edges}


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
    """Natural-language search. Filters from the query; results ordered by evidence.

    The response is explicit about which terms became filters and which
    could not be applied; never assume an unlisted constraint was enforced.
    With `discover=true`, a query the corpus cannot answer additionally
    returns `discovery_suggestions` — candidates from live searches across
    OpenAlex, Semantic Scholar and dblp that an operator may choose to
    ingest. `discover=queue` also adds them to the discovery-lead queue so a
    worker ingests them later. Neither mode ingests during the request.
    Suggestions are not results.
    """
    from .nlq import count_matches, diagnose_empty, execute_progressive, has_filters, parse

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

    def filters_payload(p):
        return {
            "skills": p.skills,
            "skill_patterns": p.skill_patterns,
            "organizations": p.organizations,
            "locations": p.locations,
            "countries": p.countries,
            "name_terms": p.name_terms, "roles": p.roles,
            "limit": p.limit,
            "offset": p.offset,
        }

    def clauses_payload(p):
        return [{"term": c["token"], "as": c["label"]} for c in (p.clause_order or [])]

    def build_results(rows):
        """Summaries plus a small evidence-count attribute sample per person."""
        ids = [r.id for r in rows]
        attr_map: dict = {pid: {} for pid in ids}
        org_map: dict = {pid: [] for pid in ids}
        extra_urls: dict = {pid: [] for pid in ids}
        source_links: dict = {pid: [] for pid in ids}
        place_hint: dict = {}
        if ids:
            for pid, at, val, src in db.execute(
                select(Evidence.person_id, Evidence.attribute_type, Evidence.value,
                       Evidence.source)
                .where(Evidence.person_id.in_(ids),
                       Evidence.attribute_type.in_(["skill", "research_interest"]))
            ).all():
                entry = attr_map[pid].setdefault(
                    (at, val),
                    {"attribute_type": at, "value": val, "evidence_count": 0, "sources": set()},
                )
                entry["evidence_count"] += 1
                if src:
                    entry["sources"].add(src)
            for pid, org_name in db.execute(
                select(Affiliation.person_id, Organization.name)
                .join(Organization, Organization.id == Affiliation.organization_id)
                .where(Affiliation.person_id.in_(ids))
            ).all():
                if org_name not in org_map[pid]:
                    org_map[pid].append(org_name)
            for pid, url, src in db.execute(
                select(IdentityLink.person_id, SourceRecord.url, SourceRecord.source)
                .join(SourceRecord, SourceRecord.id == IdentityLink.source_record_id)
                .where(IdentityLink.person_id.in_(ids), SourceRecord.url.isnot(None))
            ).all():
                if url and url not in extra_urls[pid]:
                    extra_urls[pid].append(url)
                if src and url and not any(x.get("url") == url for x in source_links[pid]):
                    source_links[pid].append({"source": src, "url": url})
            need_place = [p.id for p in rows if not (p.location or place_mentioned(p.summary))]
            if need_place:
                for pid, val in db.execute(
                    select(Evidence.person_id, Evidence.value).where(
                        Evidence.person_id.in_(need_place),
                        Evidence.attribute_type.in_(["bio", "location", "education", "role"]),
                    )
                ).all():
                    if pid in place_hint:
                        continue
                    hit = place_mentioned(val)
                    if hit:
                        place_hint[pid] = hit

        wanted_orgs = {o.lower() for o in parsed.organizations}
        out = []
        for person in rows:
            summary = _person_summary(person)
            seen = list(summary.get("profile_urls") or [])
            for url in extra_urls.get(person.id, []):
                if url not in seen:
                    seen.append(url)
            summary["profile_urls"] = seen
            summary["source_links"] = source_links.get(person.id, [])
            if not summary.get("location"):
                summary["location"] = place_hint.get(person.id)
            attrs = [
                {**a, "sources": sorted(a["sources"])}
                for a in attr_map.get(person.id, {}).values()
            ]
            summary["attributes"] = sorted(attrs, key=lambda a: -a["evidence_count"])[:6]
            summary["organizations"] = org_map.get(person.id, [])
            # the affiliation that satisfied the org filter — often NOT the
            # current one, so showing only current_organization looks wrong
            summary["matched_organization"] = next(
                (o for o in summary["organizations"] if o.lower() in wanted_orgs), None
            )
            out.append(summary)
        return out

    results = build_results(persons)
    from .rank import attach_match_ranking

    attach_match_ranking(db, results, parsed)

    response = {
        "query": q,
        "applied_filters": filters_payload(parsed),
        "applied_clauses": clauses_payload(parsed),
        "not_found": not_found,
        "unmatched_terms": asked.unmatched_terms,
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
        "discover_available": bool(
            not persons or asked.unmatched_terms or not_found or len(persons) < THIN_ANSWER
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
    # Auto live-search when the corpus returned nobody, or when the question
    # is just a name and we only have a handful of locals (one portfolio page
    # named "Rahul" is not an answer). Broader queries stay local-only so a
    # skill search does not hit GitHub on every keystroke. Press Live to go
    # upstream on purpose.
    name_only = bool(
        asked.name_terms
        and not asked.skills
        and not asked.skill_patterns
        and not asked.organizations
        and not asked.roles
    )
    if explicit or (mode == "auto" and (
        not persons or (name_only and len(persons) < THIN_ANSWER)
    )):
        from .nlq import discovery_suggestions, queue_suggestions

        try:
            # Live results are persisted when the provider returned a full person
            # payload: we already paid for that data, so keeping it means the same
            # query is answered from the graph next time instead of being re-bought.
            suggestions = discovery_suggestions(db, asked, allow_paid=allow_paid)
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
                asked = parse(db, q)
                if limit:
                    asked.limit = limit
                asked.offset = offset
                persons, parsed, not_found = execute_progressive(db, asked)
                live_ids = [
                    s_["person_id"]
                    for s_ in suggestions
                    if s_.get("person_id") and s_.get("stored")
                ]
                live_ids.extend(s_["person_id"] for s_ in replayed if s_.get("person_id"))
                rows = build_results(persons)
                live_set = set(live_ids)
                for row in rows:
                    # so a caller can tell a corpus match from a just-fetched one
                    row["from_live_search"] = row["id"] in live_set
                attach_match_ranking(db, rows, parsed)
                have = {r["id"] for r in rows}
                missing = [pid for pid in dict.fromkeys(live_ids) if pid and pid not in have]
                if missing:
                    extra_people = db.execute(
                        select(Person).where(
                            Person.id.in_(missing),
                            Person.merged_into.is_(None),
                        )
                    ).scalars().all()
                    extra_rows = build_results(extra_people)
                    for row in extra_rows:
                        row["from_live_search"] = True
                    rows.extend(extra_rows)
                rows.sort(key=lambda r: (-(r.get("match_score") or 0), r.get("canonical_name") or ""))
                response["results"] = rows
                response["count"] = len(rows)
                # People the live search returned are results, so the total has to
                # count them. Reporting only the corpus count printed "1 of 0
                # matching" — a row on screen that the total said did not exist.
                response["total_matches"] = max(count_matches(db, parsed), len(rows))
                response["unmatched_terms"] = asked.unmatched_terms
                response["applied_filters"] = filters_payload(parsed)
                response["applied_clauses"] = clauses_payload(parsed)
                response["not_found"] = not_found
            if mode == "queue":
                response["queued_leads"] = queue_suggestions(db, suggestions, q)
        except Exception:
            import logging

            logging.getLogger("rip").exception("live discovery failed")
            response["discovery_suggestions"] = response.get("discovery_suggestions") or []
            response["discovery_error"] = (
                "Live search failed; showing matches already in Seekr."
            )
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
    names = {}
    if rows:
        from .models import Person

        ids = [r.person_id for r in rows]
        names = {
            p.id: p.canonical_name
            for p in db.execute(select(Person).where(Person.id.in_(ids))).scalars()
        }
    return {
        "count": len(rows),
        "next_since_id": rows[-1].id if rows else since_id,
        "has_more": len(rows) == limit,
        "feedback": [
            {
                "id": r.id, "person_id": r.person_id, "query": r.query_raw,
                "verdict": r.verdict, "note": r.note, "voter": r.voter,
                "created_at": r.created_at,
                "canonical_name": names.get(r.person_id),
            }
            for r in rows
        ],
    }


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
