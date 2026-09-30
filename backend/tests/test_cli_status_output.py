"""What `check-db` and `serve` print, which is output written to be shown.

`check-db` has one job: produce a status page an operator reads and pastes
somewhere — a bug report, a chat, a ticket. It printed the database URL
verbatim. On SQLite that is a file path. On Postgres it is

    postgresql+psycopg://user:PASSWORD@host:5432/db

so the command whose output exists to be shared was the one handing over the
password, and `serve` wrote the same line into its startup log. The Postgres
path is supported and documented (docs/POSTGRES.md), and test_cli.py already
had an operator's .env holding exactly that shape of URL.

The credential block below it was already careful — it prints "set" or
"NOT SET" and never a value. This puts the URL on the same footing, and
tests both, because "prints a status line" and "prints a status line without
the secret in it" are not the same requirement and only one of them was met.
"""

import io
from contextlib import redirect_stdout

import pytest

from rip import cli
from rip.db import printable_url

SECRET = "hunter2-do-not-print-me"
PG = f"postgresql+psycopg://seekr:{SECRET}@db.example.org:5432/rip"


# --------------------------------------------------------------------------
# the redaction itself
# --------------------------------------------------------------------------

def test_a_postgres_password_is_replaced():
    shown = printable_url(PG)
    assert SECRET not in shown
    # and everything an operator actually needs is still there
    assert "postgresql+psycopg" in shown
    assert "db.example.org:5432" in shown
    assert "seekr" in shown
    assert "/rip" in shown


@pytest.mark.parametrize("url", [
    "sqlite:///rip.db",
    "sqlite:///file:snap.db?mode=ro&uri=true",
    "postgresql://user@host/db",          # no password to hide
    "sqlite://",
])
def test_a_url_with_no_password_is_left_alone(url):
    """Redaction must not damage the common case: this line is how an
    operator confirms they are pointed at the database they meant."""
    assert printable_url(url) == url


def test_an_unparseable_url_is_not_printed_rather_than_crashing():
    """A status command that dies on a malformed URL tells you less than one
    that says so — but it must not fall back to printing the raw string."""
    shown = printable_url("this is not a url at all::::")
    assert "unparseable" in shown
    assert "::::" not in shown


# --------------------------------------------------------------------------
# through the command
# --------------------------------------------------------------------------

def _check_db_output(monkeypatch, url):
    monkeypatch.setattr("rip.db.DB_URL", url, raising=False)
    monkeypatch.setattr(cli, "init_db", lambda *_a, **_k: None)
    out = io.StringIO()
    with redirect_stdout(out):
        cli.cmd_check_db(type("Args", (), {})())
    return out.getvalue()


def test_check_db_does_not_print_the_password(monkeypatch, session):
    monkeypatch.setattr("rip.db.SessionLocal", lambda: session)
    monkeypatch.setattr(cli, "SessionLocal", lambda: session, raising=False)
    text = _check_db_output(monkeypatch, PG)
    assert SECRET not in text, "check-db printed the database password"
    assert "db.example.org" in text, "and it must still say which database"


def test_check_db_never_prints_a_credential_value(monkeypatch, session):
    """The credential block reports whether each one is set. A value in there
    would be the same leak by a different route."""
    monkeypatch.setattr("rip.db.SessionLocal", lambda: session)
    monkeypatch.setattr(cli, "SessionLocal", lambda: session, raising=False)
    for var, value in (("GITHUB_TOKEN", "ghp_REALTOKENVALUE"),
                       ("SEMANTIC_SCHOLAR_API_KEY", "s2-REALKEYVALUE"),
                       ("RIP_API_TOKEN", "bearer-REALSECRET")):
        monkeypatch.setenv(var, value)

    text = _check_db_output(monkeypatch, "sqlite://")
    for value in ("ghp_REALTOKENVALUE", "s2-REALKEYVALUE", "bearer-REALSECRET"):
        assert value not in text, f"check-db printed {value}"
    assert "GITHUB_TOKEN" in text and "set" in text


def test_check_db_reports_what_is_missing_as_missing(monkeypatch, session):
    monkeypatch.setattr("rip.db.SessionLocal", lambda: session)
    monkeypatch.setattr(cli, "SessionLocal", lambda: session, raising=False)
    monkeypatch.delenv("OPENALEX_MAILTO", raising=False)
    text = _check_db_output(monkeypatch, "sqlite://")
    line = next(ln for ln in text.splitlines() if "OPENALEX_MAILTO" in ln)
    assert "NOT SET" in line
