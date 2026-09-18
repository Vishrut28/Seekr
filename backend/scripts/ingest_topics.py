"""Ingest people for subjects the corpus cannot answer yet.

The evaluation's held-out set fails on subjects nobody here works on —
"computational pathology", "speech recognition", "malaria" — which is a
coverage problem, not a parsing one. This asks the free topical sources for
authors on each subject and ingests them, so the vocabulary gains the topics
and the graph gains the people.

    python scripts/ingest_topics.py --per-topic 10            # what it would do
    python scripts/ingest_topics.py --per-topic 10 --yes      # do it

Every source is free (OpenAlex, Europe PMC). Enrichment is off: this is a
breadth pass, and following every identity link would multiply the calls.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TOPICS = [
    # (subject, sources) — Europe PMC covers medicine and biology, OpenAlex the rest
    ("computational pathology", ("openalex", "europepmc")),
    ("digital pathology", ("europepmc",)),
    ("speech recognition", ("openalex",)),
    ("malaria", ("europepmc", "openalex")),
    ("renewable energy", ("openalex",)),
    ("graph neural networks", ("openalex",)),
    ("database systems", ("openalex",)),
    ("quantum computing", ("openalex",)),
    ("human computer interaction", ("openalex",)),
    ("recommender systems", ("openalex",)),
    ("reinforcement learning", ("openalex",)),
    ("diabetes", ("europepmc",)),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-topic", type=int, default=10)
    parser.add_argument("--yes", action="store_true", help="ingest; otherwise only list")
    parser.add_argument("--topic", action="append", help="just this subject (repeatable)")
    args = parser.parse_args()

    from rip.connectors import get_connector
    from rip.db import SessionLocal, init_db
    from rip.ingest import run_connector
    from rip.models import Person
    from sqlalchemy import func, select

    init_db()
    wanted = [t for t in TOPICS if not args.topic or t[0] in args.topic]
    started = time.perf_counter()
    with SessionLocal() as session:
        before = session.execute(
            select(func.count(Person.id)).where(Person.merged_into.is_(None))).scalar_one()
        total_new = 0
        for subject, sources in wanted:
            for source in sources:
                connector = get_connector(source)
                try:
                    found = connector.search_authors_by_topic(subject, limit=args.per_topic)
                except Exception as exc:
                    print(f"  {source:11s} {subject:26s} search failed: {exc}")
                    continue
                print(f"  {source:11s} {subject:26s} {len(found)} candidates")
                if not args.yes:
                    continue
                for candidate in found:
                    identifier = candidate.get("id")
                    if not identifier:
                        continue
                    try:
                        person = run_connector(session, connector, str(identifier))
                    except Exception as exc:
                        print(f"      ! {candidate.get('name')}: {type(exc).__name__}: {exc}")
                        continue
                    if person is not None:
                        total_new += 1
                        print(f"      + {person.canonical_name}")
        after = session.execute(
            select(func.count(Person.id)).where(Person.merged_into.is_(None))).scalar_one()
    print(f"\ningested {total_new}; people {before} -> {after} "
          f"in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
