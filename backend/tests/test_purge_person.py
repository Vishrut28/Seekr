"""Removing a record that is not a person.

Nothing else here deletes a person: merging leaves a tombstone so the id stays
resolvable, and a real person deleted by mistake is lost silently. So this
script's authorisation is a rule, not a flag -- it refuses anything
personhood.assess still calls a person, which means it can only ever remove
what ingest would now refuse at the door.

These pin down that gate, and the two ways a sweep like this goes wrong: it
takes work that belonged to somebody else, or it leaves a row pointing at an
id that is gone.
"""

import pytest
from sqlalchemy import func, select

from rip.models import (Authorship, ChangeLog, Evidence, IdentityLink, Person,
                        PersonKey, PersonNameToken, Publication, SourceRecord)
from rip.search_index import SearchTerm
from scripts.purge_person import plan, purge, referencing_columns


def publisher(session, name="Verlag Hans Huber", external_id="A5081967324"):
    """A record like the one this was written for: OpenAlex credits a
    publisher as an author, and its catalogue arrives as that author's work."""
    person = Person(canonical_name=name)
    record = SourceRecord(source="openalex", source_type="scholarly",
                          external_id=external_id, raw={})
    session.add_all([person, record])
    session.flush()
    session.add_all([
        IdentityLink(person_id=person.id, source_record_id=record.id,
                     match_confidence=1.0, match_method="new"),
        PersonKey(person_id=person.id, key_type="url",
                  key_value=f"openalex.org/{external_id}",
                  source_record_id=record.id),
        Evidence(person_id=person.id, attribute_type="research_interest",
                 value="Haemochromatose", source="openalex",
                 source_record_id=record.id, confidence=0.6,
                 verification_state="unverified"),
        PersonNameToken(person_id=person.id, token="verlag"),
        SearchTerm(person_id=person.id, term="verlag", field="n"),
        ChangeLog(person_id=person.id, field="name", old_value=None, new_value=name),
    ])
    for i in range(3):
        pub = Publication(title=f"Ein Handbuch {i}", external_id=f"{external_id}-w{i}",
                          source_record_id=record.id, raw_authors=[name])
        session.add(pub)
        session.flush()
        session.add(Authorship(person_id=person.id, publication_id=pub.id))
    session.commit()
    return person, record


def test_a_record_personhood_still_calls_a_person_is_refused(session):
    """The whole authorisation. A real name must be untouchable here, however
    it is asked for."""
    from rip.personhood import assess

    person = Person(canonical_name="Hans Huber")
    session.add(person)
    session.commit()
    assert assess(person.canonical_name, "openalex").is_person, \
        "fixture must be a name the gate accepts, or this proves nothing"


def test_the_publisher_and_its_catalogue_go(session):
    person, record = publisher(session)
    proposed = plan(session, person)
    assert proposed["records"] == [record.id]
    assert len(proposed["publications"]) == 3

    purge(session, person, proposed)

    assert session.get(Person, person.id) is None
    assert session.get(SourceRecord, record.id) is None
    for model in (Authorship, Evidence, IdentityLink, PersonKey,
                  PersonNameToken, SearchTerm, ChangeLog):
        assert session.execute(select(func.count()).select_from(model)).scalar() == 0, model
    assert session.execute(select(func.count()).select_from(Publication)).scalar() == 0


def test_a_book_with_a_real_author_on_it_stays(session):
    """The publisher is credited alongside a real author on one title. That
    book is the author's: only the publisher's own claim to it comes off."""
    person, record = publisher(session)
    author = Person(canonical_name="Karl F. Wender")
    session.add(author)
    session.flush()
    shared = Publication(title="Neuronale Netze", external_id="W18270617",
                         source_record_id=record.id, raw_authors=["Karl F. Wender"])
    session.add(shared)
    session.flush()
    session.add_all([Authorship(person_id=author.id, publication_id=shared.id),
                     Authorship(person_id=person.id, publication_id=shared.id)])
    session.commit()

    proposed = plan(session, person)
    assert shared.id not in proposed["publications"]
    assert [p for p, _n, _t in proposed["kept_publications"]] == [shared.id]

    purge(session, person, proposed)

    assert session.get(Publication, shared.id) is not None
    assert session.get(Person, author.id) is not None
    left = session.execute(select(Authorship.person_id).where(
        Authorship.publication_id == shared.id)).scalars().all()
    assert left == [author.id], "the real author keeps the book, the publisher does not"


