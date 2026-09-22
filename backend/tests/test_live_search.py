"""Live search across sources: polite to each source, concurrent across them."""

import threading
import time

from rip.normalize import NormalizedProfile


def test_one_connector_spaces_concurrent_requests():
    """Parallel fetches through one source must still honour its interval."""
    from rip.connectors.base import BaseConnector

    stamps = []

    class FakeResponse:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {}

    class FakeClient:
        def get(self, url, params=None):
            stamps.append(time.monotonic())
            return FakeResponse()

    class Slow(BaseConnector):
        source = "slow"
        min_request_interval = 0.15

        def fetch(self, identifier):
            raise NotImplementedError

    conn = Slow()
    conn._client = FakeClient()
    threads = [threading.Thread(target=conn.get_json, args=("https://x",)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 5
    assert min(gaps) >= 0.13, gaps


def test_get_connector_shares_one_instance_per_source_and_credentials(monkeypatch):
    from rip.connectors import get_connector

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    a = get_connector("openalex")
    assert get_connector("openalex") is a
    plain = get_connector("github")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    # a token set later is not ignored by an instance built before it
    assert get_connector("github") is not plain


def _profile(source, ext, name):
    return NormalizedProfile(source=source, source_type="x", external_id=ext,
                             url=f"https://{source}/{ext}", raw={}, name=name)


def test_phase_b_profiles_are_fetched_while_phase_a_is_still_working(session, monkeypatch):
    import rip.nlq as nlq

    log = {}

    def openalex_search(query, limit):
        time.sleep(0.6)                      # a slow scholarly source
        log["openalex_done"] = time.monotonic()
        return [{"source": "openalex", "external_id": "A1", "name": "Ada Lovelace"}]

    def orcid_search(query, limit):
        return [{"source": "orcid", "external_id": "0000-0001", "name": "Ada Lovelace"}]

    class Fake:
        def __init__(self, source):
            self.source = source

        def fetch(self, identifier):
            log.setdefault(f"{self.source}_fetch", time.monotonic())
            return _profile(self.source, identifier, "Ada Lovelace")

    monkeypatch.setattr("rip.connectors.get_connector", lambda s: Fake(s))
    monkeypatch.setattr(nlq, "SUGGESTION_SEARCHERS", (
        ("openalex", openalex_search, True), ("orcid", orcid_search, False),
    ))
    parsed = nlq.parse(session, "Ada Lovelace")
    out = nlq.discovery_suggestions(session, parsed, allow_paid=False)

    assert {i["source"] for i in out if i.get("stored")} == {"openalex", "orcid"}
    # ORCID's profile was pulled before the slow OpenAlex search even returned
    assert log["orcid_fetch"] < log["openalex_done"]


def test_fetches_not_started_within_the_budget_are_skipped_and_reported(session, monkeypatch):
    import rip.nlq as nlq

    def orcid_search(query, limit):
        return [{"source": "orcid", "external_id": "0000-0002", "name": "Ada Lovelace"}]

    class Fake:
        def fetch(self, identifier):
            raise AssertionError("must not fetch once the budget is spent")

    monkeypatch.setattr("rip.connectors.get_connector", lambda s: Fake())
    monkeypatch.setattr(nlq, "SUGGESTION_SEARCHERS", (("orcid", orcid_search, False),))
    monkeypatch.setattr(nlq, "LIVE_BUDGET_SECONDS", -1.0)
    out = nlq.discovery_suggestions(session, nlq.parse(session, "Ada Lovelace"), allow_paid=False)
    assert out and out[0]["stored"] is False
    assert "TimeoutError" in out[0]["store_error"]


def test_results_are_stored_in_source_order_regardless_of_fetch_timing(session, monkeypatch):
    import rip.nlq as nlq

    def search_for(source):
        return lambda q, limit: [{"source": source, "external_id": f"{source}-1", "name": "X"}]

    class Fake:
        def __init__(self, source):
            self.source = source

        def fetch(self, identifier):
            # the first declared source is the slowest to fetch
            time.sleep({"orcid": 0.3, "wikidata": 0.0, "huggingface": 0.1}[self.source])
            return _profile(self.source, identifier, f"Ada {self.source.title()}")

    order = []
    real_store = nlq._store_results

    def spy(session_, raw_items, fetched):
        order.extend(item["source"] for item, _p, _e in fetched)
        return real_store(session_, raw_items, fetched)

    monkeypatch.setattr("rip.connectors.get_connector", lambda s: Fake(s))
    monkeypatch.setattr(nlq, "_store_results", spy)
    monkeypatch.setattr(nlq, "SUGGESTION_SEARCHERS", tuple(
        (s, search_for(s), False) for s in ("orcid", "wikidata", "huggingface")))
    nlq.discovery_suggestions(session, nlq.parse(session, "Ada Lovelace"), allow_paid=False)
    assert order == ["orcid", "wikidata", "huggingface"]


def test_a_long_backoff_a_source_asks_for_is_capped(session):
    """A burst of parallel probes had OpenAlex answer 429 with a reset at
    midnight. Honouring that verbatim took the graph's main scholarly source
    out for thirteen hours over a limit that clears in a second."""
    from datetime import datetime, timezone

    from rip.models import SourceThrottle
    from rip.nlq import MAX_THROTTLE_SECONDS, _note_source_throttled, _source_throttled

    _note_source_throttled(session, "openalex", seconds=47125.0)
    row = session.query(SourceThrottle).filter_by(source="openalex").one()
    waiting = (row.blocked_until - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
    assert waiting <= MAX_THROTTLE_SECONDS + 5
    assert _source_throttled(session, "openalex")


def test_a_candidate_named_after_the_subject_is_not_a_person(monkeypatch):
    """Scholarly indexes hold records named after subjects: searching
    "hypertension arterial stiffness researchers" stored an author called
    "ARTERIAl STIffnESS". The old check compared the whole name with single
    query words, so any two-word subject walked through it."""
    from rip.normalize import NormalizedProfile

    from rip import nlq

    names = {"S1": "ARTERIAl STIffnESS", "S2": "Kate Tilling"}

    class Fake:
        def fetch(self, external_id):
            return NormalizedProfile(source="semanticscholar", source_type="scholarly",
                                     external_id=external_id, url="u", raw={},
                                     name=names[external_id])

    monkeypatch.setattr("rip.connectors.get_connector", lambda s: Fake())
    term = "hypertension arterial stiffness researchers"
    items = [{"source": "semanticscholar", "external_id": ext, "_term": term}
             for ext in names]
    results = {item["external_id"]: (profile, exc)
               for item, profile, exc in nlq._fetch_profiles(items)}
    assert results["S1"][0] is None and "named after the search term" in str(results["S1"][1])
    assert results["S2"][0] is not None and results["S2"][1] is None
