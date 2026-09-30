"""GitHub's rate-limit backoff state lives in SourceThrottle (a DB table)
rather than a module-level Python global — the whole point being that it's
visible across separate Sessions/connections, the same way separate worker
processes under a multi-worker deployment would each open their own
connection to the same database. A module-level global would be invisible
across those processes; these tests specifically prove the DB-backed
version is NOT.

_may_fetch_github is GitHub's own POLICY layered on top of the generic
_source_throttled/_note_source_throttled mechanism (see test_source_
throttle_generalized.py for that generic mechanism tested directly, and for
the fact every OTHER source now shares it too).
"""
import time

from rip.discovery import _may_fetch_github, _note_source_throttled, _source_throttled
from rip.models import SourceThrottle


def test_not_throttled_by_default(session):
    assert _source_throttled(session, "github") is False
    assert _may_fetch_github(session, stored_so_far=0) is True


def test_note_throttled_blocks_immediately(session):
    _note_source_throttled(session, "github", seconds=900.0)
    assert _source_throttled(session, "github") is True
    assert _may_fetch_github(session, stored_so_far=0) is False


def test_throttle_expires_after_the_given_duration(session):
    """A very short window: the block should be lifted almost immediately
    once real time passes it, without needing to wait 900 real seconds."""
    _note_source_throttled(session, "github", seconds=0.05)
    assert _source_throttled(session, "github") is True
    time.sleep(0.1)
    assert _source_throttled(session, "github") is False


def test_renoting_updates_the_existing_row_not_a_duplicate(session):
    """A second rate-limit hit must extend the SAME row, not create a
    second one — SourceThrottle has a uniqueness constraint on `source`
    specifically to guarantee this, but the application logic (fetch-then-
    update-or-insert) needs to actually respect it too."""
    _note_source_throttled(session, "github", seconds=100.0)
    _note_source_throttled(session, "github", seconds=200.0)
    rows = session.query(SourceThrottle).filter_by(source="github").all()
    assert len(rows) == 1


def test_throttle_state_is_visible_across_a_separate_session(session):
    """A weaker but still meaningful property than true cross-process
    sharing (see test_throttle_state_survives_a_separate_os_process below
    for that): the state must be read fresh from the database by a second
    Session's own query, not served from the first session's identity map
    or anything reachable only through the original session object. This
    guards against a common SQLAlchemy mistake (accidentally relying on a
    session-local cache) — it does NOT, by itself, prove real OS-process
    safety, since this test's sqlite engine is in-memory and therefore
    cannot span processes at all regardless of how the code is written.
    """
    from sqlalchemy.orm import sessionmaker

    engine = session.get_bind()
    _note_source_throttled(session, "github", seconds=900.0)

    # a second, independent Session against the SAME engine — proves the
    # state isn't scoped to the first session's own identity map
    OtherSession = sessionmaker(bind=engine, expire_on_commit=False)
    with OtherSession() as other_session:
        assert _source_throttled(other_session, "github") is True
        assert _may_fetch_github(other_session, stored_so_far=0) is False


