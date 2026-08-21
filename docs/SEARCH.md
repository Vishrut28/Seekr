# Search: what changed, and why it matters

Seekr should find people who match **what you typed** — a name, a skill, a
place — and say so when something is missing. That is what this work did.

---

## The problem

A search like **`Neha c++ bangalore`** or **`Andrew python california`** is
three things: who, what they know, where they are.

Seekr was not treating it that way.

| You typed | What actually happened |
|---|---|
| Name + skill + city | Anyone matching *any one* of the three words |
| `python` | Could be treated as a person’s **name** |
| `california` / `bangalore` | Could be tagged as a **name**, not a place |
| City written only in a bio | “Not found” — Seekr only looked at the empty location box |
| GitHub in the results | A label you could not click |
| Every search | Waited on the internet even when the answer was already here |

The list looked busy. It was not answering the question.

---

## What we changed

### 1. All three words together — then step back, honestly

`Andrew python california` now means Andrew **and** Python **and** California.

If nobody has all three, Seekr drops the last word and tries the first two,
then the first word. An orange line says which word was not found, for
example: *california was not found as a location — showing Andrew + python.*

**Why it matters:** You get the tightest real match, not 13 people who each
hit one word. You always know what was dropped.

### 2. A skill is a skill. A place is a place.

`python` and `c++` are skills, even if someone in the data is surnamed Python.
`Bangalore` and `California` are places, even if some record used that word in
a name.

**Why it matters:** `Neha c++ bangalore` is a person, a language, and a city —
not two names and a guess.

### 3. A city in the bio still counts

If GitHub left location blank but the person wrote “NMIT, Bangalore”, Seekr
finds them for Bangalore. The location column now shows the city instead of a
dash. (Bangalore and Bengaluru are treated as the same place.)

**Why it matters:** We were missing people who clearly said where they were.
We are not missing them now.

### 4. Profiles open in one click

GitHub, OpenAlex, and other sources in the table are real links — not dead
text.

**Why it matters:** You can check the person without digging through their
profile page.

### 5. Search is fast because it stopped waiting on the internet

Seekr used to call outside sites on almost every search. That is why it felt
slow.

Now **Search** uses what Seekr already knows. Press **Live** when you want it
to look on the internet. Live still runs by itself only when Seekr has
*nobody* for the question.

**Why it matters:** Everyday search is quick. Live is still there when you
need a wider net.

### 6. Likes and dislikes have a place to look

Thumbs up / down on a result are saved against that search. They do **not**
reorder the list (Seekr does not decide who to hire).

See them under **Review → Match votes**: person, query, good or bad, date.

The bookmark is separate — that saves someone to a **Shortlist**.

**Why it matters:** Feedback was invisible. Now you can see what you judged.

---

## Before and after

**Search:** `Neha c++ bangalore`

| | Before | After |
|---|---|---|
| Who shows up | Anyone matching any word | People matching all three, or the closest honest fallback |
| Bangalore in a bio | “Not found” | Found, shown as location |
| GitHub | Text | Click to open the profile |
| Speed | Slow every time | Fast when we already have them |
| If a city is missing | Unrelated people, no explanation | A clear note: this word was not found |

---

## The point

A long list of almost-right people is worse than a short list of the right
ones. If search “finds people” while answering a different question, you
cannot trust it.

This work makes the table match the question. When it cannot, it tells you —
instead of guessing.

---

## Try it

1. Open Seekr and refresh (Ctrl+F5).
2. Search **Neha c++ bangalore** — right person, Bangalore as place, GitHub
   as a link.
3. Search **Andrew python california** — all three if they exist; otherwise a
   short note, not a random mix of Andrews.
4. Press **Live** only when you want the internet.
5. Thumbs up or down a row, then open **Review → Match votes**.
