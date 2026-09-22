"""The graph endpoint beyond one hop: co-authors of co-authors, bounded so the
answer stays a picture of who works with whom."""

import pytest
from fastapi.testclient import TestClient
from rip.db import Base
from rip.ingest import ingest_profile
from rip.normalize import NormalizedProfile, OrgAffiliation, PublicationData
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rip import api


def researcher(ext, name, papers, orgs=()):
    return NormalizedProfile(
        source="openalex", source_type="scholarly", external_id=ext,
        url=f"https://openalex.org/{ext}", raw={}, name=name,
        organizations=[OrgAffiliation(name=o) for o in orgs],
        publications=[PublicationData(title=title, external_id=pid, raw_authors=[])
                      for pid, title in papers],
    )


@pytest.fixture()
def session():
    """One in-memory database every thread sees: the test client serves
    requests on its own thread, and a per-thread SQLite connection would hand
    it an empty database."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def client(session):
    api.app.dependency_overrides[api.get_db] = lambda: session
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def ids(session):
    from rip.models import Person

    return {p.canonical_name: p.id for p in session.query(Person).all()}


@pytest.fixture()
def chain(session):
    """Ada -- Ben -- Cy -- Di -- Eve, each pair sharing one paper; Ada and Ben
    share a second. Ada works at Acme."""
    ingest_profile(session, researcher("A", "Ada Lovelace", [("P1", "one"), ("P5", "five")],
                                       orgs=["Acme Labs"]))
    ingest_profile(session, researcher("B", "Ben Carter", [("P1", "one"), ("P2", "two"),
                                                           ("P5", "five")]))
    ingest_profile(session, researcher("C", "Cy Dorsey", [("P2", "two"), ("P3", "three")]))
    ingest_profile(session, researcher("D", "Di Evans", [("P3", "three"), ("P4", "four")]))
    ingest_profile(session, researcher("E", "Eve Frost", [("P4", "four")]))
    return ids(session)


def people(body):
    return {n["label"]: n.get("hop") for n in body["nodes"] if n["type"] == "person"}


def test_depth_one_is_the_person_their_organizations_and_co_authors(client, chain):
    body = client.get(f"/v1/persons/{chain['Ada Lovelace']}/graph").json()
    assert people(body) == {"Ada Lovelace": 0, "Ben Carter": 1}
    assert [n["label"] for n in body["nodes"] if n["type"] == "organization"] == ["Acme Labs"]
    coauthor = [e for e in body["edges"] if e["type"] == "coauthor"]
    assert len(coauthor) == 1 and coauthor[0]["shared_publications"] == 2
    assert body["truncated"] is False


def test_each_hop_reaches_one_step_further(client, chain):
    ada = chain["Ada Lovelace"]
    assert people(client.get(f"/v1/persons/{ada}/graph?depth=2").json()) == \
        {"Ada Lovelace": 0, "Ben Carter": 1, "Cy Dorsey": 2}
    body = client.get(f"/v1/persons/{ada}/graph?depth=3").json()
    assert people(body) == {"Ada Lovelace": 0, "Ben Carter": 1, "Cy Dorsey": 2, "Di Evans": 3}
    # a path, not a star: every edge joins one hop to the next
    pairs = {(e["from"], e["to"]) for e in body["edges"] if e["type"] == "coauthor"}
    assert pairs == {(ada, chain["Ben Carter"]), (chain["Ben Carter"], chain["Cy Dorsey"]),
                     (chain["Cy Dorsey"], chain["Di Evans"])}


def test_depth_is_capped_at_three(client, chain):
    assert client.get(f"/v1/persons/{chain['Ada Lovelace']}/graph?depth=4").status_code == 422


def test_an_edge_between_two_people_already_drawn_is_drawn_once(client, session):
    """A triangle: Ada, Ben and Cy all wrote P1."""
    for ext, name in (("A", "Ada Lovelace"), ("B", "Ben Carter"), ("C", "Cy Dorsey")):
        ingest_profile(session, researcher(ext, name, [("P1", "shared")]))
    who = ids(session)
    body = client.get(f"/v1/persons/{who['Ada Lovelace']}/graph?depth=2").json()
    edges = [frozenset((e["from"], e["to"])) for e in body["edges"] if e["type"] == "coauthor"]
    assert len(edges) == len(set(edges)) == 3            # Ada-Ben, Ada-Cy, Ben-Cy


def test_the_walk_stops_adding_people_at_max_nodes_and_says_so(client, session):
    ingest_profile(session, researcher("hub", "Hub Person", [(f"P{i}", f"p{i}") for i in range(8)]))
    for i in range(8):
        ingest_profile(session, researcher(f"x{i}", f"Coauthor Number{i}", [(f"P{i}", f"p{i}")]))
    hub = ids(session)["Hub Person"]
    body = client.get(f"/v1/persons/{hub}/graph?max_nodes=4").json()
    assert len(people(body)) == 4 and body["truncated"] is True
    capped = client.get(f"/v1/persons/{hub}/graph?limit_coauthors=3").json()
    assert len(people(capped)) == 4 and capped["truncated"] is False


def test_a_consortium_paper_is_not_followed_past_the_first_hop(client, session, monkeypatch):
    """Everyone on a huge collaboration is everyone's co-author; expanding a
    frontier across one says nothing about who works with whom."""
    from rip import graph

    monkeypatch.setattr(graph, "MAX_TEAM_FOR_EXPANSION", 5)
    ingest_profile(session, researcher("A", "Ada Lovelace", [("P1", "small")]))
    ingest_profile(session, researcher("B", "Ben Carter", [("P1", "small"), ("BIG", "consortium")]))
    for i in range(8):
        ingest_profile(session, researcher(f"m{i}", f"Member Number{i}", [("BIG", "consortium")]))
    who = ids(session)
    near = people(client.get(f"/v1/persons/{who['Ben Carter']}/graph").json())
    assert len(near) == 10                               # one hop: shown as it is
    far = people(client.get(f"/v1/persons/{who['Ada Lovelace']}/graph?depth=2").json())
    assert far == {"Ada Lovelace": 0, "Ben Carter": 1}   # not followed through BIG


def test_merged_away_people_are_not_drawn(client, chain, session):
    from rip.models import Person

    session.get(Person, chain["Ben Carter"]).merged_into = chain["Ada Lovelace"]
    session.commit()
    body = client.get(f"/v1/persons/{chain['Ada Lovelace']}/graph?depth=3").json()
    assert people(body) == {"Ada Lovelace": 0}
