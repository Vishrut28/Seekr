"""Build the read-only deploy snapshot from the live ingest DB.

Strips raw payloads (nothing serves them) and — importantly — checkpoints the
WAL and switches the copy back to a rollback journal. A WAL-mode database
cannot be opened on a read-only filesystem, because SQLite must create the
-wal/-shm sidecars first.

Usage: python scripts/build_snapshot.py [source.db] [dest.db]
"""

import os
import shutil
import sqlite3
import sys

SOURCE = sys.argv[1] if len(sys.argv) > 1 else "rip.db"
DEST = sys.argv[2] if len(sys.argv) > 2 else "data/rip.db"
# Vercel rejects any deployed file over 100 MB
MAX_BYTES = 100 * 1024 * 1024
CHANGE_LOG_WINDOW = 40_000


def main() -> None:
    # checkpoint the live DB so the copy contains every committed row
    with sqlite3.connect(SOURCE) as live:
        live.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    os.makedirs(os.path.dirname(DEST) or ".", exist_ok=True)
    shutil.copyfile(SOURCE, DEST)
    for sidecar in ("-wal", "-shm"):
        if os.path.exists(DEST + sidecar):
            os.remove(DEST + sidecar)

    con = sqlite3.connect(DEST)
    try:
        con.execute("UPDATE source_record SET raw='{}'")
        # Ingest-only tables: never read by the API, but they dominate the file
        # (the blocking index alone is ~20 rows per person). Vercel rejects any
        # file over 100 MB, so shipping them would cap the corpus at ~8k people.
        for table in ("person_name_token", "discovery_lead"):
            try:
                con.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                pass  # table not present in older snapshots
        # keep only recent operational history; /v1/health/sources needs a
        # sample, not the full log
        con.execute(
            "DELETE FROM ingestion_run WHERE id NOT IN "
            "(SELECT id FROM ingestion_run ORDER BY id DESC LIMIT 500)"
        )
        # /v1/changes carries a rolling window in the snapshot. A consumer whose
        # cursor predates the window must re-backfill from /v1/persons; the
        # window is reported by the API so they can detect that.
        con.execute(
            f"DELETE FROM change_log WHERE id NOT IN "
            f"(SELECT id FROM change_log ORDER BY id DESC LIMIT {CHANGE_LOG_WINDOW})"
        )
        con.commit()
        # plain journal: required for read-only hosting
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("VACUUM")
    finally:
        con.close()
    for sidecar in ("-wal", "-shm"):
        if os.path.exists(DEST + sidecar):
            os.remove(DEST + sidecar)

    mode = sqlite3.connect(f"file:{DEST}?mode=ro", uri=True).execute(
        "PRAGMA journal_mode"
    ).fetchone()[0]
    if os.path.getsize(DEST) > MAX_BYTES:
        # The search index is a read-path accelerator, not data: without it
        # /v1/query falls back to plain SQL filters and returns the same people,
        # just more slowly. Give up speed before giving up people.
        con = sqlite3.connect(DEST)
        try:
            dropped = False
            for table in ("search_term", "search_doc", "search_index_state"):
                try:
                    con.execute(f"DROP TABLE {table}")
                    dropped = True
                except sqlite3.OperationalError:
                    pass
            if dropped:
                con.commit()
                con.execute("VACUUM")
                print("snapshot over the size limit with the search index; "
                      "shipping without it (search uses the slower SQL fallback)")
        finally:
            con.close()

    size_mb = os.path.getsize(DEST) / 1e6
    print(f"snapshot {DEST}: {size_mb:.1f} MB, journal_mode={mode}")
    if mode == "wal":
        raise SystemExit("refusing to ship a WAL snapshot — it cannot be read on Vercel")
    if os.path.getsize(DEST) > MAX_BYTES:
        raise SystemExit(
            f"snapshot is {size_mb:.0f} MB, over Vercel's 100 MB file limit.\n"
            "The corpus has outgrown the snapshot model — move the read API to "
            "Postgres (see docs/POSTGRES.md) and point RIP_DATABASE_URL at it."
        )


if __name__ == "__main__":
    main()
