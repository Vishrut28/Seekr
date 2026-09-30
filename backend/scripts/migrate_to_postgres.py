"""Copy the local SQLite corpus into a Postgres database.

Table-by-table, batched, in dependency order. Idempotent per table: existing
rows with the same primary key are skipped, so an interrupted run can be
resumed by re-running.

Usage:
    RIP_DATABASE_URL=postgresql+psycopg://user:pass@host/db \\
      python scripts/migrate_to_postgres.py [source.db]

The source stays untouched; ingestion can keep running against SQLite while
the read API is switched over.
"""

import os
import sys
from typing import cast

from sqlalchemy import Table, create_engine, func, insert, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rip.db import Base  # noqa: E402
from rip.models import (  # noqa: E402
    Affiliation,
    AttributeConflict,
    Authorship,
    ChangeLog,
    Contribution,
    DiscoveryLead,
    Evidence,
    IdentityLink,
    IngestionRun,
    MergeCandidate,
    Organization,
    Person,
    PersonKey,
    PersonNameToken,
    Project,
    Publication,
    SourceRecord,
    WebhookDelivery,
    WebhookSubscription,
)

SOURCE = sys.argv[1] if len(sys.argv) > 1 else "rip.db"
BATCH = 2000

# parents before children
ORDER = [
    Person, SourceRecord, Organization, Publication, Project,
    IdentityLink, PersonKey, PersonNameToken, Evidence, Affiliation,
    Authorship, Contribution, AttributeConflict, MergeCandidate,
    DiscoveryLead, ChangeLog, IngestionRun,
    WebhookSubscription, WebhookDelivery,
]


def main() -> None:
    target_url = os.environ.get("RIP_DATABASE_URL", "")
    if not target_url or target_url.startswith("sqlite"):
        raise SystemExit(
            "set RIP_DATABASE_URL to a Postgres URL, e.g.\n"
            "  RIP_DATABASE_URL=postgresql+psycopg://user:pass@host/db "
            "python scripts/migrate_to_postgres.py"
        )

    src = create_engine(f"sqlite:///{SOURCE}")
    dst = create_engine(target_url)
    Base.metadata.create_all(dst)
    SrcSession = sessionmaker(bind=src)
    DstSession = sessionmaker(bind=dst)

    total = 0
    with SrcSession() as s, DstSession() as d:
        for model in ORDER:
            table = cast(Table, model.__table__)
            pk = list(table.primary_key.columns)[0]
            existing = d.execute(select(func.count()).select_from(table)).scalar_one()
            if existing:
                print(f"  {table.name:24} {existing:>9,} already present, skipping")
                total += existing
                continue

            rows = s.execute(select(table)).mappings().all()
            copied = 0
            for i in range(0, len(rows), BATCH):
                chunk = [dict(r) for r in rows[i : i + BATCH]]
                if chunk:
                    d.execute(insert(table), chunk)
                    d.commit()
                    copied += len(chunk)
            # keep Postgres sequences ahead of the copied integer ids
            if pk.autoincrement and str(pk.type).startswith("INTEGER"):
                d.execute(
                    func.setval(
                        f"{table.name}_{pk.name}_seq",
                        select(func.coalesce(func.max(pk), 1)).scalar_subquery(),
                    )
                )
                d.commit()
            print(f"  {table.name:24} {copied:>9,} copied")
            total += copied

    print(f"\nmigrated {total:,} rows to {target_url.split('@')[-1]}")
    print("verify with: RIP_DATABASE_URL=... python -m rip.cli check-db")


if __name__ == "__main__":
    main()
