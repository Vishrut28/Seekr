import pytest
from rip.db import Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from rip import models  # noqa: F401


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
