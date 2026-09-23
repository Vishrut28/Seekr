"""Database setup. SQLite by default, any SQLAlchemy URL via RIP_DATABASE_URL."""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_URL = os.environ.get("RIP_DATABASE_URL", "sqlite:///rip.db")


def printable_url(url: str = "") -> str:
    """DB_URL with the password taken out, for anything that displays it.

    `check-db` exists to be run and shown to somebody — that is what a
    diagnostic command is for — and it printed the URL verbatim. On SQLite
    that is a file path and harmless; on Postgres it is
    postgresql+psycopg://user:PASSWORD@host/db, and the command whose output
    goes into a bug report was the one handing the password over. `serve`
    printed it at startup too, into the log.

    SQLAlchemy's own renderer rather than a regex: it knows which part of a
    URL is the password in every shape of URL it accepts, and it leaves one
    without a password exactly as it found it.
    """
    from sqlalchemy.engine import make_url

    try:
        return make_url(url or DB_URL).render_as_string(hide_password=True)
    except Exception:
        # An unparseable URL is not a reason to crash a status command, but
        # it is every reason not to print it.
        return "<unparseable database url>"


class Base(DeclarativeBase):
    pass


_connect_args = {"check_same_thread": False} if DB_URL.startswith("sqlite") else {}
# Pool settings apply to server-backed engines only. SQLite uses SingletonThread/
# NullPool depending on the URL and rejects pool_size, so passing these
# unconditionally would break the default local setup.
_pool_kwargs = (
    {}
    if DB_URL.startswith("sqlite")
    else {"pool_size": 5, "max_overflow": 10, "pool_pre_ping": True, "pool_recycle": 1800}
)
engine = create_engine(DB_URL, connect_args=_connect_args, **_pool_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# True when the graph cannot be written to — the deployed snapshot is opened
# read-only, so a live search there can find people but never keep them.
READ_ONLY = "mode=ro" in DB_URL

if DB_URL.startswith("sqlite"):
    from sqlalchemy import event

    _READ_ONLY = READ_ONLY

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        """WAL lets a serving process read while an ingest process writes;
        busy_timeout waits out a writer instead of raising 'database is locked'.

        Never enable WAL on a read-only database: WAL needs to create -wal and
        -shm sidecars, which fails on a read-only filesystem (the deployed
        snapshot) and takes the whole connection down with it.
        """
        cursor = dbapi_connection.cursor()
        try:
            if not _READ_ONLY:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass
        finally:
            cursor.close()


def init_db() -> None:
    from . import models  # noqa: F401  ensure models are registered

    Base.metadata.create_all(engine)
    _migrate()


def _migrate() -> None:
    """Minimal additive migrations for pre-existing databases."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    additive = {
        "identity_link": [("review_state", "VARCHAR(32) DEFAULT 'unreviewed'")],
        "person": [("merged_into", "VARCHAR(36)"), ("country", "VARCHAR(2)")],
        "search_cache": [("person_ids", "TEXT")],
        "organization": [("norm_name", "VARCHAR(255)")],
        "discovery_lead": [("claimed_by", "VARCHAR(64)"), ("claimed_at", "TIMESTAMP")],
        # filled by the search-index rebuild that INDEX_VERSION 9 triggers
        "search_doc": [("publications", "INTEGER DEFAULT 0"), ("citations", "INTEGER DEFAULT 0")],
    }
    _backfill_name_tokens(inspector)
    for table, columns in additive.items():
        if table not in inspector.get_table_names():
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl in columns:
            if name not in existing:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
    # after the columns exist, not before: this fills one of them
    _backfill_org_norm_names(inspect(engine))
    _create_missing_indexes(inspect(engine))
    _build_search_index()


def _build_search_index() -> None:
    """Build the search index for a corpus that predates it (or an older
    INDEX_VERSION). One pass over every person; afterwards the session hooks
    keep it current incrementally."""
    import logging
    import time

    from .search_index import needs_rebuild, rebuild

    if READ_ONLY:
        return
    with SessionLocal() as session:
        if not needs_rebuild(session):
            return
        log = logging.getLogger("rip.db")
        log.warning("building search index (one-time, after upgrade)...")
        started = time.monotonic()
        n = rebuild(session)
        log.warning("search index built for %d people in %.1fs", n, time.monotonic() - started)


def _create_missing_indexes(inspector) -> None:
    """Additive index migrations — mirrors the additive-column pattern above.

    Base.metadata.create_all() only creates brand-new tables; it does not add
    an index to a table that already exists. A database created before
    ix_person_merged_into_id existed would otherwise keep paying the
    "materialize and sort before LIMIT" cost that index exists to remove
    (see the comment on Person.__table_args__) forever, on every deploy.
    """
    from sqlalchemy import text

    indexes = {
        "person": [("ix_person_merged_into_id", "merged_into, id")],
        # the output totals a count filter reads ("at least 20 papers"); a
        # threshold with no subject scans search_doc without them
        "search_doc": [("ix_search_doc_publications", "publications"),
                       ("ix_search_doc_citations", "citations")],
    }
    for table, specs in indexes.items():
        if table not in inspector.get_table_names():
            continue
        existing = {ix["name"] for ix in inspector.get_indexes(table)}
        for name, columns in specs:
            if name not in existing:
                with engine.begin() as conn:
                    conn.execute(text(f"CREATE INDEX {name} ON {table} ({columns})"))


def _backfill_org_norm_names(inspector) -> None:
    """Fill the organization identity key for rows created before it existed."""
    from sqlalchemy import text

    if "organization" not in inspector.get_table_names():
        return
    if "norm_name" not in {c["name"] for c in inspector.get_columns("organization")}:
        return
    from .models import normalize_org_name

    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, name FROM organization WHERE norm_name IS NULL")
        ).fetchall()
        for oid, name in rows:
            conn.execute(
                text("UPDATE organization SET norm_name = :n WHERE id = :i"),
                {"n": normalize_org_name(name), "i": oid},
            )


def _backfill_name_tokens(inspector) -> None:
    """Populate the blocking index for persons ingested before it existed."""
    from sqlalchemy import func, select

    if "person_name_token" not in inspector.get_table_names():
        return
    from .models import Person, PersonNameToken
    from .resolution import sync_name_tokens

    with SessionLocal() as session:
        persons = session.execute(select(func.count(Person.id))).scalar_one()
        tokens = session.execute(select(func.count(PersonNameToken.id))).scalar_one()
        if persons == 0 or tokens > 0:
            return
        for person in session.execute(select(Person)).scalars():
            sync_name_tokens(session, person)
        session.commit()
