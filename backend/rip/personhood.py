"""Is this record a person — and if so, what is their name?

Sources hand us names that are not names. On the real graph:

- web pages give their <title>: "Dhruv Dixit's Profile | YMGrad", "Rahul Jain
  Portfolio", "My Fieldwire Journey: Rahul Joshi". Real people, messy names —
  these are CLEANED, never dropped.
- web pages that are not about a person at all: job posts ("Senior Software
  Engineer - C/C++ Networking … | Pro"), listicles ("20+ Deep Learning Projects
  for Beginners"), course pages, "Careers at Millennium", "GCSAYN Annual Forum".
- GitHub accounts of type User that are communities or lists: "Deep Learning
  Türkiye", "Awesome machine learning deep learning libraries".
- degenerate scholarly author strings: "D. .", "p", "O. B. O. C. C. Group".

Stored as people, they pollute every search that touches names: "deep learning
researchers" became a NAME filter because "Deep Learning Türkiye" exists.

The rules are deliberately asymmetric. Rejecting a real person silently loses
them, while keeping a bad record is visible and purgeable, so anything short of
a clear signal is kept. The clear signals: a known topic phrase or entity word
inside the name, nothing that reads as a name at all, or (web pages only,
where the name is a page title) a name made mostly of ordinary words.

SCRIPT. Those signals are word lists, and a word list needs words. Chinese,
Japanese and Korean do not space them, so "第一次机器学习回归函数" ("first
machine learning regression function") is ONE token: it passed the "1-5 words"
test, matched no English topic phrase, and became a person in the graph --
while "Deep Learning Tutorial", the same kind of string in English, was
correctly refused. CJK now has its own signals below, in the kinds that script
offers: length, and characters that do grammatical work.

Every other script is still unjudged, and says so rather than passing quietly.
"Машинное обучение" is Russian for "machine learning" and nothing here can
tell. A Verdict carries `judged=False` in that case; it is still accepted,
because the asymmetry above has not changed and refusing every name this file
cannot read would lose most of the world. scripts/unjudged_names.py lists
them, so the gap is countable instead of invisible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .geo import COUNTRIES, DEMONYMS, PLACES
from .textnorm import fold

# Multi-word subjects. A person is not called "Machine Learning Anything".
TOPIC_PHRASES = {
    "machine learning", "deep learning", "data science", "data scientist",
    "artificial intelligence", "computer vision", "natural language",
    "language processing", "neural network", "neural networks", "reinforcement learning",
    "software engineering", "software engineer", "software development", "web development",
    "full stack", "open source", "big data", "cloud computing", "cyber security",
    "information security", "data engineering", "data analytics", "business intelligence",
    "user experience", "product design", "generative ai", "large language",
    "language models", "internet of things", "quantum computing", "computer science",
}

# Words that name a kind of thing, not a person, and are not surnames.
ENTITY_WORDS = {
    "libraries", "library", "projects", "resources", "tutorials", "tutorial", "courses",
    "course", "classes", "careers", "jobs", "hiring", "vacancy", "vacancies", "internship",
    "bootcamp", "academy", "institute", "university", "college", "school", "conference",
    "summit", "forum", "meetup", "symposium", "workshop", "webinar", "annual", "awesome",
    "cheatsheet", "roadmap", "guide", "handbook", "software", "platform", "solutions",
    "services", "technologies", "pvt", "ltd", "inc", "llc", "gmbh", "foundation",
    "association", "society", "consortium", "committee", "department", "journal",
    "proceedings", "newsletter", "podcast", "magazine",
    # Publishers. OpenAlex credits some of them as AUTHORS: "Verlag Hans
    # Huber", a Swiss medical publisher, arrived with 73 works and scored 0.40
    # on the conflation detector, which is what a publisher's catalogue looks
    # like from the inside. None of these is a surname anybody bears.
    #
    # "press" is deliberately NOT here. It is a real surname — William H.
    # Press wrote Numerical Recipes — and this file rejects nothing on a
    # signal that could cost a real person their record. The publishers that
    # carry it are caught anyway: "Oxford University Press" by "university",
    # "MIT Press Ltd" by "ltd".
    # Only the forms something in the corpus or a real publisher's name
    # actually uses. Singular "publisher" and "imprint" were tried and
    # dropped: nothing carries them, and every word here is a way to lose a
    # real person, so an untested one is a liability rather than cover.
    "verlag", "publishers", "publishing", "publications",
}

# Scripts that do not space their words: CJK ideographs, kana, and hangul.
CJK_RANGES = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af"
_CJK = re.compile(f"[{CJK_RANGES}]")

# Personal names in these scripts are short: Chinese is a 1-2 character
# surname and a 1-2 character given name, Japanese and Korean run to about
# six. A longer unbroken run is a phrase -- the same judgement the Latin path
# makes with "1-5 words", in the unit this script actually has.
MAX_CJK_NAME = 7

# Characters doing grammatical work, which join words into a phrase and do not
# appear in a personal name: 的 is the attributive particle ("奔跑的小刺猬",
# "running little hedgehog" -- a handle that reached the graph as a person),
# 第 forms ordinals ("第一次", "the first time"), 了 marks aspect, and 吗 呢 吧
# end questions. Kept to the ones no name bears: 和 and 有 were considered and
# left out because Japanese given names use them (和也, 有里).
CJK_FUNCTION_CHARS = "的第了吗呢吧"

# The CJK half of TOPIC_PHRASES and ENTITY_WORDS. Matched as substrings
# because there are no word boundaries to match on; each is a compound of two
# or more characters, so none of them lands inside a personal name.
CJK_ENTITY_WORDS = (
    # subjects
    "机器学习", "深度学习", "神经网络", "人工智能", "数据科学", "计算机视觉",
    "自然语言", "强化学习", "数据分析", "软件工程", "网络安全",
    # kinds of document
    "教程", "笔记", "入门", "指南", "手册", "文档", "总结", "实战", "简介",
    # kinds of organisation
    "大学", "学院", "研究所", "出版社", "有限公司", "公司", "协会", "委员会",
)


def cjk_signal(name: str) -> str | None:
    """The clear non-person signal in a CJK name, if any."""
    text = "".join(str(name or "").split())
    if not _CJK.search(text):
        return None
    for word in CJK_ENTITY_WORDS:
        if word in text:
            return f"contains '{word}'"
    for char in text:
        if char in CJK_FUNCTION_CHARS:
            return f"contains the grammatical character '{char}'"
    if len(_CJK.findall(text)) > MAX_CJK_NAME:
        return f"{len(_CJK.findall(text))} characters unbroken: a phrase, not a name"
    return None


def unjudged(name: str) -> bool:
    """True when nothing in this file can speak to NAME at all.

    The signals are Latin word lists plus the CJK rules above. A name written
    only in Cyrillic, Arabic, Hebrew, Thai or Devanagari matches none of them
    and is accepted for want of any reason to refuse it -- which is a gap, not
    a judgement, and worth being able to count.
    """
    text = str(name or "")
    if not text.strip():
        return False
    if _CJK.search(text):
        return False
    return not re.search(r"[A-Za-z]", text)


# Ordinary words. A page title made mostly of these is about something, not
# someone. Used only for web pages, where the name IS a page title — never for
# a source that states a name, because real names collide with ordinary words.
COMMON_WORDS = {
    "a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to",
    "with", "near", "your", "my", "our", "how", "what", "why", "best", "top", "new",
    "make", "build", "learn", "online", "free", "tutor", "training", "developer",
    "developers", "engineer", "engineers", "expert", "experts", "professional",
    "senior", "junior", "lead", "manager", "consultant", "language", "languages",
    "programming", "python", "java", "javascript", "networking", "security",
    "data", "learning", "machine", "deep", "science", "research", "design",
    "portfolio", "profile", "resume", "cv", "pdf", "blog", "home", "page", "website",
    "welcome", "about", "official", "team", "group", "company", "careers", "career",
    "journey", "story", "stories", "projects", "beginners", "boost", "source", "code",
    "tech", "park", "ats", "automated", "candidate", "screening", "recruiting",
    "agencies", "structural", "engineering", "forensic", "electronics", "things",
}

# Title decoration around a name: "X's Portfolio", "X Portfolio", "X - Home".
_DECOR = re.compile(
    r"(?:['’]s)?\s*\b(?:personal\s+)?(?:portfolio|profile|resume|résumé|cv|homepage|"
    r"home\s*page|website|web\s*site|site|blog|page|home)\b(?:\s+\w+)?\s*$",
    re.IGNORECASE,
)
_SEGMENTS = re.compile(r"\s+[|•·–—-]\s+|\s*[|•·]\s*|:\s+")
_LEADING = re.compile(r"^(?:about|meet|hi,?\s+i['’]?m|i\s+am|welcome\s+to|this\s+is)\s+",
                      re.IGNORECASE)
_DEGREES = re.compile(r"\b(?:ph\.?\s?d|m\.?\s?sc|b\.?\s?tech|m\.?\s?tech|dr|prof)\b\.?", re.IGNORECASE)
_POSSESSIVE = re.compile(r"['’]s$")
_DOMAIN = re.compile(r"\w\.(?:com|org|net|io|ai|app|dev|me|co|in|uk|de|tech|site)\b", re.IGNORECASE)


@dataclass
class Verdict:
    is_person: bool
    name: str | None       # the cleaned name to store (None when rejected)
    reason: str = ""
    # other spellings found inside the name as given ("Rahul M Mulajkar,Rahul
    # Mukundrao Mulajkar,RMM" is one person written three ways)
    aliases: tuple = ()
    # False when no rule here can speak to this name's script at all, so
    # is_person is the default rather than a finding. Last, because callers
    # pass aliases positionally. See unjudged().
    judged: bool = True


def _alpha_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[^\W\d_]+", fold(text)) if w]


def _is_place(word: str) -> bool:
    return word in COUNTRIES or word in DEMONYMS or word in PLACES


def has_entity_signal(name: str) -> str | None:
    """The clear non-person signal in NAME, if any."""
    cjk = cjk_signal(name)
    if cjk:
        return cjk
    low = " ".join(_alpha_words(name))
    padded = f" {low} "
    for phrase in TOPIC_PHRASES:
        if f" {phrase} " in padded:
            return f"contains the topic '{phrase}'"
    for word in low.split():
        if word in ENTITY_WORDS:
            return f"contains '{word}'"
    if re.match(r"^\s*(?:top\s+)?\d+\+?\s", name or "", re.IGNORECASE):
        return "reads as a listicle"
    return None


def _reads_as_name(segment: str) -> bool:
    """1–5 words, at least one real word, no digits, not mostly ordinary words."""
    tokens = segment.split()
    if not 1 <= len(tokens) <= 5 or any(ch.isdigit() for ch in segment):
        return False
    if _DOMAIN.search(segment):
        return False                     # "UrbanPro.com" is the site, not a person
    words = _alpha_words(segment)
    real = [w for w in words if len(w) >= 2]
    if not real:
        return False
    # an ALL-CAPS token of 3+ letters is an acronym ("ATS", "PDF") unless the
    # whole name is written in capitals
    if not segment.isupper() and any(t.isupper() and len(t.strip(".,")) >= 3 for t in tokens):
        return False
    common = sum(1 for w in real if w in COMMON_WORDS or _is_place(w))
    return common / len(real) < 0.5


def _strip(segment: str) -> str:
    s = segment.strip()
    # everything from the first bracket on is commentary, not name:
    # "SAHAYA MERCY A PhD (Full Time Research Scholar) St. Joseph's College"
    s = re.sub(r"\(.*$", " ", s).replace(")", " ")
    s = _LEADING.sub("", s)
    for _ in range(2):
        s = _DECOR.sub("", s).strip(" -|:,")
    s = _DEGREES.sub(" ", s)
    s = _POSSESSIVE.sub("", s.strip())
    return " ".join(s.split())


def assess(name: str | None, source: str | None = None) -> Verdict:
    """Decide whether NAME is a person and return the name to store."""
    if name is None or not str(name).strip():
        return Verdict(True, name, "no name given")    # nothing to judge
    raw = " ".join(str(name).split())
    is_page_title = (source or "") == "web"

    signal = has_entity_signal(raw)
    segments = [seg for seg in (_strip(s) for s in _SEGMENTS.split(raw)) if seg]
    namelike = [seg for seg in segments if _reads_as_name(seg) and not has_entity_signal(seg)]

    if is_page_title:
        # A page title: find the part that names someone, else it is not a person.
        if signal:
            # The title is about a subject ("20 Machine Learning Projects … |
            # Udacity"); only a full two-word name can rescue it — a lone word
            # left over is the site's brand, not someone's name.
            namelike = [seg for seg in namelike if len(_alpha_words(seg)) >= 2]
        if namelike:
            return Verdict(True, _tidy(namelike[0]), "cleaned page title")
        return Verdict(False, None, signal or "page title names no one")

    if signal and not namelike:
        return Verdict(False, None, signal)
    real = [w for w in _alpha_words(raw) if len(w) >= 2]
    if not real:
        return Verdict(False, None, "no word in the name")
    if signal:
        # "Rahul Gupta - Humanitarian • Social Worker" keeps the name part
        return Verdict(True, _tidy(namelike[0]), "kept the name part")
    if len(segments) > 1 and namelike and namelike[0] != raw:
        return Verdict(True, _tidy(namelike[0]), "kept the name part")
    cleaned = _strip(raw)
    name, aliases, why = _unpack_commas(_tidy(cleaned) if cleaned else raw)
    if unjudged(raw):
        return Verdict(True, name, why or "no rule here reads this script",
                       tuple(aliases), judged=False)
    return Verdict(True, name, why, tuple(aliases))


# What may follow a name after a comma without being part of it.
_NAME_SUFFIXES = re.compile(
    r"^(?:ph\.?\s?d|m\.?\s?d|m\.?\s?sc|b\.?\s?tech|m\.?\s?tech|mba|jr|sr|ii|iii|iv|"
    r"frs|facs|fcps|mrcp|esq)\.?$", re.IGNORECASE)


def _unpack_commas(name: str) -> tuple[str, list[str], str]:
    """A name with commas in it: "Singh, Karan" is Karan Singh; "Dhruv Dixit,
    PhD" is Dhruv Dixit; "Rahul M Mulajkar,Rahul Mukundrao Mulajkar,RMM,Rahul
    Mulajkar" is one person listed three ways, stored whole as their name.
    Returns (name, other spellings, reason)."""
    stripped = name.strip(" ,;")
    if "," not in stripped:
        return stripped, [], "" if stripped == name else "stray comma removed"
    parts = [p.strip() for p in stripped.split(",") if p.strip()]
    parts = [p for p in parts if not _NAME_SUFFIXES.match(p)]
    if len(parts) == 1:
        return parts[0], [], "title after the name removed"
    # "Singh, Karan", "Smith, John A.", "Kim, Jae-Hyun": one surname, then the
    # given names — a bibliography's order, not a list
    if (len(parts) == 2 and len(parts[0].split()) == 1
            and 1 <= len(parts[1].split()) <= 3 and _reads_as_name(parts[1])):
        return f"{parts[1]} {parts[0]}", [], "surname-first name reordered"
    # "Dhruv Dixit, Bangalore": a name, then where they are
    from .geo import city_country, country_in_text

    if (len(_alpha_words(parts[0])) >= 2 and _reads_as_name(parts[0])
            and all(country_in_text(p) or city_country(p) for p in parts[1:])):
        return parts[0], [], "place after the name removed"
    full = [p for p in parts if len(_alpha_words(p)) >= 2 and _reads_as_name(p)]
    if len(full) >= 2:
        # the most complete spelling: whole words over initials
        best = max(full, key=lambda p: (sum(1 for w in _alpha_words(p) if len(w) > 1),
                                        len(_alpha_words(p)), -parts.index(p)))
        others = [p for p in parts if p != best and p.lower() != best.lower()]
        return best, others, "several spellings of one name"
    return stripped, [], ""


def _tidy(name: str) -> str:
    """"SAHAYA MERCY A" -> "Sahaya Mercy A"; everything else as written."""
    if name.isupper() and len(name) > 3:
        return " ".join(w.capitalize() if len(w) > 1 else w for w in name.split())
    return name


def is_person(name: str | None, source: str | None = None) -> bool:
    return assess(name, source).is_person


class NotAPerson(ValueError):
    """Raised at ingest for a record that is not a person."""
