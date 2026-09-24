"""The test suite must not be able to reach anybody's real corpus.

rip.db reads RIP_DATABASE_URL once, at import, and defaults to
sqlite:///rip.db -- relative to the working directory, which for anybody
running the suite from backend/ is their live graph. Two tests reached it
without meaning to, passed because the corpus happened to be there, and
failed in any fresh checkout. tests/conftest.py now points the suite at a
throwaway database before rip.db is imported; this is the check that it
still does.
"""

import pathlib
import tempfile


def test_the_suite_is_not_pointed_at_the_default_database():
    from rip import db

    assert db.DB_URL != "sqlite:///rip.db", "the suite is using the real corpus"
    assert db.DB_URL.startswith("sqlite:///")


def test_the_suite_database_lives_in_a_temporary_directory():
    from rip import db

    path = pathlib.Path(db.DB_URL[len("sqlite:///"):]).resolve()
    temp_root = pathlib.Path(tempfile.gettempdir()).resolve()
    assert temp_root in path.parents, path
    assert path.name != "rip.db", path
