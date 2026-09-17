"""One way of turning text into comparable words, shared by indexing and search.

Both sides of a match have to agree exactly on what a "word" is, or the index
answers a different question than the parser asked. Everything that compares
text for search goes through fold() and words().

Worldwide text is the point. The previous tokenizer was `[a-zA-Z0-9]`: "São
Paulo" became "S", "o", "Paulo", "München" became "M", "nchen", and a query in
Chinese, Hindi or Arabic produced no words at all.

- fold():  case- and accent-insensitive — "Zürich" == "zurich", "José" == "jose".
  Only Latin/Greek/Cyrillic diacritics are removed; marks that are letters in
  their own script (Devanagari vowel signs, Japanese dakuten) are kept, and
  Hangul syllables are recomposed so Korean survives the round trip.
- words(): unicode-aware word split. "c++", "c#" and "f#" stay whole.
- Scripts written without spaces (Chinese, Japanese, Thai) are split into
  overlapping character pairs — the standard approach (Lucene's CJKBigram) that
  lets "机器学习" match inside "机器学习与计算机视觉" without a dictionary.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# letters NFKD does not decompose into base + accent
_EXTRA = str.maketrans({
    "ø": "o", "Ø": "o", "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ð": "d",
    "Ð": "d", "þ": "th", "Þ": "th", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe",
    "ı": "i", "ß": "ss", "’": "'", "‘": "'", "ʼ": "'",
})


def _is_diacritic(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x0300 <= cp <= 0x036F or 0x1AB0 <= cp <= 0x1AFF
        or 0x1DC0 <= cp <= 0x1DFF or 0x20D0 <= cp <= 0x20FF
        or 0xFE20 <= cp <= 0xFE2F
    )


@lru_cache(maxsize=65536)
def fold(text: str | None) -> str:
    """Lowercase, accent-free, compatibility-normalised text."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text.translate(_EXTRA))
    t = "".join(ch for ch in t if not _is_diacritic(ch))
    return unicodedata.normalize("NFC", t).casefold()


# a word: letters/digits, optionally followed by + or # ("c++", "c#")
_WORD = re.compile(r"[^\W_]+[+#]*")
# scripts that do not put spaces between words
_UNSPACED = re.compile(
    "[぀-ヿ㐀-䶿一-鿿豈-﫿฀-๿"
    "຀-໿က-႟ក-៿]+"
)


def is_unspaced(text: str | None) -> bool:
    """Is TEXT written in a script without spaces between words?"""
    return bool(text) and bool(_UNSPACED.search(text))


def _split_unspaced(token: str) -> list[str]:
    """"机器学习" -> ["机器", "器学", "学习"]; Latin runs stay whole."""
    if not _UNSPACED.search(token):
        return [token]
    out: list[str] = []
    pos = 0
    for m in _UNSPACED.finditer(token):
        if m.start() > pos:
            out.append(token[pos:m.start()])
        run = m.group()
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
        pos = m.end()
    if pos < len(token):
        out.append(token[pos:])
    return out


@lru_cache(maxsize=65536)
def _words_cached(text: str) -> tuple[str, ...]:
    out: list[str] = []
    for tok in _WORD.findall(fold(text)):
        out.extend(_split_unspaced(tok))
    return tuple(out)


def words(text: str | None) -> list[str]:
    """The folded words of TEXT, in order."""
    if not text:
        return []
    return list(_words_cached(text))


# a plural ending that a crude rule can undo without a dictionary
_PLURAL_KEEP = ("ss", "us", "is", "os")     # business, corpus, analysis, ios


def singular(word: str) -> str:
    """A crude, deliberately consistent singular.

    Applied to BOTH sides of a comparison — the query phrase and the stored
    value — so it only has to agree with itself, not with English. "physics"
    becoming "physic" costs nothing as long as the vocabulary's "Physics"
    becomes the same thing. Short words, non-ASCII words and anything with
    punctuation ("c++", "c#") are left exactly as they are.
    """
    if len(word) < 4 or not word.isascii() or not word.isalpha():
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"                  # studies -> study
    if word.endswith(("yses", "eses")) and len(word) > 5:
        return word[:-2] + "is"                 # analyses -> analysis, theses -> thesis
    if word.endswith(("sses", "xes", "ches", "shes", "zzes")):
        return word[:-2]                        # processes -> process, approaches -> approach
    if word.endswith(("che", "she", "xe")):
        # the singulars the rule above would split from their plurals:
        # "caches" loses "es", so "cache" loses its "e" to meet it
        return word[:-1]
    if word.endswith("s") and not word.endswith(_PLURAL_KEEP):
        # only the "s": diseases -> disease, databases -> database. Taking
        # "es" from every "-ses" word made "diseases" into "diseas", which
        # never met "disease"
        return word[:-1]
    return word


def stems(text: str | None) -> list[str]:
    """The folded, singularised words of TEXT, in order."""
    return [singular(w) for w in words(text)]


MAX_TERM_LEN = 128


def phrase_terms(text: str | None) -> list[str]:
    """Index terms that must ALL be present for TEXT to occur as a phrase.

    One word is its own term; a longer phrase is its chain of adjacent word
    pairs — the index stores every adjacent pair, so "new york city" is found
    as "new york" + "york city" without a positional index.
    """
    return _phrase(words(text))


def text_terms(text: str | None) -> set[str]:
    """Every index term for TEXT: its words and its adjacent word pairs."""
    return _terms(words(text))


def stem_phrase_terms(text: str | None) -> list[str]:
    """`phrase_terms` over stems, for a field where number is not identity."""
    return _phrase(stems(text))


def stem_text_terms(text: str | None) -> set[str]:
    """`text_terms` over stems, for a field where number is not identity."""
    return _terms(stems(text))


def _phrase(ws: list[str]) -> list[str]:
    if len(ws) <= 1:
        return [w for w in ws if len(w) <= MAX_TERM_LEN]
    pairs = [f"{a} {b}" for a, b in zip(ws, ws[1:])]
    return [p for p in dict.fromkeys(pairs) if len(p) <= MAX_TERM_LEN]


def _terms(ws: list[str]) -> set[str]:
    terms = {w for w in ws if len(w) <= MAX_TERM_LEN}
    terms.update(f"{a} {b}" for a, b in zip(ws, ws[1:]) if len(a) + len(b) < MAX_TERM_LEN)
    return terms


def contains_phrase(haystack_words: list[str], phrase_words: list[str]) -> bool:
    """Is PHRASE_WORDS a contiguous run inside HAYSTACK_WORDS?"""
    n = len(phrase_words)
    if not n:
        return False
    if n == 1:
        return phrase_words[0] in haystack_words
    first = phrase_words[0]
    for i in range(len(haystack_words) - n + 1):
        if haystack_words[i] == first and haystack_words[i:i + n] == phrase_words:
            return True
    return False


def value_key(text: str | None) -> str:
    """A whole value as one comparable key: "Machine  Learning (Applied)" and
    "machine learning applied" are the same key."""
    return " ".join(words(text))[:255]


ORG_SUFFIXES = {
    "inc", "llc", "ltd", "limited", "pvt", "private", "corp", "corporation",
    "gmbh", "co", "plc", "sa", "bv", "ag", "llp", "lp",
}


def org_key(name: str | None) -> str:
    """One key per company however it is spelled: "Deccan.AI" == "Deccan AI",
    "Technische Universität München" == "technische universitat munchen"."""
    ws = words(name)
    kept = [w for w in ws if w not in ORG_SUFFIXES]
    return " ".join(kept or ws)[:255]
