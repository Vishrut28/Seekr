"""A flood stop on /v1.

There was none. Anything that could reach the port could ask as fast as it
liked, and the expensive path is the one an attacker would pick: a query the
corpus cannot answer sends Seekr out to OpenAlex and Europe PMC, so a cheap
request in becomes several expensive ones out.

This is a sliding window per address, in process memory, and the tests pin
what that does and does not promise.
"""

import time

import pytest
from fastapi.testclient import TestClient

from rip import api

REMOTE = ("203.0.113.9", 40000)


@pytest.fixture(autouse=True)
def clean_buckets(monkeypatch):
    monkeypatch.delenv("RIP_API_TOKEN", raising=False)
    api._buckets.clear()
    yield
    api._buckets.clear()


def test_a_flood_from_one_address_is_stopped(monkeypatch):
    monkeypatch.setattr(api, "RATE_LIMIT", 5)
    client = TestClient(api.app, client=REMOTE)
    codes = [client.get("/v1/auth").status_code for _ in range(8)]
    assert codes[:5] == [200] * 5
    assert codes[5:] == [429] * 3
    assert client.get("/v1/auth").headers.get("Retry-After")


def test_this_machine_is_never_limited(monkeypatch):
    """The UI polls this API, and a developer running a script against their
    own machine is not the traffic being guarded against."""
    monkeypatch.setattr(api, "RATE_LIMIT", 2)
    client = TestClient(api.app)                    # loopback
    assert [client.get("/v1/auth").status_code for _ in range(10)] == [200] * 10


def test_one_address_flooding_does_not_shut_out_another(monkeypatch):
    monkeypatch.setattr(api, "RATE_LIMIT", 3)
    noisy = TestClient(api.app, client=("203.0.113.10", 1))
    quiet = TestClient(api.app, client=("203.0.113.11", 1))
    for _ in range(5):
        noisy.get("/v1/auth")
    assert noisy.get("/v1/auth").status_code == 429
    assert quiet.get("/v1/auth").status_code == 200


def test_the_window_slides_rather_than_resetting_on_the_minute(monkeypatch):
    """A counter cleared every 60s lets twice the limit through across the
    boundary: all of it at 59s, all of it again at 61s."""
    monkeypatch.setattr(api, "RATE_LIMIT", 3)
    monkeypatch.setattr(api, "RATE_WINDOW", 0.3)
    client = TestClient(api.app, client=REMOTE)
    assert [client.get("/v1/auth").status_code for _ in range(4)][-1] == 429
    time.sleep(0.35)
    assert client.get("/v1/auth").status_code == 200


def test_the_bucket_table_cannot_grow_without_bound(monkeypatch):
    """Otherwise a botnet is a memory leak as well as a flood."""
    monkeypatch.setattr(api, "RATE_MEMORY", 16)
    monkeypatch.setattr(api, "RATE_LIMIT", 100)
    for i in range(200):
        TestClient(api.app, client=(f"198.51.100.{i % 254}", 1)).get("/v1/auth")
    assert len(api._buckets) <= 16


def test_it_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(api, "RATE_LIMIT", 0)
    client = TestClient(api.app, client=REMOTE)
    assert [client.get("/v1/auth").status_code for _ in range(20)] == [200] * 20
