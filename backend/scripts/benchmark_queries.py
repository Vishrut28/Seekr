"""Run a suite of realistic queries and report how Seekr handles each.

Local-only by default (no paid calls). Reports, per query: how many people
matched, which filters were applied, which terms were dropped, and how long
the request took — so a bad result can be traced to parsing, coverage, or
speed, and a performance change has real before/after numbers instead of a
guess.

Usage:
    python scripts/benchmark_queries.py                  # discover=false, one pass
    python scripts/benchmark_queries.py true              # discover=true, one pass
    python scripts/benchmark_queries.py --compare         # both, side by side
    python scripts/benchmark_queries.py --warm            # a throwaway pass first,
                                                            # so the vocab cache is
                                                            # warm before the timed run
"""

import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request

BASE = os.environ.get("SEEKR_BASE", "http://127.0.0.1:8000")
TOKEN = os.environ.get("SEEKR_TOKEN", "localdev")

# (category, query) — the category groups drive the per-category timing
# breakdown below; keep it in sync when adding new queries.
QUERIES = [
    # --- easy
    ("easy", "Product designers at Deccan.ai"),
    ("easy", "ML engineers at OpenAI"),
    ("easy", "Backend engineers in Bangalore"),
    ("easy", "Researchers at IIT Bombay working on robotics"),
    ("easy", "Data scientists at Google India"),
    ("easy", "Founders of Indian AI startups"),
    ("easy", "Python developers in Mumbai"),
    ("easy", "UX designers with fintech experience"),
    ("easy", "Computer vision researchers"),
    ("easy", "Rust developers in India"),
    ("easy", "Engineers who have worked at both Google and Microsoft"),
    ("easy", "Open source maintainers in India"),
    ("easy", "Product managers at SaaS companies"),
    ("easy", "AI researchers from Stanford"),
    ("easy", "Cybersecurity engineers in Bangalore"),
    # --- medium
    ("medium", "Distributed systems engineers who have worked at large-scale tech companies"),
    ("medium", "Indian researchers publishing papers on reinforcement learning"),
    ("medium", "Product designers who have worked on developer tools"),
    ("medium", "Engineers contributing to popular Kubernetes projects"),
    ("medium", "Researchers in India working on multimodal AI"),
    ("medium", "Machine learning engineers with experience deploying models in production"),
    ("medium", "Backend engineers who specialize in high-throughput systems"),
    ("medium", "Computer science PhDs working on LLM evaluation"),
    ("medium", "Engineers who have contributed to major open-source databases"),
    ("medium", "Product designers with experience in AI products and enterprise SaaS"),
    ("medium", "Robotics engineers who have published papers and worked in industry"),
    ("medium", "People working on privacy-preserving machine learning in Europe"),
    ("medium", "Software engineers who have spoken at engineering conferences about distributed systems"),
    ("medium", "Data engineers with experience at fintech companies and strong open-source contributions"),
    ("medium", "Researchers who work on compiler optimization and have industry experience"),
    # --- complex
    ("complex", "Find researchers in India working on multimodal AI who have published at NeurIPS, ICML, or ACL and currently work outside academia"),
    ("complex", "Find engineers who have contributed significantly to Kubernetes and have worked at companies operating large-scale distributed systems"),
    ("complex", "Product designers who have designed AI-native products, have experience at an early-stage startup, and currently work in India"),
    ("complex", "Find people who have both academic research and production engineering experience in reinforcement learning"),
    ("complex", "Software engineers in India who specialize in distributed databases, have public GitHub contributions, and have previously worked at Google, Amazon, Microsoft, or Meta"),
    ("complex", "Find researchers working on LLM reasoning or evaluation with publications from the last three years and a public research profile"),
    ("complex", "Find robotics researchers who have both peer-reviewed publications and substantial open-source projects"),
    ("complex", "Indian computer scientists with expertise in compilers who have published research and worked in industry"),
    ("complex", "Find cybersecurity researchers who have published on vulnerability discovery and contribute to open source"),
    ("complex", "Find ML engineers who have worked on model training infrastructure and contributed to open-source ML tooling"),
    ("complex", "Researchers who have moved from academia into AI startups and continue to publish publicly"),
    ("complex", "Engineers with expertise in high-performance computing who have contributed to open-source systems projects"),
    # --- messy / conversational
    ("messy", "I need someone really good at distributed systems, ideally someone who's built this stuff at scale and has some public work to show"),
    ("messy", "Find me a few people who actually understand AI infrastructure, not just ML modeling"),
    ("messy", "Looking for a researcher who knows reinforcement learning really well but has also spent time building real products"),
    ("messy", "Who are the strongest open-source contributors in India working on databases?"),
    ("messy", "Find people who seem unusually strong in computer vision"),
    # --- stress tests
    ("stress", "People who worked at DeepMind and later joined an AI startup"),
    ("stress", "Researchers who published on graph neural networks and have open-source implementations on GitHub"),
    ("stress", "Engineers who contributed to PostgreSQL and have experience building distributed systems"),
    ("stress", "Indian AI researchers with publications at top conferences and a public GitHub presence"),
    ("stress", "People with experience in both robotics and computer vision who have published research since 2023"),
    ("stress", "Strong generalist engineers who have worked across backend, infrastructure, and ML systems"),
    ("stress", "Experts in low-level systems programming"),
    # --- how the same question is typed: number, spelling, phrases the corpus
    # does not hold. Each pair should behave the same way, and a phrase nobody
    # in the corpus works on should be reported, not answered with one of its
    # words ("networks" alone used to return wireless sensor researchers).
    ("variants", "distributed systems engineers"),
    ("variants", "distributed system engineers"),
    ("variants", "neural networks researchers"),
    ("variants", "neural network researchers"),
    ("variants", "distributed sytems engineers"),
    ("variants", "computer vsion researchers"),
    ("variants", "reinforcement lerning"),
    ("variants", "graph neural networks"),
]


