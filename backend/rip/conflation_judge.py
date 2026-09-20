"""Ask a model whether one person could have written all of this.

The detector in rip.conflation finds records whose papers fall into groups
sharing neither topics nor co-authors. Measured against seventeen records read
by hand it is right half the time — and three attempts to sharpen it with the
data already present (group coverage, topic-graph distance, overlapping active
years) each made it worse or did nothing.

What separated the two classes, every time, was reading the titles and knowing
the world: petroleum waterflood and dengue entomology are not one career;
arterial stiffness and COPD pharmacy are. That is the judgement this asks for.

It is deliberately a SECOND STAGE. rip.conflation stays deterministic, free and
offline, and decides what is worth asking about; this runs over its output,
needs a key and the network, and costs money. Nothing in the request path calls
it — it is for the batch that fills a review queue.

The verdict is still not a verdict. It goes to a person, like everything else
here, and the model is asked to say "unsure" rather than guess, because a wrong
"several people" sends somebody hunting a split that does not exist.

    ANTHROPIC_API_KEY=... python scripts/judge_conflated.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass

MODEL = "claude-opus-5"
# A verdict and a sentence. Thinking is adaptive and billed separately from
# this, so it does not need to be large.
MAX_TOKENS = 4000
TITLES_PER_GROUP = 8

VERDICTS = ("one_person", "several_people", "unsure")

SYSTEM = """\
You are looking at the publication record filed under one name in a research \
database, split into groups of papers that share no topics and no co-authors \
with each other.

Sources get author disambiguation wrong, so a record like this is sometimes \
several different researchers filed under one name — and sometimes one person \
with a wide career, a change of field, or a methodological speciality that \
crosses domains.

Decide which. Reasons a record IS one person: a statistician or bioinformatician \
whose methods apply everywhere; a clinician who also teaches or writes policy; a \
career that moved fields once; an interdisciplinary programme. Reasons it is \
SEVERAL: bodies of work needing training that nobody holds at once, or that no \
single career path connects.

Say unsure when it could honestly go either way. A wrong "several_people" sends \
somebody hunting a split that is not there, and a wrong "one_person" leaves a \
corrupted record in place. Neither is free, so do not guess."""

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "reason": {
            "type": "string",
            "description": "One sentence, naming the work that decided it.",
        },
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


@dataclass
class Verdict:
    person_id: str
    name: str | None
    verdict: str
    reason: str

    @property
    def conflated(self) -> bool:
        return self.verdict == "several_people"


def _question(name: str | None, groups: list[dict]) -> str:
    """The record as a reader sees it. No score, and no hint of what the
    detector suspected: told that something looks wrong, a model agrees."""
    lines = [f"Name on the record: {name or 'unknown'}", ""]
    for i, group in enumerate(groups, 1):
        lines.append(f"Group {i} — {group['papers']} papers, {group['years']}")
        if group.get("topics"):
            lines.append(f"  subjects: {', '.join(group['topics'])}")
        for title in group["titles"][:TITLES_PER_GROUP]:
            lines.append(f"  - {(title or '').strip()[:160]}")
        lines.append("")
    lines.append("Is this one researcher, or more than one?")
    return "\n".join(lines)


def judge(client, person_id: str, name: str | None, groups: list[dict]) -> Verdict:
    """One record, one verdict. Raises whatever the SDK raises."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": _question(name, groups)}],
    )
    # A declined request returns 200 with no usable content; treat it as an
    # absence of judgement rather than reading content that is not there.
    if getattr(response, "stop_reason", None) == "refusal":
        return Verdict(person_id, name, "unsure", "the model declined to answer")
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = json.loads(text)
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        return Verdict(person_id, name, "unsure", f"unrecognised verdict {verdict!r}")
    return Verdict(person_id, name, verdict, data.get("reason", ""))


def client_or_reason():
    """A client, or why there is not one — so a caller can explain itself
    instead of dying on an import."""
    try:
        import anthropic
    except ImportError:
        return None, ("the anthropic package is not installed "
                      "(pip install anthropic)")
    try:
        return anthropic.Anthropic(), None
    except Exception as exc:                 # missing or unreadable credentials
        return None, f"no usable credentials: {exc}"
