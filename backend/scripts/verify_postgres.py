"""Run the query paths that are engine-specific against a real Postgres.

SQLite forgives things Postgres does not, and the search index writes SQL by
hand: correlated EXISTS per constraint, negated EXISTS for exclusions, a
totals lookup for count filters, and additive column/index migrations. It
also checks that a compressed source payload round trips, because SQLite
stores JSON as text and hands back what was put in while Postgres parses it
into jsonb. This builds a small corpus in a Postgres database, asks the same
questions, and checks the answers match what the same data gives on SQLite.

    createdb seekr_verify                      # or: psql -c "CREATE DATABASE seekr_verify"
    export RIP_TEST_POSTGRES_URL=postgresql+psycopg://user:pass@127.0.0.1:5432/seekr_verify
    python scripts/verify_postgres.py

It writes only to that database, and drops every table it made when it is
done (--keep to inspect them instead).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CASES = [
    ("machine learning researchers", {"Ada Both", "Bob Google", "Cy Micro", "Dee Vision"}),
    ("machine learning researchers not at Google", {"Cy Micro", "Dee Vision"}),
    ("machine learning researchers at both Google and Microsoft", {"Ada Both"}),
    ("machine learning researchers at Google", {"Ada Both", "Bob Google"}),
    ("machine learning researchers in India", {"Dee Vision"}),
    ("machine learning researchers not in India", {"Ada Both", "Bob Google", "Cy Micro"}),
    ("machine learning researchers with at least 3 papers", {"Ada Both"}),
    ("machine learning researchers with over 100 citations", {"Ada Both", "Bob Google"}),
    ("people with at least 500 citations", {"Ada Both"}),
    ("computer vision but not deep learning", {"Cy Micro"}),
    ("Dee Vision", {"Dee Vision"}),
]

PEOPLE = [
    # name, topics, orgs, location, papers (citations each)
    ("Ada Both", ["Machine Learning and Algorithms"], ["Google", "Microsoft"], "Berlin, Germany",
     [300, 200, 100]),
    ("Bob Google", ["Machine Learning and Algorithms"], ["Google"], "Zurich, Switzerland", [150]),
    ("Cy Micro", ["Machine Learning and Algorithms", "Computer Vision and Pattern Recognition"],
     ["Microsoft"], "Seattle, United States", [10]),
    ("Dee Vision", ["Machine Learning and Algorithms", "Deep Learning"], [], "Pune, India", [5]),
]


def seed(session):
    from rip.ingest import ingest_profile
    from rip.normalize import EvidenceItem, NormalizedProfile, OrgAffiliation, PublicationData

    for index, (name, topics, orgs, location, papers) in enumerate(PEOPLE):
        ingest_profile(session, NormalizedProfile(
            source="openalex", source_type="scholarly", external_id=f"A{index}",
            url=f"https://openalex.org/A{index}", raw={}, name=name, location=location,
            organizations=[OrgAffiliation(name=o) for o in orgs],
            evidence=[EvidenceItem(attribute_type="research_interest", value=t) for t in topics],
            publications=[PublicationData(title=f"{name} paper {i}", external_id=f"A{index}-{i}",
                                          citations=c) for i, c in enumerate(papers)]))
    session.commit()


def answers(session, indexed: bool = True):
    from rip.nlq import count_matches, execute_progressive, parse

    from rip import nlq

    # the SQL path a database serves from before its index is built: correlated
    # EXISTS per organization, a GROUP BY for the candidate cut, NOT IN for
    # exclusions — all of it engine-specific
    real_ready = nlq.si.is_ready
    if not indexed:
        nlq.si.is_ready = lambda session: False
    out = {}
    for query, _expected in CASES:
        parsed = parse(session, query)
        rows, _applied, _dropped = execute_progressive(session, parsed)
        names = {p.canonical_name for p in rows if not getattr(p, "partial_match", None)}
        out[query] = (names, count_matches(session, parsed))
    nlq.si.is_ready = real_ready
    return out


def check_facets(session) -> int:
    """The country menu counts from the index, and must agree with the filter.

    Its own SQL: a join from the postings table to person, grouped by term and
    ordered by a count — an aggregate in ORDER BY that is not in the SELECT
    list is exactly the kind of thing SQLite waves through and Postgres does
    not.
    """
    from rip import api

    failures = 0
    values = api._facets_uncached("country", 40, session)["values"]
    counted = {v["value"]: v["people"] for v in values}
    for code, people in sorted(counted.items()):
        total = api.list_persons(
            **{**api._ALL_FILTERS_NONE, "country": code, "limit": 1, "db": session}
        )["total_matches"]
        ok = total == people
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} facet {code}: menu says {people}, "
              f"filter returns {total}")
    if not counted:
        print("  FAIL facet country: no countries counted at all")
        failures += 1
    return failures


def check_migration(url: str) -> int:
    """An existing database gains the v9 totals and their indexes.

    create_all() writes the current shape, so a fresh database never exercises
    the additive path that every deployed one takes.
    """
    from sqlalchemy import create_engine, inspect, text

    from rip import db as db_module

    engine = create_engine(url)
    db_module.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS search_doc CASCADE"))
        conn.execute(text(
            "CREATE TABLE search_doc (person_id VARCHAR(36) PRIMARY KEY, prior FLOAT, "
            "evidence_count INTEGER, source_count INTEGER, impact FLOAT)"))
        conn.execute(text("INSERT INTO search_doc VALUES ('p1', 0.5, 3, 1, 12.0)"))
    real_engine, real_build = db_module.engine, db_module._build_search_index
    db_module.engine, db_module._build_search_index = engine, lambda: None
    try:
        db_module._migrate()
    finally:
        db_module.engine, db_module._build_search_index = real_engine, real_build
    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("search_doc")}
    names = {ix["name"] for ix in inspector.get_indexes("search_doc")}
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT prior, publications, citations FROM search_doc")).one()
    engine.dispose()
    failures = 0
    for label, ok in (("totals added", {"publications", "citations"} <= columns),
                      ("indexes added",
                       {"ix_search_doc_publications", "ix_search_doc_citations"} <= names),
                      ("existing row kept", tuple(row) == (0.5, 0, 0))):
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} migration: {label}")
    return failures


def check_payloads(session) -> int:
    """A compressed source payload survives a round trip on this engine.

    models.CompressedJSON writes a wrapper dict into a JSON column and reads
    it back inflated. SQLite stores JSON as text and hands back whatever was
    put in; Postgres parses it into jsonb and may not, which is the whole
    reason this script exists. Worth checking because the payload is the
    provenance store -- if it does not round trip, the evidence is gone and
    nothing else here would notice.
    """
    from rip.models import CompressedJSON, SourceRecord
    from sqlalchemy import select

    big = {"works": [{"id": f"W{i}", "title": f"A study of things, number {i}",
                      "authorships": [{"author": {"id": "A1"}}]} for i in range(200)]}
    small = {"id": "A1", "display_name": "Ada Lovelace"}
    failures = 0
    for label, payload in (("large (compressed)", big), ("small (stored as is)", small)):
        record = SourceRecord(source="openalex", source_type="scholarly",
                              external_id=f"payload-{label[:5]}", url="https://x",
                              raw=payload)
        session.add(record)
        session.commit()
        session.expunge_all()
        back = session.execute(select(SourceRecord).where(
            SourceRecord.external_id == record.external_id)).scalar_one()
        ok = back.raw == payload
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} payload {label:22s} round trips")
        if not ok:
            print(f"       got {type(back.raw).__name__}: {str(back.raw)[:120]}")
        # And the large one really is stored compressed, not merely correct.
        # This has to go through the DRIVER: a column-level select is still
        # processed by the type, so it hands back the inflated dict and the
        # check passes for the wrong reason. It did, on the first run.
        if payload is big:
            stored = session.connection().exec_driver_sql(
                "select raw from source_record where external_id = %(x)s"
                if session.bind.dialect.paramstyle == "pyformat"
                else "select raw from source_record where external_id = ?",
                {"x": record.external_id}
                if session.bind.dialect.paramstyle == "pyformat"
                else (record.external_id,)).scalar()
            packed = CompressedJSON.KEY in str(stored)
            failures += not packed
            print(f"  {'ok  ' if packed else 'FAIL'} ...and is stored compressed")
    return failures


def run(url: str, keep: bool) -> int:
    os.environ["RIP_DATABASE_URL"] = url
    for module in [m for m in list(sys.modules) if m.startswith("rip")]:
        del sys.modules[module]
    from rip.db import Base, SessionLocal, engine, init_db

    from rip import (
        models,  # noqa: F401
        search_index,  # noqa: F401
    )

    print(f"connecting to {url.split('@')[-1]}")
    init_db()
    with SessionLocal() as session:
        seed(session)
        search_index.rebuild(session)
        session.commit()
        got = answers(session)
        legacy = answers(session, indexed=False)
        facet_failures = check_facets(session)
        payload_failures = check_payloads(session)

    failures = facet_failures + payload_failures
    for query, expected in CASES:
        names, total = got[query]
        ok = names == expected and total == len(expected)
        failures += not ok
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {query:52s} {sorted(names)} (total {total})")
        if not ok:
            print(f"       expected {sorted(expected)}")
        names_sql, total_sql = legacy[query]
        # the pre-index path answers the same question, minus the totals only
        # the index stores (source-reported publication and citation counts)
        if "citations" in query or "papers" in query:
            continue
        same = (names_sql, total_sql) == (names, total)
        failures += not same
        print(f"  {'ok  ' if same else 'FAIL'} ...and without the index")
        if not same:
            print(f"       index {sorted(names)} ({total}) vs sql {sorted(names_sql)} ({total_sql})")
    if not keep:
        Base.metadata.drop_all(engine)
        print("dropped the tables it created")
    return failures


def from_env_file() -> str | None:
    """RIP_TEST_POSTGRES_URL out of .env, so the password stays in your file."""
    path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    if not os.path.exists(path):
        return None
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if line.startswith("RIP_TEST_POSTGRES_URL="):
            return line.split("=", 1)[1].strip().strip("\"'") or None
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("RIP_TEST_POSTGRES_URL") or from_env_file())
    parser.add_argument("--keep", action="store_true", help="leave the tables in place")
    args = parser.parse_args()
    if not args.url:
        sys.exit("set RIP_TEST_POSTGRES_URL (or pass --url) to a Postgres database "
                 "this may create and drop tables in")
    if "postgres" not in args.url:
        sys.exit("that is not a Postgres URL")
    failures = check_migration(args.url) + run(args.url, args.keep)
    print("\nall query paths agree with SQLite" if not failures else f"\n{failures} mismatches")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
