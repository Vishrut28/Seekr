# Running on Postgres

SQLite is the default and is fine into the low hundreds of thousands of
persons. Move to Postgres when you want concurrent writers (several ingest
workers), a serving API that reads live data instead of a snapshot, or
trigram indexes for name matching at 100K+ persons.

**No code changes are required.** Every query is SQLAlchemy Core/ORM; the only
SQLite-specific code is the PRAGMA hook in `rip/db.py`, which is skipped for
non-SQLite URLs.

## Switch

```bash
export RIP_DATABASE_URL="postgresql+psycopg://user:pass@host:5432/rip"
pip install "psycopg[binary]"
python -m rip.cli init-db      # creates tables + runs additive migrations
```

The read API picks up the same variable. On Vercel, set `RIP_DATABASE_URL` as
a project env var and the function serves live data instead of the bundled
snapshot — at which point `data/rip.db` and the snapshot step in
`scripts/nightly_refresh.sh` become unnecessary.

## Several workers

`rip.cli worker --processes N` (and `ingest-leads`, `refresh`) runs on SQLite
too, but SQLite takes one writer at a time, so its workers queue for the lock
on every write. Postgres writes concurrently, which is where more processes
keep paying off.

Workers never take the same lead: each claims its batch with one statement,

```sql
UPDATE discovery_lead SET status = 'claimed', claimed_by = :worker, claimed_at = now()
WHERE id IN (SELECT id FROM discovery_lead WHERE status = 'pending'
             ORDER BY created_at, id LIMIT :n FOR UPDATE SKIP LOCKED)
  AND status = 'pending'
```

Verified against Postgres 18 with two sessions: while the first held rows 1–5
in an open transaction, the second claimed rows 6–10 immediately. Without
`SKIP LOCKED` it would have waited for the first and then found its rows gone.

## When you must move

The Vercel snapshot model has a hard ceiling: **Vercel rejects any deployed
file over 100 MB**, which is about **14,000 people** once evidence,
publications and affiliations are counted. `scripts/build_snapshot.py` refuses
to build past that rather than producing a deploy that fails at runtime.
Beyond that size, Postgres is required, not optional.

## Migrating existing data

`scripts/migrate_to_postgres.py` copies the SQLite corpus table by table in
dependency order, batched, and advances the Postgres sequences afterwards. It
skips tables that already have rows, so an interrupted run resumes cleanly.
The SQLite file is never modified — ingestion can keep running against it
while the read API is switched over.

```bash
export RIP_DATABASE_URL="postgresql+psycopg://user:pass@host/rip"
pip install "psycopg[binary]"
python scripts/migrate_to_postgres.py            # defaults to rip.db
RIP_DATABASE_URL=$RIP_DATABASE_URL python -m rip.cli check-db
```

Verified locally against Postgres 17: 1,063,024 rows across 19 tables,
after which every read endpoint (`/v1/persons`, `/v1/query`, `graph`,
`changes`, `provenance`) returns identical results to SQLite.

Then set `RIP_DATABASE_URL` as a Vercel env var and redeploy; `api/index.py`
only falls back to the bundled snapshot when that variable is unset, so no
code change is needed.

### One portability trap, already fixed

Postgres has no equality operator for the `json` type, so
`SELECT DISTINCT` over a row containing a JSON column fails —
while SQLite accepts it silently. `list_persons` and `nlq.execute` therefore
de-duplicate on `person.id` and fetch rows by id. Keep that pattern for any
new query that joins and needs de-duplication.

## Recommended indexes at scale

Entity resolution blocks on `person_name_token.token` and organization name,
both already indexed. Above ~100K persons, add trigram search for the fuzzy
comparison itself:

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX CONCURRENTLY ix_person_name_trgm
  ON person USING gin (lower(canonical_name) gin_trgm_ops);
CREATE INDEX CONCURRENTLY ix_org_name_trgm
  ON organization USING gin (lower(name) gin_trgm_ops);
-- evidence lookups by attribute (powers /v1/query and profile aggregation)
CREATE INDEX CONCURRENTLY ix_evidence_person_attr
  ON evidence (person_id, attribute_type);
-- change-feed cursor scans
CREATE INDEX CONCURRENTLY ix_changelog_id ON change_log (id);
```

## Concurrency notes

- On SQLite, concurrent ingest + serve works because `rip/db.py` enables WAL
  and a 5s busy timeout, but writes still serialize. Postgres removes that
  ceiling.
- `rip.cli worker` can then run several instances; each claims leads in its
  own transaction. Deduplication is enforced by unique constraints
  (`uq_source_external`, `uq_lead`, `uq_person_name_token`), so a race
  produces a constraint error and a retry, never a duplicate person.
- Webhook delivery (`rip.cli deliver-webhooks`) should stay a single process;
  it preserves per-subscription ordering by delivery id.

## What does not change

Person UUIDs, the change-feed cursor semantics, provenance, and the API
contract are all storage-independent. A downstream ranking tool cannot tell
which backend is in use.
