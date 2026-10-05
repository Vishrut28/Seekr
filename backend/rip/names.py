"""The same person's name written differently: nicknames and spelling variants.

Two relations, used differently on purpose:

- **Transliterations** are one name romanised more than one way: Agarwal /
  Aggarwal / Agrawal, Mohammed / Muhammad / Mohamed, Chaudhary / Choudhury.
  Search treats them as the same word, and so does entity resolution.

- **Look-alikes** are different names that read alike: Katherine / Kathryn,
  Stephen / Steven. Search accepts them; resolution does not, because a
  Katherine and a Kathryn at one institute are two people as often as one.

- **Nicknames** are a different name for the same person: Bill for William,
  Bob for Robert. Search expands them ("Bill Gates" finds William H. Gates),
  but resolution does not: "Sam" is Samuel or Samantha, "Alex" is Alexander
  or Alexandra, and a nickname alone is too weak to merge two records on.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .textnorm import fold

# One name, romanised more than one way. The same person writes it either way
# across sources, so resolution treats these as one name.
_TRANSLITERATIONS = [
    {"mohammad", "mohammed", "muhammad", "mohamed", "mohamad", "muhammed", "mohd"},
    {"ahmed", "ahmad"}, {"hussain", "hussein", "husain"}, {"yusuf", "yousef", "youssef", "yousuf"},
    {"abdul", "abdel"}, {"aggarwal", "agarwal", "agrawal", "agarwala"},
    {"srivastava", "shrivastava", "srivastav"},
    {"chaudhary", "choudhary", "chowdhury", "chaudhari", "choudhury", "chaudhuri"},
    {"mukherjee", "mukerjee", "mukherji"}, {"banerjee", "banerji"}, {"chatterjee", "chatterji"},
    {"bhattacharya", "bhattacharyya", "bhattacharjee"}, {"reddy", "reddi"},
    {"pillai", "pillay"}, {"lakshmi", "laxmi"}, {"vijay", "vijai"},
    {"sanjay", "sanjai"}, {"ajay", "ajai"}, {"deepak", "dipak"}, {"pradeep", "pradip"},
    {"sandeep", "sandip"}, {"kuldeep", "kuldip"}, {"rajeev", "rajiv"}, {"sanjeev", "sanjiv"},
    {"abhijit", "abhijeet"}, {"shrinivas", "srinivas", "sreenivas"},
    {"sergey", "sergei"}, {"aleksandr", "aleksander", "alexandr"},
    {"dmitry", "dmitri", "dmitriy"}, {"yuri", "yury", "iurii"}, {"andrey", "andrei"},
    {"mikhail", "michail"},
]
# Different names that sound or look alike. Someone searching "Katherine" may
# want a Kathryn, so search accepts them — but a Katherine and a Kathryn at the
# same institute are two people as often as one, so resolution does not.
_LOOKALIKES = [
    {"catherine", "katherine", "kathryn", "katharine", "kathrine"}, {"stephen", "steven"},
    {"sara", "sarah"}, {"hannah", "hanna"}, {"phillip", "philip"}, {"jeffrey", "geoffrey"},
    {"nicolas", "nicholas", "nikolas"}, {"verma", "varma"},
    {"alexander", "aleksandr", "aleksander", "alexandr"},
]
_NICKNAMES = {
    "william": {"bill", "will", "billy", "liam"}, "robert": {"bob", "rob", "bobby", "robbie"},
    "richard": {"rick", "rich", "richie", "dick"}, "michael": {"mike", "mikey"},
    "james": {"jim", "jimmy", "jamie"}, "john": {"jack", "johnny"}, "joseph": {"joe", "joey"},
    "thomas": {"tom", "tommy"}, "charles": {"charlie", "chuck"}, "christopher": {"chris"},
    "daniel": {"dan", "danny"}, "david": {"dave"}, "edward": {"ed", "eddie", "ted"},
    "elizabeth": {"liz", "beth", "lizzie", "eliza"}, "jennifer": {"jen", "jenny"},
    "katherine": {"kate", "katie", "kathy", "kat"}, "margaret": {"maggie", "meg", "peggy"},
    "matthew": {"matt"}, "nicholas": {"nick"}, "patrick": {"pat"}, "peter": {"pete"},
    "samuel": {"sam"}, "samantha": {"sam"}, "stephen": {"steve"}, "susan": {"sue", "susie"},
    "anthony": {"tony"}, "andrew": {"andy", "drew"}, "alexander": {"alex", "sasha"},
    "alexandra": {"alex", "sasha"}, "benjamin": {"ben"}, "gregory": {"greg"},
    "jonathan": {"jon"}, "joshua": {"josh"}, "kenneth": {"ken", "kenny"},
    "lawrence": {"larry"}, "ronald": {"ron"}, "timothy": {"tim"}, "victoria": {"vicky"},
    "zachary": {"zach"}, "nathaniel": {"nate", "nathan"}, "abhishek": {"abhi"},
    "venkatesh": {"venky"}, "krishnamurthy": {"krish"},
}


def _index(groups) -> dict[str, frozenset[str]]:
    out: dict[str, set[str]] = {}
    for group in groups:
        for word in group:
            out.setdefault(word, set()).update(group)
    return {k: frozenset(v) for k, v in out.items()}


_SPELLING_OF = _index(_TRANSLITERATIONS)
_NICK_GROUPS = []
for full, nicks in _NICKNAMES.items():
    spellings = _SPELLING_OF.get(full, frozenset({full}))
    _NICK_GROUPS.append(set(spellings) | nicks)
_EVERY_FORM_OF = _index([*_TRANSLITERATIONS, *_LOOKALIKES, *_NICK_GROUPS])


@lru_cache(maxsize=65536)
def spelling_key(word: str) -> str:
    """One key per transliteration group: agarwal, aggarwal and agrawal share
    one. Look-alikes and nicknames are NOT folded (see the module docstring)."""
    w = fold(word)
    group = _SPELLING_OF.get(w)
    return min(group) if group else w


def search_forms(word: str) -> frozenset[str]:
    """Every way to write WORD that a search should accept: its spellings and
    its nicknames, or the full names a nickname stands for."""
    w = fold(word)
    return _EVERY_FORM_OF.get(w, frozenset({w}))


def search_key(word: str) -> str:
    """One key per word for comparing names in search: spellings and
    nicknames together ("bill" and "william" compare equal)."""
    return min(search_forms(word))


def name_phrase_forms(phrase: str, limit: int = 12) -> list[str]:
    """PHRASE with each word in each of its search forms, the typed form first."""
    parts = fold(phrase).split()
    forms = [""]
    for part in parts:
        options = [part] + sorted(search_forms(part) - {part})
        forms = [f"{f} {o}".strip() for f in forms for o in options][:limit]
    return forms


def _name_words(text: str | None) -> list[str]:
    return [w for w in re.split(r"[^\w]+", fold(text or "")) if w]


def carries_name(wanted: str, names, handles=()) -> bool:
    """Is someone called one of NAMES (or with one of HANDLES) the person a
    search for the name WANTED asks about?

    Every word of WANTED must be a word of one name, in any of its search
    forms; case, accents and word order do not matter. A search for "Sundar
    Pichai" kept people called Michael Bauer and JACKSPARROWbts, because a
    full-text source matched the words somewhere in their papers or profile.
    A handle counts when it is the name run together ("sundarpichai").
    """
    want = _name_words(wanted)
    if not want:
        return True
    keys = [search_key(w) for w in want]
    for name in names:
        have = {search_key(w) for w in _name_words(name)}
        if have and all(k in have for k in keys):
            return True
    joined = "".join(want)
    return any("".join(_name_words(h)) == joined for h in handles if h)


def _word_fits(a: str, b: str) -> bool:
    """One word of a name standing for another: the same word in any search
    form, an initial, or a shortening ("Sundar" for "Sundararajan")."""
    if a == b or search_key(a) == search_key(b):
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) == 1:
        return long_.startswith(short)
    return len(short) >= 3 and long_.startswith(short)


def alias_fits(name: str | None, alias: str | None) -> bool:
    """Could ALIAS be NAME written another way, rather than somebody else's?

    Sources list alternative names that belong to other people: OpenAlex
    filed "Aman Sharma" under Poonam Sharma and Kusum Sharma, so a search for
    Aman Sharma answered with both. Initials, reordering, nicknames, a
    shortening and an added middle name all fit. Two names that each have a
    word the other cannot account for -- Aman against Poonam -- are two
    people; that also turns away a changed surname, which the canonical name
    still finds. A name in another script is not judged.
    """
    n = [w for w in _name_words(name) if not w.isdigit()]
    a = [w for w in _name_words(alias) if not w.isdigit()]
    if not n or not a or any(ch.isalpha() and ord(ch) > 0x24F for ch in "".join(a)):
        return True
    alias_left = [w for w in a if not any(_word_fits(w, x) for x in n)]
    name_left = [w for w in n if not any(_word_fits(w, x) for x in a)]
    return not (alias_left and name_left)