def test_the_schema_forbids_two_people_holding_one_record(session):
    """Why the purge can take a person's records without asking who else
    holds them: identity_link is unique on source_record_id, so nobody else
    can. A person may hold SEVERAL records -- the merged combinatorialist
    holds three -- but not the reverse. Written down because a reader would
    otherwise expect plan() to check, and a check here could never fire.
    """
    from sqlalchemy.exc import IntegrityError

    person, record = publisher(session)
    other = Person(canonical_name="Hans Huber")
    session.add(other)
    session.flush()
    session.add(IdentityLink(person_id=other.id, source_record_id=record.id,
                             match_confidence=1.0, match_method="new"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_a_publication_from_another_record_is_left_alone(session):
    """Work only comes off with the record that brought it. The publisher's
    authorship goes either way; the publication goes only if it arrived on a
    record being deleted."""
    person, _record = publisher(session)
    elsewhere = SourceRecord(source="openalex", source_type="scholarly",
                             external_id="A999", raw={})
    session.add(elsewhere)
    session.flush()
    borrowed = Publication(title="Ein fremdes Buch", external_id="W999",
                           source_record_id=elsewhere.id, raw_authors=["Someone"])
    session.add(borrowed)
    session.flush()
    session.add(Authorship(person_id=person.id, publication_id=borrowed.id))
    session.commit()

    proposed = plan(session, person)
    assert borrowed.id not in proposed["publications"]
    assert len(proposed["publications"]) == 3

    purge(session, person, proposed)
    assert session.get(Publication, borrowed.id) is not None
    assert session.get(SourceRecord, elsewhere.id) is not None


def test_nothing_is_left_pointing_at_a_deleted_id(session):
    """SQLite tolerates a dangling foreign key and Postgres does not, and this
    project supports both. Checked by walking the mapped tables, because the
    hand-written list of what references a person missed person_name_token."""
    person, record = publisher(session)
    purge(session, person, plan(session, person))

    for table, gone in (("person", person.id), ("source_record", record.id)):
        for model, column in referencing_columns(table):
            dangling = session.execute(
                select(func.count()).select_from(model).where(column == gone)).scalar()
            assert dangling == 0, f"{model.__tablename__}.{column.name} still points at it"


def test_the_table_walk_finds_more_than_a_person_would_write_down(session):
    """The reason plan() introspects instead of listing. If this ever drops
    below what a reader expects, a table stopped being reachable."""
    found = {m.__tablename__ for m, _c in referencing_columns("person")}
    assert {"authorship", "evidence", "identity_link", "person_key",
            "person_name_token", "search_term", "change_log",
            "merge_candidate", "conflation_review"} <= found
    assert len(found) >= 12, sorted(found)


def test_referencing_columns_answers_for_the_table_it_was_asked_about():
    """plan() asks about three different tables, and every one must get its
    own answer. A version that always answered "person" passed every other
    test here -- including the dangling-reference sweep, because that sweep
    calls this same function and was asking the wrong question too."""
    person_tables = {m.__tablename__ for m, _c in referencing_columns("person")}
    record_tables = {m.__tablename__ for m, _c in referencing_columns("source_record")}
    pub_tables = {m.__tablename__ for m, _c in referencing_columns("publication")}

    assert pub_tables == {"authorship"}, sorted(pub_tables)
    assert "publication" in record_tables and "publication" not in person_tables
    assert "authorship" in person_tables and "search_term" not in record_tables
    for model, column in referencing_columns("source_record"):
        assert [fk.column.table.name for fk in column.foreign_keys] == ["source_record"], \
            f"{model.__tablename__}.{column.name} is not a source_record key"


def run_script(tmp_path, *args, seed=None):
    """The script end to end against its own database, because its two most
    important refusals live in main() and nothing else reaches them."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rip.db import Base

    backend = Path(__file__).resolve().parents[1]
    db = tmp_path / "rip.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    if seed:
        with sessionmaker(bind=engine)() as session:
            seed(session)
            session.commit()
    env = dict(os.environ, RIP_DATABASE_URL=f"sqlite:///{db}",
               PYTHONIOENCODING="utf-8")
    done = subprocess.run([sys.executable, "scripts/purge_person.py", *args],
                          cwd=backend, env=env, capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


def test_the_script_refuses_a_real_person(tmp_path):
    """The authorisation. Asked to purge somebody personhood accepts, it must
    refuse and change nothing -- this is the only thing standing between the
    script and a real record."""
    def seed(session):
        session.add(Person(id="aaaaaaaa-0000-0000-0000-000000000000",
                           canonical_name="Hans Huber"))

    code, output = run_script(tmp_path, "aaaaaaaa", "--apply", seed=seed)
    assert code != 0, output
    assert "REFUSED" in output, output
    assert "personhood" in output, output
    assert "rows to delete" not in output, f"it planned a deletion anyway:\n{output}"


def test_the_script_refuses_an_id_prefix_matching_more_than_one(tmp_path):
    """A short prefix must never resolve to "whichever came first"."""
    def seed(session):
        for n in range(2):
            session.add(Person(id=f"bbbbbbbb-000{n}-0000-0000-000000000000",
                               canonical_name="Verlag Hans Huber"))

    code, output = run_script(tmp_path, "bbbbbbbb", "--apply", seed=seed)
    assert code != 0, output
    assert "matches 2 people" in output, output


def test_the_script_plans_without_applying(tmp_path):
    def seed(session):
        session.add(Person(id="cccccccc-0000-0000-0000-000000000000",
                           canonical_name="Verlag Hans Huber"))

    code, output = run_script(tmp_path, "cccccccc", seed=seed)
    assert code == 0, output
    assert "plan only" in output and "purged" not in output
def test_an_empty_record_is_removable_though_personhood_cannot_rule_on_it(session):
    """The second way past the gate. assess() returns "nothing to judge" for a
    record with no name, which let an empty Europe PMC payload through ingest
    and then blocked it from being cleaned up: personhood still called it a
    person. A record with no name, no evidence and no papers cannot cost
    anybody their data, because it holds none."""
    from rip.personhood import assess
    from scripts.purge_person import is_empty

    nobody = Person(canonical_name=None)
    session.add(nobody)
    session.flush()
    assert assess(nobody.canonical_name, "openalex").is_person   # cannot rule
    assert is_empty(session, nobody)                             # but holds nothing


def test_a_record_holding_anything_at_all_is_not_empty(session):
    from scripts.purge_person import is_empty

    named = Person(canonical_name="Jes Olesen")
    nameless = Person(canonical_name=None)
    session.add_all([named, nameless])
    session.flush()
    assert not is_empty(session, named)
    session.add(Evidence(person_id=nameless.id, attribute_type="research_interest",
                         value="Migraine", source="europepmc"))
    session.flush()
    assert not is_empty(session, nameless)
