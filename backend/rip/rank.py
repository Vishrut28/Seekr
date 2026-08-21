"""Evidence-backed relevance for search result order.

Filters still decide *who* matches. This module only decides *which matching
person is shown first*: corroboration, evidence confidence, and how tightly
the profile fits the query terms. It is not a hiring score — there is no
"fitness for a role" model here, only how well the stored evidence supports
the filters the user already applied.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.orm import Session

from .models import Affiliation, Evidence, IdentityLink, Organization, Person

SKILL_ATTRS = ("skill", "research_interest", "specialization")


def _source_count():
    return (
        select(func.count(IdentityLink.id))
        .where(IdentityLink.person_id == Person.id)
        .correlate(Person)
        .scalar_subquery()
    )


def _avg_confidence():
    return (
        select(func.coalesce(func.avg(Evidence.confidence), 0.0))
        .where(Evidence.person_id == Person.id)
        .correlate(Person)
        .scalar_subquery()
    )


def _corroborated_count():
    return (
        select(func.count(Evidence.id))
        .where(
            Evidence.person_id == Person.id,
            Evidence.verification_state == "corroborated",
        )
        .correlate(Person)
        .scalar_subquery()
    )


def _skill_hit_count(parsed=None, skill: str | None = None):
    clauses = []
    if skill:
        clauses.append(func.lower(Evidence.value).like(f"%{skill.lower()}%"))
    if parsed is not None:
        for group in getattr(parsed, "skill_groups", []) or []:
            if group.get("values"):
                clauses.append(
                    func.lower(Evidence.value).in_([v.lower() for v in group["values"]])
                )
            if group.get("pattern"):
                clauses.append(
                    func.lower(Evidence.value).like(f"%{group['pattern'].lower()}%")
                )
        for value in getattr(parsed, "skills", []) or []:
            clauses.append(func.lower(Evidence.value) == value.lower())
    if not clauses:
        return literal(0)
    return (
        select(func.count(Evidence.id))
        .where(
            Evidence.person_id == Person.id,
            Evidence.attribute_type.in_(SKILL_ATTRS),
            or_(*clauses),
        )
        .correlate(Person)
        .scalar_subquery()
    )


def _current_org_boost(parsed=None, organization: str | None = None,
                       current_organization: str | None = None):
    names = [n for n in (organization, current_organization) if n]
    if parsed is not None:
        names.extend(getattr(parsed, "organizations", []) or [])
    names = [n.strip() for n in names if n and n.strip()]
    if not names:
        return literal(0.0)
    hit = or_(*[
        func.lower(Person.current_organization).like(f"%{n.lower()}%")
        for n in names
    ])
    return case((hit, 5.0), else_=0.0)


def _name_boost(parsed=None, q: str | None = None):
    terms = [q] if q else []
    if parsed is not None:
        terms.extend(getattr(parsed, "name_terms", []) or [])
    terms = [t.strip() for t in terms if t and t.strip()]
    if not terms:
        return literal(0.0)
    exact = or_(*[func.lower(Person.canonical_name) == t.lower() for t in terms])
    prefix = or_(*[
        func.lower(Person.canonical_name).like(t.lower() + "%") for t in terms
    ])
    return case((exact, 6.0), (prefix, 3.0), else_=0.0)


def _role_boost(parsed=None, role: str | None = None):
    titles = [role] if role else []
    if parsed is not None:
        titles.extend(getattr(parsed, "roles", []) or [])
    titles = [t.strip() for t in titles if t and t.strip()]
    if not titles:
        return literal(0.0)
    hit = or_(*[
        func.lower(Person.current_role).like(f"%{t.lower()}%") for t in titles
    ])
    return case((hit, 2.5), else_=0.0)


def relevance_score_expr(parsed=None, *, skill=None, organization=None,
                         current_organization=None, role=None, q=None):
    """Numeric score used only to ORDER matching rows. Higher is better."""
    sources = func.coalesce(_source_count(), 0)
    confidence = func.coalesce(_avg_confidence(), 0.0)
    corroborated = func.coalesce(_corroborated_count(), 0)
    # Cap so a celebrity with 40 sources cannot drown a well-matched specialist.
    source_term = case((sources > 8, 8), else_=sources) * 1.5
    corr_term = case((corroborated > 10, 10), else_=corroborated) * 0.4
    complete = (
        case((Person.current_role.isnot(None), 0.6), else_=0.0)
        + case((Person.current_organization.isnot(None), 0.6), else_=0.0)
        + case((Person.location.isnot(None), 0.4), else_=0.0)
    )
    skill_term = func.coalesce(_skill_hit_count(parsed, skill), 0) * 2.0
    return (
        source_term
        + confidence * 4.0
        + corr_term
        + complete
        + skill_term
        + _current_org_boost(parsed, organization, current_organization)
        + _name_boost(parsed, q)
        + _role_boost(parsed, role)
    )


def relevance_order_by(parsed=None, **filters):
    """Stable ORDER BY: score, then name, then id."""
    return (
        relevance_score_expr(parsed, **filters).desc(),
        Person.canonical_name.asc(),
        Person.id.asc(),
    )


def attach_match_ranking(
    session: Session,
    rows: list[dict],
    parsed=None,
    *,
    skill: str | None = None,
    organization: str | None = None,
    current_organization: str | None = None,
    role: str | None = None,
    q: str | None = None,
) -> None:
    """Add match_score (0–1) and short match_reasons onto each result dict."""
    ids = [r["id"] for r in rows if r.get("id")]
    if not ids:
        return

    source_n = dict(
        session.execute(
            select(IdentityLink.person_id, func.count(IdentityLink.id))
            .where(IdentityLink.person_id.in_(ids))
            .group_by(IdentityLink.person_id)
        ).all()
    )
    conf_rows = session.execute(
        select(
            Evidence.person_id,
            func.avg(Evidence.confidence),
            func.sum(case((Evidence.verification_state == "corroborated", 1), else_=0)),
        )
        .where(Evidence.person_id.in_(ids))
        .group_by(Evidence.person_id)
    ).all()
    avg_conf = {pid: float(avg or 0) for pid, avg, _ in conf_rows}
    corr_n = {pid: int(n or 0) for pid, _, n in conf_rows}

    wanted_skills = []
    if skill:
        wanted_skills.append(skill.lower())
    if parsed is not None:
        wanted_skills.extend(s.lower() for s in (parsed.skills or []))
        wanted_skills.extend(
            (g.get("term") or g.get("pattern") or "").lower()
            for g in (parsed.skill_groups or [])
        )
    wanted_skills = [s for s in wanted_skills if s]

    skill_hits: dict[str, list[tuple[str, int]]] = defaultdict(list)
    if wanted_skills:
        for pid, val, n, srcs in session.execute(
            select(
                Evidence.person_id,
                Evidence.value,
                func.count(Evidence.id),
                func.count(func.distinct(Evidence.source)),
            )
            .where(
                Evidence.person_id.in_(ids),
                Evidence.attribute_type.in_(SKILL_ATTRS),
            )
            .group_by(Evidence.person_id, Evidence.value)
        ).all():
            low = (val or "").lower()
            if any(w in low or low in w for w in wanted_skills if w):
                skill_hits[pid].append((val, int(srcs or n or 1)))

    wanted_orgs = []
    if organization:
        wanted_orgs.append(organization.lower())
    if current_organization:
        wanted_orgs.append(current_organization.lower())
    if parsed is not None:
        wanted_orgs.extend(o.lower() for o in (parsed.organizations or []))
    wanted_orgs = [o for o in wanted_orgs if o]

    current_affil: dict[str, list[str]] = defaultdict(list)
    if wanted_orgs:
        for pid, name, is_current in session.execute(
            select(Affiliation.person_id, Organization.name, Affiliation.is_current)
            .join(Organization, Organization.id == Affiliation.organization_id)
            .where(Affiliation.person_id.in_(ids))
        ).all():
            if name and any(o in name.lower() or name.lower() in o for o in wanted_orgs):
                label = name if not is_current else f"{name} (current)"
                if label not in current_affil[pid]:
                    current_affil[pid].append(label)

    name_terms = [q.lower()] if q else []
    if parsed is not None:
        name_terms.extend(t.lower() for t in (parsed.name_terms or []))
    name_terms = [t for t in name_terms if t]

    role_terms = [role.lower()] if role else []
    if parsed is not None:
        role_terms.extend(t.lower() for t in (parsed.roles or []))
    role_terms = [t for t in role_terms if t]

    for row in rows:
        pid = row["id"]
        reasons: list[str] = []
        n_src = int(source_n.get(pid, 0))
        if n_src >= 2:
            reasons.append(f"corroborated across {n_src} sources")
        elif n_src == 1:
            reasons.append("single-source profile")

        hits = skill_hits.get(pid) or []
        hits.sort(key=lambda h: -h[1])
        for value, n in hits[:2]:
            if n > 1:
                reasons.append(f"skill {value} ({n} sources)")
            else:
                reasons.append(f"skill {value}")

        for org in current_affil.get(pid, [])[:1]:
            reasons.append(f"affiliated with {org}")

        name = (row.get("canonical_name") or "")
        if name_terms and any(t in name.lower() for t in name_terms):
            reasons.append("name matches the query")

        title = (row.get("current_role") or "")
        if role_terms and any(t in title.lower() for t in role_terms):
            reasons.append(f"role {title}")

        conf = avg_conf.get(pid, 0.0)
        if corr_n.get(pid, 0) >= 2:
            reasons.append(f"{corr_n[pid]} corroborated claims")
        elif conf >= 0.7:
            reasons.append(f"evidence confidence {conf:.0%}")

        if row.get("from_live_search"):
            reasons.append("returned by live search for this query")

        # Same shape as the SQL order, compressed to 0–1 for display.
        raw = (
            min(n_src, 8) * 1.5
            + conf * 4.0
            + min(corr_n.get(pid, 0), 10) * 0.4
            + (0.6 if row.get("current_role") else 0.0)
            + (0.6 if row.get("current_organization") else 0.0)
            + (0.4 if row.get("location") else 0.0)
            + min(len(hits), 6) * 2.0
            + (5.0 if current_affil.get(pid) and row.get("current_organization")
               and any(o in (row.get("current_organization") or "").lower()
                       for o in wanted_orgs) else
               (2.0 if current_affil.get(pid) else 0.0))
            + (6.0 if name_terms and any(name.lower() == t for t in name_terms) else
               (3.0 if name_terms and any(name.lower().startswith(t) for t in name_terms)
                else 0.0))
            + (2.5 if role_terms and any(t in title.lower() for t in role_terms) else 0.0)
            + (2.0 if row.get("from_live_search") else 0.0)
        )
        row["match_score"] = round(min(raw / 22.0, 1.0), 3)
        row["match_reasons"] = reasons[:4]
