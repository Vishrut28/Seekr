"""Record the Seekr demo film.

Everything on screen is the real product against the real graph. Scene
boundaries are written to marks.json so the edit can zoom, and so the sound
can be cut to the picture.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import scenes  # noqa: E402
from capture import OUT, TOKEN, UI, Chrome  # noqa: E402
from scenes import (  # noqa: E402
    MARKS,
    caption,
    caption_off,
    caption_typed,
    card,
    click_cursor,
    click_live,
    goto_ui,
    mark,
    move_cursor,
    record,
    show_cursor,
    submit,
    type_query,
)

MARK = re.search(r'd="([^"]+)"',
                 (pathlib.Path(__file__).parents[1] / "frontend/assets/mark.svg").read_text()).group(1)
MARK_SVG = (f'<svg viewBox="0 0 512 512" width="620"><path d="{MARK}" '
            'fill="currentColor" fill-rule="evenodd"/></svg>')


def main():
    # Fail loudly rather than filming Chrome's error page: a recording that
    # captures "This site can't be reached" looks fine until you watch it.
    import urllib.request
    try:
        with urllib.request.urlopen(UI, timeout=10) as r:
            assert r.status == 200
    except Exception as exc:
        raise SystemExit(f"the app is not answering at {UI}: {exc}") from exc

    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    c = Chrome()
    try:
        c.send("Page.navigate", url=UI)
        time.sleep(2.5)
        c.js(f'localStorage.setItem("seekr_token","{TOKEN}");'
             'localStorage.setItem("seekr_theme","light");'
             'localStorage.removeItem("seekr_recent")')

        # ---- open ----
        mark("title", "card")
        card(c, "Seekr", "Public evidence about people, collected and kept current.",
             kicker="Deccan AI", seconds=1.6, size=76, mark_svg=MARK_SVG)
        mark("premise", "card")
        card(c, "Every claim traceable to its source.",
             sub="Seekr discovers public profiles, resolves them into one person, "
                 "and maintains them as the sources change.",
             kicker="What it is", seconds=1.6, size=42, mark_svg=MARK_SVG)

        # ---- ask, zoomed on the search field ----
        goto_ui(c)
        mark("ask", "zoom_search")
        caption_typed(c, "Ask", "Plain language. No query syntax.",
                      "Ask for people the way you would describe them.", per_char=0.014)
        record(c, 0.4)
        type_query(c, "machine learning researchers in India")
        record(c, 0.5)
        submit(c)
        record(c, 1.9)
        caption_off(c)
        record(c, 0.4)

        # ---- any field, not just software ----
        mark("breadth_card", "card")
        card(c, "The same question, in any field.",
             sub="The vocabulary comes from the corpus itself, so a question about "
                 "climate modelling parses exactly like one about compilers.",
             kicker="Breadth", seconds=1.6, size=40, mark_svg=MARK_SVG)
        goto_ui(c)
        mark("breadth_climate", "zoom_search")
        type_query(c, "climate model intercomparison researchers")
        click_live(c)
        record(c, 0.5)
        c.js("window.scrollBy({top:190, behavior:'smooth'})")
        record(c, 0.5)
        caption(c, "Breadth", "Climate science",
                "Veronika Eyring, Ronald Stouffer, Gerald Meehl, Karl Taylor — the "
                "people who lead the model intercomparison projects.")
        record(c, 1.9)
        caption_off(c)
        record(c, 0.3)
        mark("breadth_bio")
        type_query(c, "structural biologists working on protein folding")
        click_live(c)
        record(c, 0.5)
        c.js("window.scrollBy({top:190, behavior:'smooth'})")
        record(c, 0.5)
        caption(c, "Breadth", "Structural biology",
                "Martin Karplus, who shared the Nobel Prize for exactly this.")
        record(c, 1.9)
        caption_off(c)
        record(c, 0.4)

        print(f"[film] act one: {scenes.frame_no}")
        _act_two(c)
    finally:
        pathlib.Path(__file__).parent.joinpath("marks.json").write_text(
            json.dumps({"fps": scenes.FPS, "frames": scenes.frame_no,
                        "marks": MARKS, "stamps": c.stamps}, indent=1))
        c.close()


def _act_two(c: Chrome):
    """The live sources reporting for themselves, the dossier, and shortlists."""
    # ---- every source, reporting for itself ----
    mark("sources_card", "card")
    card(c, "Five sources, asked in turn.",
         sub="A live search is not one request. Each source reports what it was "
             "asked, what it found, and what was worth keeping.",
         kicker="Live", seconds=1.6, size=40, mark_svg=MARK_SVG)
    goto_ui(c)
    mark("source_cards", "zoom_search")
    type_query(c, "rust developers in berlin")
    click_live(c)
    record(c, 1.2)
    caption(c, "Live", "Each source answers for itself",
            "Searching, found, kept — or skipped, and why.")
    record(c, 7.0)
    caption_off(c)
    record(c, 0.5)

    # ---- provenance, briefly ----
    # A well-evidenced record, so the dossier that follows shows what the
    # report looks like when the sources have plenty to say. A GitHub-only
    # profile scores low and honestly, but it is not what to lead with.
    mark("person")
    goto_ui(c, settle=1.4)
    type_query(c, "geoffrey hinton", per_char=0.02)
    submit(c)
    time.sleep(2.6)
    # Wait for the row to exist, then for the route to change. Sleeping a
    # fixed interval and hoping produced a dossier for the person "search".
    for _ in range(30):
        if c.js("!!document.querySelector('table tbody tr')"):
            break
        time.sleep(0.3)
    c.js("document.querySelector('table tbody tr').click()")
    person_id = None
    for _ in range(30):
        h = c.js("location.hash") or ""
        if "/person/" in h:
            person_id = h.rsplit("/", 1)[-1]
            break
        time.sleep(0.3)
    if not person_id:
        raise SystemExit("the person page never opened; the film would be wrong")
    time.sleep(1.6)
    caption(c, "Provenance", "Every claim points back at a source",
            "Identifiers, affiliations and links, reconciled into one record.")
    record(c, 2.0)
    c.js("window.scrollBy({top:560, behavior:'smooth'})")
    record(c, 1.2)
    caption_off(c)
    record(c, 0.3)

    # ---- the dossier ----
    mark("dossier_card", "card")
    card(c, "One page a human can act on.",
         sub="Everything Seekr holds about a person, scored on how well evidenced "
             "it is, with the source of every line — and what it does not establish.",
         kicker="Dossier", seconds=2.0, size=40, mark_svg=MARK_SVG)
    mark("dossier")
    # Back to the app first: a title card leaves the tab on a data: URL, where
    # a relative /v1 fetch has no origin and localStorage is empty.
    goto_ui(c, f"#/person/{person_id}", settle=1.8)

    # Show the pointer reach the button and press it, rather than cutting to a
    # document that appears from nowhere. The click is real — the button
    # enters its own busy state — and window.open is redirected so the report
    # lands in this tab instead of one the recorder cannot see.
    show_cursor(c, x=980, y=760)
    record(c, 0.35)
    c.js("""(function(){
      window.__seekrOpen = window.open;
      window.open = function(url){ window.__seekrPdf = url; return null; };
    })()""")
    sel = "button.btn:not([disabled])"
    dossier_btn = c.js("""(function(){
      const b=[...document.querySelectorAll('button')]
        .find(x=>x.textContent.trim().startsWith('Dossier'));
      if(!b) return null;
      b.setAttribute('data-demo','dossier');
      return true;
    })()""")
    if dossier_btn:
        sel = "button[data-demo='dossier']"
        move_cursor(c, sel, seconds=1.0)
        click_cursor(c, sel, hold=1.4)
    else:
        record(c, 0.6)

    # the report itself, rendered in place
    c.js("""(async function(){
      const res = await fetch(`/v1/persons/%s/dossier`, {
        headers: { Authorization: 'Bearer ' + localStorage.getItem('seekr_token') },
      });
      const html = await res.text();
      document.open(); document.write(html); document.close();
    })()""" % person_id)
    time.sleep(2.2)
    caption(c, "Dossier", "Evidence, scored — not the person",
            "A rubric over the record itself: corroboration, recency, identity "
            "strength, published output.")
    record(c, 3.0)
    caption_off(c)
    for _ in range(3):
        c.js("window.scrollBy({top:620, behavior:'smooth'})")
        record(c, 1.5)
    record(c, 0.5)

    # ---- shortlists ----
    mark("shortlist_card", "card")
    card(c, "Keep the people worth keeping.",
         sub="Saved into named lists, each entry remembering the query that found them.",
         kicker="Shortlists", seconds=1.6, size=40, mark_svg=MARK_SVG)
    goto_ui(c)
    mark("shortlist")
    type_query(c, "rust developers in berlin", per_char=0.018)
    submit(c)
    time.sleep(3.2)
    caption(c, "Shortlists", "Saved in one click", "")
    record(c, 0.8)
    c.js("""(function(){
      window.prompt = () => "Rust · Berlin";
      const b=[...document.querySelectorAll('button')].filter(x=>x.title==='Save to a shortlist');
      b[0] && b[0].click();
      setTimeout(()=>b[2] && b[2].click(), 650);
      setTimeout(()=>b[4] && b[4].click(), 1300);
    })()""")
    record(c, 2.6)
    caption_off(c)
    record(c, 0.4)

    # ---- close ----
    mark("close", "card")
    card(c, "Seekr", "Evidence in. Structure out.", kicker="Deccan AI",
         seconds=3.0, size=76, mark_svg=MARK_SVG)
    print(f"[film] captured {scenes.frame_no} frames")


if __name__ == "__main__":
    main()
