"""When the vocabulary is allowed to be rebuilt.

It used to expire on a timer and rebuild whether or not anything had changed.
Measured on a synthetic corpus, that rebuild does not stay cheap:

    distinct terms   _build_vocab   _build_aux     total
         2,726          188 ms        329 ms       0.5 s
        99,897        1,369 ms      5,158 ms       6.5 s
       299,894        5,998 ms     19,193 ms      25.0 s

At about 3.5 distinct terms per person that is 85,000 people spending 25
seconds of every 60 rebuilding something nobody changed, per worker process,
and past roughly 285,000 people the rebuild outlasts its own window and the
cache can never be warm.

Two checks replace the timer. A maximum id per table says nothing was ADDED,
at 0.4 ms. A count says nothing was DELETED, at 32 ms, and is asked only when
the timer expires. A corpus nobody writes to is never rebuilt.
"""

import time

import pytest
from rip.models import Evidence, Person
from sqlalchemy import select

from rip import nlq


@pytest.fixture
def corpus(session):
    person = Person(id="p1", canonical_name="Ada Lovelace")
    session.add(person)
    session.add(Evidence(person_id="p1", attribute_type="research_interest",
                         value="Analytical Engines", source="test"))
    session.commit()
    nlq.invalidate_vocab()
    return session


@pytest.fixture
def counted(monkeypatch):
    builds = []
    real = nlq._build_vocab
    monkeypatch.setattr(nlq, "_build_vocab",
                        lambda sess: (builds.append(1), real(sess))[1])
    return builds


def age_the_cache(session, seconds):
    key = nlq.si._bind_key(session)
    stamp, built, aux, fingerprint, census = nlq._vocab_cache[key]
    nlq._vocab_cache[key] = (stamp - seconds, built, aux, fingerprint, census)


def test_a_corpus_nobody_changed_is_never_rebuilt(corpus, counted):
    nlq._vocab(corpus)
    assert len(counted) == 1
    age_the_cache(corpus, 100_000)          # far past any timer
    nlq._vocab(corpus)
    for _ in range(20):
        nlq._vocab(corpus)
    assert len(counted) == 1, "rebuilt a vocabulary that could not have changed"


def test_something_added_by_another_process_is_picked_up(corpus, counted):
    """The timer existed because ingest runs elsewhere and this process never
    hears about it. The maximum id hears about it."""
    skills, _o, _l = nlq._vocab(corpus)
    assert "punched card looms" not in skills

    corpus.add(Evidence(person_id="p1", attribute_type="research_interest",
                        value="Punched Card Looms", source="test"))
    corpus.commit()
    age_the_cache(corpus, nlq.VOCAB_MIN_INTERVAL + 1)
    skills, _o, _l = nlq._vocab(corpus)
    assert len(counted) == 2
    assert "punched card looms" in skills


def test_a_busy_writer_does_not_buy_a_rebuild_for_every_query(corpus, counted):
    """Bulk ingest changes evidence constantly. Without a floor each query
    during one would pay for its own rebuild."""
    nlq._vocab(corpus)
    for i in range(5):
        corpus.add(Evidence(person_id="p1", attribute_type="research_interest",
                            value=f"Subject {i}", source="test"))
        corpus.commit()
        nlq._vocab(corpus)
    assert len(counted) == 1, "rebuilt while a writer was mid-flight"


def test_a_deletion_by_another_process_is_caught_when_the_timer_expires(corpus,
                                                                       counted):
    """A maximum id cannot fall, so deletions need the count.

    A deletion made HERE is noticed at once, because indexing bumps this
    process's generation and that is part of the fingerprint. The case the
    count exists for is a deletion made somewhere else, which this process has
    no way to hear about -- reproduced by putting the current fingerprint back
    into the cache, which is exactly what "we did not notice" looks like.
    """
    corpus.add(Evidence(person_id="p1", attribute_type="research_interest",
                        value="Difference Engines", source="test"))
    corpus.commit()
    skills, _o, _l = nlq._vocab(corpus)
    assert "difference engines" in skills
    built_so_far = len(counted)

    row = corpus.execute(select(Evidence).where(
        Evidence.value == "Difference Engines")).scalar_one()
    corpus.delete(row)
    corpus.commit()

    key = nlq.si._bind_key(corpus)
    stamp, built, aux, _fingerprint, census = nlq._vocab_cache[key]
    unnoticed = nlq._vocab_fingerprint(corpus)          # as another process left it

    nlq._vocab_cache[key] = (stamp - (nlq.VOCAB_MIN_INTERVAL + 1), built, aux,
                             unnoticed, census)
    nlq._vocab(corpus)
    assert len(counted) == built_so_far, "a deletion is not worth a rebuild yet"

    nlq._vocab_cache[key] = (stamp - (nlq.VOCAB_TTL_SECONDS + 1), built, aux,
                             unnoticed, census)
    skills, _o, _l = nlq._vocab(corpus)
    assert len(counted) == built_so_far + 1, "the count never ran"
    assert "difference engines" not in skills


def test_a_deletion_here_is_noticed_at_once(corpus, counted):
    """The other half of the same rule: this process indexes what it changes,
    and the index generation is in the fingerprint."""
    corpus.add(Evidence(person_id="p1", attribute_type="research_interest",
                        value="Bernoulli Numbers", source="test"))
    corpus.commit()
    assert "bernoulli numbers" in nlq._vocab(corpus)[0]
    row = corpus.execute(select(Evidence).where(
        Evidence.value == "Bernoulli Numbers")).scalar_one()
    corpus.delete(row)
    corpus.commit()
    age_the_cache(corpus, nlq.VOCAB_MIN_INTERVAL + 1)
    assert "bernoulli numbers" not in nlq._vocab(corpus)[0]


def test_the_cheap_check_is_cheap(corpus):
    """It runs on every query, so it has to cost nothing worth measuring."""
    nlq._vocab(corpus)
    age_the_cache(corpus, 100_000)
    started = time.perf_counter()
    for _ in range(200):
        nlq._vocab(corpus)
    each = (time.perf_counter() - started) * 1000 / 200
    assert each < 5, f"{each:.2f} ms per call just to check the cache"
