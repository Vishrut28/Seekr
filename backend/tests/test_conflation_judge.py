"""The second-stage judgement, without a key or a network.

Everything here runs against a stub client. What is worth pinning down is not
the model's answer — that is the thing being bought — but the shape around it:
what gets asked, what is deliberately NOT asked, and that a strange reply comes
back as "unsure" rather than as a confident wrong answer.
"""

import json

import pytest

from rip import conflation_judge


class Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class Reply:
    def __init__(self, payload, stop_reason="end_turn"):
        self.content = [Block(payload)] if payload is not None else []
        self.stop_reason = stop_reason


class StubClient:
    """Records the request and returns whatever it was told to."""

    def __init__(self, reply):
        self._reply = reply
        self.sent = None

        outer = self

        class Messages:
            def create(self, **kwargs):
                outer.sent = kwargs
                return outer._reply

        self.messages = Messages()


GROUPS = [
    {"papers": 6, "years": "2011-2025", "topics": ["Congenital anomalies"],
     "titles": ["Sigmoid volvulus in Hirschsprung's disease"]},
    {"papers": 6, "years": "2019-2026", "topics": ["Markov chains"],
     "titles": ["Optimal mixing of the down-up walk"]},
]


def verdict_from(payload, stop_reason="end_turn"):
    client = StubClient(Reply(payload, stop_reason))
    return client, conflation_judge.judge(client, "pid", "Ann Double", GROUPS)


def test_a_verdict_comes_back_with_its_reason():
    _client, out = verdict_from(json.dumps(
        {"verdict": "several_people", "reason": "a surgeon and a combinatorialist"}))
    assert out.conflated is True
    assert out.reason == "a surgeon and a combinatorialist"


def test_one_person_is_not_reported_as_a_split():
    _client, out = verdict_from(json.dumps(
        {"verdict": "one_person", "reason": "a statistician's methods travel"}))
    assert out.conflated is False and out.verdict == "one_person"


def test_a_declined_request_is_unsure_not_a_split(capsys):
    """A refusal returns 200 with nothing usable. Reading content that is not
    there would either crash or, worse, read a stale block as a verdict."""
    _client, out = verdict_from(None, stop_reason="refusal")
    assert out.verdict == "unsure" and out.conflated is False


def test_a_verdict_outside_the_vocabulary_is_unsure():
    _client, out = verdict_from(json.dumps({"verdict": "probably", "reason": "..."}))
    assert out.verdict == "unsure"
    assert "probably" in out.reason


def test_the_question_carries_the_papers_and_hides_the_suspicion():
    """Told that a record looks wrong, a model agrees. The question shows the
    groups and says nothing about the detector or its score."""
    client, _out = verdict_from(json.dumps({"verdict": "unsure", "reason": "-"}))
    asked = client.sent["messages"][0]["content"]
    assert "Sigmoid volvulus" in asked and "down-up walk" in asked
    assert "Congenital anomalies" in asked and "2019-2026" in asked
    for leak in ("score", "conflat", "suspect", "flagged", "detector"):
        assert leak not in asked.lower(), leak


def test_it_asks_the_model_we_meant_to_ask_for_a_shape_we_can_read():
    client, _out = verdict_from(json.dumps({"verdict": "unsure", "reason": "-"}))
    assert client.sent["model"] == "claude-opus-5"
    fmt = client.sent["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert set(fmt["schema"]["required"]) == {"verdict", "reason"}
    assert fmt["schema"]["additionalProperties"] is False
    # both outcomes have to be reachable, or the answer is decided in advance
    assert set(fmt["schema"]["properties"]["verdict"]["enum"]) == {
        "one_person", "several_people", "unsure"}


def test_a_missing_package_or_key_explains_itself_rather_than_raising():
    client, why = conflation_judge.client_or_reason()
    assert (client is None) == (why is not None)
    if client is None:
        assert "anthropic" in why or "credentials" in why
