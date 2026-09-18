"""The UI asks for a token only when the backend wants one.

/v1/auth sits behind the bearer middleware on purpose: with RIP_API_TOKEN set
the probe is refused, and the UI shows its sign-in gate; unset, it answers and
the UI goes straight to the app.
"""

from fastapi.testclient import TestClient

from rip import api


def test_an_open_backend_says_no_token_is_required(monkeypatch):
    monkeypatch.delenv("RIP_API_TOKEN", raising=False)
    assert TestClient(api.app).get("/v1/auth").json() == {"required": False}


def test_a_protected_backend_refuses_the_probe(monkeypatch):
    monkeypatch.setenv("RIP_API_TOKEN", "sekret")
    client = TestClient(api.app)
    assert client.get("/v1/auth").status_code == 401
    assert client.get("/v1/auth", headers={"Authorization": "Bearer sekret"}).json() == {
        "required": False}      # past the door, the question is moot
