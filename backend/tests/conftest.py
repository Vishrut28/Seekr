import os
import pathlib
import tempfile

# Before ANYTHING imports rip.db, which reads RIP_DATABASE_URL once at import
# and defaults to sqlite:///rip.db -- a path relative to the working
# directory, which for anybody running the suite from backend/ is their real
# corpus. Two tests were reaching it: one through an app client with no
# database override, and one through a migration that inspected a patched
# engine while its backfill opened SessionLocal, still bound to the original.
# Both passed only because the corpus they read happened to be there, and
# both failed in a fresh checkout. This makes forgetting an override land on
# an empty throwaway database instead of on somebody's data.
_SUITE_DB = pathlib.Path(tempfile.mkdtemp(prefix="rip-tests-")) / "suite.db"
os.environ["RIP_DATABASE_URL"] = f"sqlite:///{_SUITE_DB.as_posix()}"

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from rip import db as _db  # noqa: E402
from rip import models  # noqa: E402,F401
from rip.db import Base  # noqa: E402

Base.metadata.create_all(_db.engine)


@pytest.fixture(autouse=True)
def _fresh_connectors():
    """Connectors are shared per process; no test may inherit another's."""
    yield
    from rip.connectors import reset_connectors

    reset_connectors()


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        yield s