def run(query: str, discover: str = "false"):
    """One request. Returns (response, elapsed_seconds, error)."""
    url = f"{BASE}/v1/query?q={urllib.parse.quote(query)}&discover={discover}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.load(resp)
        return data, time.perf_counter() - start, None
    except Exception as exc:
        return None, time.perf_counter() - start, exc


def _pct(values, p: float) -> float:
    """Nearest-rank percentile, no numpy dependency."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
    return s[k]


def _fmt_ms(seconds: float) -> str:
    return f"{seconds * 1000:6.0f}ms"


def one_pass(discover: str, warm: bool):
    """Run every query once against BASE. Returns a list of per-query records."""
    if warm:
        # a throwaway request so the vocab cache (RIP_VOCAB_TTL) is warm
        # before anything in this pass is timed — otherwise the first query
        # alone eats the full cold-cache rebuild cost and skews that one
        # query's number without telling you anything about steady state
        run(QUERIES[0][1], discover)

    records = []
    for category, q in QUERIES:
        d, elapsed, err = run(q, discover)
        if err is not None:
            records.append({"category": category, "query": q, "elapsed": elapsed,
                             "error": str(err), "n": -1})
            continue
        f = d["applied_filters"]
        applied = []
        for key, label in (("skills", "skill"), ("skill_patterns", "skill~"),
                           ("organizations", "org"), ("locations", "loc"),
                           ("countries", "country"), ("name_terms", "name")):
            for v in f.get(key) or []:
                applied.append(f"{label}:{v}")
        n = max(d["total_matches"], d.get("count") or 0)
        records.append({
            "category": category, "query": q, "elapsed": elapsed, "error": None,
            "n": n, "applied": applied, "dropped": d["unmatched_terms"],
            "empty_reason": d.get("empty_reason"),
            "discovery_suggestions": len(d.get("discovery_suggestions") or []),
            "stored_from_live": d.get("stored_from_live", 0),
        })
    return records


def print_report(records, title: str) -> None:
    good = [r for r in records if r["n"] >= 3]
    thin = [r for r in records if r["n"] in (1, 2)]
    zero = [r for r in records if r["n"] == 0]
    errors = [r for r in records if r["n"] == -1]

    print(f"\n=== {title} ===")
    print(f"{'ms':>7}  {'n':>6}  query / filters / dropped")
    print("-" * 100)
    for r in records:
        print(f"{_fmt_ms(r['elapsed']):>7}  {r['n']:>6}  {r['query'][:74]}")
        if r["n"] == -1:
            print(f"{'':>15}ERROR: {r['error']}")
            continue
        if r["applied"]:
            print(f"{'':>15}applied: {', '.join(str(x)[:40] for x in r['applied'])[:110]}")
        if r["dropped"]:
            print(f"{'':>15}dropped: {', '.join(r['dropped'])[:110]}")
        if r["empty_reason"]:
            print(f"{'':>15}why-empty: {r['empty_reason']['message'][:100]}")

    total = len(records)
    print("-" * 100)
    print(f"3+ results: {len(good)}/{total}   1-2: {len(thin)}/{total}   "
          f"zero: {len(zero)}/{total}   errors: {len(errors)}/{total}")

    # timing, overall and per category — this is the number that matters for
    # judging whether a performance change actually helped
    times = [r["elapsed"] for r in records if r["n"] != -1]
    if times:
        print(f"\n{'category':<10} {'n':>4}  {'p50':>8}  {'p95':>8}  {'max':>8}  {'mean':>8}")
        print("-" * 60)
        categories = list(dict.fromkeys(r["category"] for r in records))
        for cat in categories + ["ALL"]:
            group = times if cat == "ALL" else [
                r["elapsed"] for r in records if r["category"] == cat and r["n"] != -1
            ]
            if not group:
                continue
            print(f"{cat:<10} {len(group):>4}  {_fmt_ms(_pct(group, 50)):>8}  "
                  f"{_fmt_ms(_pct(group, 95)):>8}  {_fmt_ms(max(group)):>8}  "
                  f"{_fmt_ms(statistics.mean(group)):>8}")


def main() -> None:
    args = sys.argv[1:]
    warm = "--warm" in args
    args = [a for a in args if a != "--warm"]
    compare = "--compare" in args
    args = [a for a in args if a != "--compare"]

    if compare:
        cold = one_pass("false", warm=warm)
        print_report(cold, "discover=false")
        live = one_pass("true", warm=warm)
        print_report(live, "discover=true")

        # the number that actually answers "how much does live search cost":
        t_false = statistics.mean(r["elapsed"] for r in cold if r["n"] != -1)
        t_true = statistics.mean(r["elapsed"] for r in live if r["n"] != -1)
        print(f"\nmean latency: discover=false {_fmt_ms(t_false)}  "
              f"discover=true {_fmt_ms(t_true)}  "
              f"(+{_fmt_ms(t_true - t_false)}, {t_true / max(t_false, 1e-9):.1f}x)")
        return

    discover = args[0] if args else "false"
    records = one_pass(discover, warm=warm)
    print_report(records, f"discover={discover}")


if __name__ == "__main__":
    main()
