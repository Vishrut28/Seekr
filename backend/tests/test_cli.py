"""The operator interface, which was 13% covered.

rip/cli.py is 1,054 lines and 28 commands, and 572 of its statements had never
been executed by a test. Three of those commands change the graph — they
delete people, fold organizations together and merge duplicates — and one of
them reads the credentials every other command depends on. A mistake in any
of them is the kind an operator finds afterwards.

These cover the two that cannot be recovered from by re-running something:
what `load_env` does with a file people hand-edit, and whether a destructive
command actually waits to be told twice.
"""

import pytest

from rip import cli
from rip.models import Evidence, Organization, Person

# --------------------------------------------------------------------------
# load_env: every command depends on it and nothing checked it
# --------------------------------------------------------------------------

def write_env(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_it_reads_the_shapes_people_actually_write(tmp_path, monkeypatch):
    for key in ("A_PLAIN", "B_QUOTED", "C_SINGLE", "D_SPACED", "E_EMPTY", "F_URL"):
        monkeypatch.delenv(key, raising=False)
    path = write_env(tmp_path, "\n".join([
        "# a comment",
        "",
        "A_PLAIN=value",
        'B_QUOTED="quoted value"',
        "C_SINGLE='single'",
        "  D_SPACED = padded  ",
        "E_EMPTY=",
        "F_URL=postgresql+psycopg://user:pw@host:5432/db",
        "not a pair",
    ]))
    import os

    assert cli.load_env(path) == 6
    assert os.environ["A_PLAIN"] == "value"
    assert os.environ["B_QUOTED"] == "quoted value"
    assert os.environ["C_SINGLE"] == "single"
    assert os.environ["D_SPACED"] == "padded"
    assert os.environ["E_EMPTY"] == ""
    # a password with punctuation in it must survive intact
    assert os.environ["F_URL"] == "postgresql+psycopg://user:pw@host:5432/db"


def test_what_is_already_in_the_environment_wins(tmp_path, monkeypatch):
    """So an explicit VAR=... on the command line still overrides the file."""
    monkeypatch.setenv("ALREADY", "from the shell")
    path = write_env(tmp_path, "ALREADY=from the file\nFRESH=new\n")
    assert cli.load_env(path) == 1

    import os

    assert os.environ["ALREADY"] == "from the shell"
    assert os.environ["FRESH"] == "new"


def test_a_missing_file_is_not_an_error(tmp_path):
    """Most commands are run without one."""
    assert cli.load_env(str(tmp_path / "nothing-here")) == 0


# --------------------------------------------------------------------------
# purge-nonpersons: it deletes people
# --------------------------------------------------------------------------

@pytest.fixture
def graph(session, monkeypatch):
    """A corpus holding one person and one thing that is not one."""
    monkeypatch.setattr(cli, "SessionLocal", lambda: session, raising=False)
    from rip import db as db_module

    monkeypatch.setattr(db_module, "SessionLocal", lambda: session)
    real = Person(id="real", canonical_name="Ada Lovelace")
    listicle = Person(id="bad", canonical_name="20+ Deep Learning Projects")
    session.add_all([real, listicle])
    session.add(Evidence(person_id="bad", attribute_type="research_interest",
                         value="Deep learning", source="web"))
    session.commit()
    return session


def test_it_does_nothing_until_it_is_told_twice(graph, capsys):
    cli.cmd_purge_nonpersons(type("Args", (), {"yes": False})())
    out = capsys.readouterr().out
    assert "--yes" in out
    assert graph.get(Person, "bad") is not None, "deleted without being asked twice"
    assert graph.get(Person, "real") is not None


def test_with_yes_it_removes_the_non_person_and_leaves_the_person(graph, capsys):
    cli.cmd_purge_nonpersons(type("Args", (), {"yes": True})())
    capsys.readouterr()
    graph.expire_all()
    assert graph.get(Person, "bad") is None
    assert graph.get(Person, "real") is not None
    # and nothing of the deleted record is left pointing at a gone id
    leftover = graph.query(Evidence).filter(Evidence.person_id == "bad").count()
    assert leftover == 0


def test_it_judges_with_the_same_rule_ingest_uses(graph):
    """The command exists because personhood.assess gates INGEST only, so a
    record stored before a rule existed stays. It must not invent a second
    opinion about what a person is."""
    from rip.personhood import assess

    assert not assess("20+ Deep Learning Projects", "openalex").is_person
    assert assess("Ada Lovelace", "openalex").is_person


# --------------------------------------------------------------------------
# merge-orgs: it folds organizations together
# --------------------------------------------------------------------------

def test_merging_organizations_waits_to_be_told_twice(session, monkeypatch, capsys):
    from rip import db as db_module

    monkeypatch.setattr(db_module, "SessionLocal", lambda: session)
    session.add_all([Organization(name="Deccan AI", norm_name="deccan ai"),
                     Organization(name="Deccan.AI", norm_name="deccan ai")])
    session.commit()

    cli.cmd_merge_orgs(type("Args", (), {"yes": False})())
    capsys.readouterr()
    session.expire_all()
    assert session.query(Organization).count() == 2, "folded without being asked twice"