def _write_throttle_in_subprocess(db_path: str, seconds: float) -> None:
    """Runs in a freshly spawned OS process (must be a module-level function
    to be picklable by multiprocessing's spawn context) — the current
    file's own module-level imports get re-executed from scratch here, so
    anything relying on a global set only in the parent process cannot
    possibly be visible in this process."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rip.db import Base
    from rip.discovery import _note_source_throttled

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine, expire_on_commit=False)() as s:
            _note_source_throttled(s, "github", seconds=seconds)
    finally:
        # Release the file handle before this process exits. Windows locks
        # an open file against deletion by another process in a way POSIX
        # does not — without this, the subprocess can exit while the OS
        # still considers the sqlite file "in use", and the temp-directory
        # cleanup in the parent test later fails with WinError 32. Harmless
        # (and necessary) on every platform, not just Windows.
        engine.dispose()


def test_throttle_state_survives_a_separate_os_process():
    """The actual property #17 exists for: two SEPARATE OS processes,
    talking to a real sqlite FILE (not the in-memory engine the rest of
    this suite uses, which cannot span processes by construction), must
    see the same throttle state. A module-level Python global would fail
    this test unconditionally — a spawned process starts with its own
    fresh interpreter and module state, so it could never observe a value
    only ever assigned in the PARENT process's memory. This is the
    difference the earlier same-process test above cannot actually prove.
    """
    import multiprocessing
    import shutil
    import tempfile
    import time
    from pathlib import Path

    # Not tempfile.TemporaryDirectory()'s own context-manager cleanup: on
    # Python 3.14, its internal retry logic only suppresses a
    # PermissionError for the ROOT directory itself, not for a file nested
    # inside it that still fails after one retry (a real, observed
    # difference from earlier Python versions — the "ignore_cleanup_errors"
    # flag does not cover this case there). mkdtemp() + our own
    # shutil.rmtree(..., ignore_errors=True), with a short retry loop for
    # Windows' slower/asynchronous file-lock release (antivirus/indexer
    # processes routinely hold a brief extra handle on a just-written
    # file), is deletion logic we fully control and that cannot fail the
    # test regardless of stdlib version quirks — appropriate here because
    # by the time cleanup runs, every assertion this test exists to make
    # has already passed; a leftover temp file is a harmless OS-cleanup
    # detail, not a sign anything under test is wrong.
    tmp = tempfile.mkdtemp()
    try:
        db_path = str(Path(tmp) / "cross_process_test.db")

        ctx = multiprocessing.get_context("spawn")
        proc = ctx.Process(target=_write_throttle_in_subprocess, args=(db_path, 900.0))
        proc.start()
        proc.join(timeout=30)
        assert proc.exitcode == 0, "subprocess failed to write throttle state"

        # a THIRD, separate connection, in THIS (the original test) process,
        # against the same file the subprocess wrote to
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from rip.db import Base

        engine = create_engine(f"sqlite:///{db_path}")
        try:
            Base.metadata.create_all(engine)
            with sessionmaker(bind=engine, expire_on_commit=False)() as s:
                assert _source_throttled(s, "github") is True
                assert _may_fetch_github(s, stored_so_far=0) is False
        finally:
            # Same reason as in the subprocess: release the handle before
            # cleanup tries to delete the file.
            engine.dispose()
    finally:
        for attempt in range(5):
            try:
                shutil.rmtree(tmp, ignore_errors=False)
                break
            except OSError:
                if attempt == 4:
                    # Every assertion above already ran and passed by this
                    # point — a still-locked temp file is an OS/antivirus
                    # timing quirk unrelated to what this test verifies.
                    # ignore_errors=True on this final attempt guarantees
                    # cleanup can never fail the test itself.
                    shutil.rmtree(tmp, ignore_errors=True)
                    break
                time.sleep(0.2)


def test_with_github_token_set_always_allowed_even_when_not_throttled(session, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "sometoken")
    assert _may_fetch_github(session, stored_so_far=5) is True


def test_with_github_token_set_still_blocked_while_throttled(session, monkeypatch):
    """A token raises the rate limit; it does not bypass an active block —
    GitHub can still return 429s to a token'd request under heavy load, and
    the backoff should still be respected regardless of GITHUB_TOKEN."""
    monkeypatch.setenv("GITHUB_TOKEN", "sometoken")
    _note_source_throttled(session, "github", seconds=900.0)
    assert _may_fetch_github(session, stored_so_far=5) is False


def test_without_token_only_fetches_when_nothing_stored_yet(session):
    """Unauthenticated GitHub calls are conserved for the case where free
    scholarly sources found nothing at all."""
    assert _may_fetch_github(session, stored_so_far=0) is True
    assert _may_fetch_github(session, stored_so_far=1) is False
