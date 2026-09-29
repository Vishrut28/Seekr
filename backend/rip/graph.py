"""A person's neighbourhood in the graph: organizations, and co-authors out to
a few hops.

Expanded one hop at a time — one query per hop over the whole frontier —
rather than with a recursive CTE. What keeps a co-author graph readable is a
cap on each person's edges (their strongest collaborators), and "top k per
node" is not something a recursive CTE can express portably across SQLite and
Postgres. At depth 3 that is three queries.

Every cap here bounds the payload and says so; none of them ranks people. The
shared-publication count on an edge is a fact, not a score.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from .models import Affiliation, Authorship, Organization, Person

MAX_DEPTH = 3
# Beyond the first hop, a paper with more linked authors than this is not
# followed. A consortium paper makes everyone on it everyone's co-author, and
# expanding a frontier of twenty people across one would return thousands of
# edges that say nothing about who works with whom.
MAX_TEAM_FOR_EXPANSION = 50


def neighborhood(
    session: Session, person: Person, depth: int = 1,
    limit_coauthors: int = 20, max_nodes: int = 200,
) -> dict:
    nodes = [{"id": person.id, "type": "person", "label": person.canonical_name, "hop": 0}]
    edges: list[dict] = []
    seen_nodes = {person.id}

    for org, aff in session.execute(
        select(Organization, Affiliation)
        .join(Affiliation, Affiliation.organization_id == Organization.id)
        .where(Affiliation.person_id == person.id)
    ).all():
        node_id = f"org-{org.id}"
        if node_id not in seen_nodes:
            seen_nodes.add(node_id)
            nodes.append({"id": node_id, "type": "organization", "label": org.name})
        edges.append({
            "from": person.id, "to": node_id, "type": aff.relation,
            "role": aff.role, "is_current": aff.is_current,
        })

    linked = set()                      # undirected co-author pairs already drawn
    frontier = [person.id]
    truncated = False
    for hop in range(1, min(depth, MAX_DEPTH) + 1):
        pairs = _coauthor_pairs(session, frontier, expanding=hop > 1)
        next_frontier: list[str] = []
        for src in frontier:
            strongest = sorted(pairs.get(src, []), key=lambda p: (-p[2], p[1] or "", p[0]))
            for dst, label, shared, via in strongest[:limit_coauthors]:
                pair = frozenset((src, dst))
                if pair in linked:
                    continue
                if dst not in seen_nodes:
                    if len(seen_nodes) >= max_nodes:
                        truncated = True
                        continue
                    seen_nodes.add(dst)
                    nodes.append({"id": dst, "type": "person", "label": label, "hop": hop})
                    next_frontier.append(dst)
                linked.add(pair)
                edges.append({
                    "from": src, "to": dst, "type": "coauthor",
                    "shared_publications": shared, "via_publication_id": via,
                })
        frontier = next_frontier
        if not frontier:
            break

    return {"person_id": person.id, "depth": depth, "nodes": nodes, "edges": edges,
            "truncated": truncated}


def _coauthor_pairs(session: Session, frontier: list[str], expanding: bool) -> dict:
    """{person in FRONTIER: [(co-author id, name, shared papers, one such paper)]}

    Two queries, not one join. Joined to `person` for its merged_into filter,
    SQLite (which keeps no statistics unless ANALYZE has run) believed "not
    merged" was selective and walked every live person probing authorships for
    each — 156 ms for one person on a 10k corpus. Pairs first, driven by the
    frontier's own authorships, then the few people they name by primary key.
    """
    if not frontier:
        return {}
    mine, theirs = aliased(Authorship), aliased(Authorship)
    stmt = (
        select(mine.person_id, theirs.person_id,
               func.count(mine.publication_id), func.min(mine.publication_id))
        .join(theirs, theirs.publication_id == mine.publication_id)
        .where(mine.person_id.in_(frontier), theirs.person_id != mine.person_id)
        .group_by(mine.person_id, theirs.person_id)
    )
    if expanding:
        # counted only over the frontier's own papers, so the check costs what
        # the hop costs rather than a pass over every authorship in the graph
        frontier_papers = select(Authorship.publication_id).where(
            Authorship.person_id.in_(frontier))
        crowded = (
            select(Authorship.publication_id)
            .where(Authorship.publication_id.in_(frontier_papers))
            .group_by(Authorship.publication_id)
            .having(func.count(Authorship.person_id) > MAX_TEAM_FOR_EXPANSION)
        )
        stmt = stmt.where(mine.publication_id.not_in(crowded))
    rows = session.execute(stmt).all()
    live = dict(session.execute(
        select(Person.id, Person.canonical_name)
        .where(Person.id.in_({dst for _, dst, _, _ in rows}), Person.merged_into.is_(None))
    ).tuples().all()) if rows else {}
    out: dict[str, list] = defaultdict(list)
    for src, dst, shared, via in rows:
        if dst in live:
            out[src].append((dst, live[dst], shared, via))
    return out
