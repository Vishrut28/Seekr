"""Ask OpenAlex where the corpus's unplaced papers sit, by DOI.

The conflation detector compares the subjects of a person's bodies of work,
using OpenAlex's taxonomy (subfield < field < domain). ORCID, Europe PMC and
Semantic Scholar bring papers without it, so on 2026-09-30 a quarter of the
people with six or more papers -- 126 of 461 -- had not one paper it could
place, and could never be reported. Most of those papers carry a DOI, and
OpenAlex knows most DOIs.

    python scripts/place_topics_by_doi.py              # how many it would ask about
    python scripts/place_topics_by_doi.py --yes        # ask, and store the answers
    python scripts/place_topics_by_doi.py --yes --limit 500

Answers go to work_topics, never to Publication.topics: that column is what a
paper's own source said, and the ranker reads it. A DOI OpenAlex does not know
is stored as a miss, so a rerun asks only about papers it has not asked about.
Fifty DOIs per request. Back up the database before running with --yes.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def unplaced_dois(session) -> list[str]:
    """DOIs of papers with no topic placed in OpenAlex's taxonomy, not yet asked about."""
    from rip.models import Publication, WorkTopics
    from sqlalchemy import select

    from rip import conflation

    hierarchy = conflation.topic_hierarchy(session)
    asked = set(session.execute(select(WorkTopics.doi)).scalars())
    wanted: dict[str, None] = {}
    for doi, topics in session.execute(
            select(Publication.doi, Publication.topics).where(Publication.doi.isnot(None))):
        key = conflation._doi_key(doi)
        if not key or key in asked or key in wanted:
            continue
        if any(t in hierarchy for t in conflation._as_list(topics)):
            continue
        wanted[key] = None
    return list(wanted)


def placed(work: dict) -> list[dict]:
    """A work's topics in the shape work_topics stores."""
    out = []
    for topic in work.get("topics") or []:
        if not topic.get("display_name"):
            continue
        out.append({"name": topic["display_name"],
                    **{level: (topic.get(level) or {}).get("display_name")
                       for level in ("subfield", "field", "domain")}})
    return out


def store(session, asked: list[str], works: list[dict]) -> tuple[int, int]:
    """Record every DOI asked about: what OpenAlex placed it under, or a miss."""
    from rip.models import WorkTopics

    from rip import conflation

    by_doi = {conflation._doi_key(w.get("doi")): w for w in works if w.get("doi")}
    hits = 0
    for doi in asked:
        work = by_doi.get(doi)
        row = session.get(WorkTopics, doi) or WorkTopics(doi=doi)
        row.openalex_id = work["id"].rsplit("/", 1)[-1] if work else None
        row.topics = placed(work) if work else []
        session.add(row)
        hits += bool(work)
    return hits, len(asked) - hits


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="ask OpenAlex and store the answers")
    parser.add_argument("--limit", type=int, default=0, help="ask about at most this many DOIs")
    args = parser.parse_args()

    from rip.connectors.openalex import OpenAlexConnector
    from rip.db import SessionLocal, init_db

    init_db()                       # creates work_topics on a database that predates it
    with SessionLocal() as session:
        dois = unplaced_dois(session)
        if args.limit:
            dois = dois[:args.limit]
        requests = -(-len(dois) // 50)
        print(f"{len(dois)} unplaced DOIs to ask about, in {requests} requests")
        if not args.yes or not dois:
            if dois:
                print("re-run with --yes to ask OpenAlex")
            return

        connector = OpenAlexConnector()
        hits = misses = 0
        for start in range(0, len(dois), 50):
            chunk = dois[start:start + 50]
            h, m = store(session, chunk, connector.works_by_doi(chunk))
            session.commit()        # a failed request later loses nothing already answered
            hits, misses = hits + h, misses + m
            print(f"  {start + len(chunk)}/{len(dois)}: {hits} placed, {misses} unknown to OpenAlex")
        print(f"done: {hits} papers placed, {misses} DOIs OpenAlex does not know")


if __name__ == "__main__":
    main()
