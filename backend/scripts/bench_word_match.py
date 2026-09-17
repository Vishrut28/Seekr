"""Isolate the _word_match(Evidence.value, ...) path specifically: the
skill= filter on /v1/persons, and the location_anywhere() bio-text branch
used by every location-filtered /v1/query. Run with SEEKR_BASE set.
"""
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request

BASE = os.environ.get("SEEKR_BASE", "http://127.0.0.1:8000")
TOKEN = os.environ.get("SEEKR_TOKEN", "localdev")

# terms guaranteed to appear only inside bio/role free text for a chunk of
# the synthetic corpus (see scripts/seed_bench_corpus.py) — never as an
# exact SKILL_ATTRS vocabulary entry — so this exercises the LOCATION_TEXT_ATTRS
# / bio branch of location_anywhere(), the _word_match(Evidence.value,...)
# hot path this benchmark targets.
SKILL_QUERIES = [
    "Distributed Systems", "Rust", "Kubernetes", "Machine Learning",
    "Computer Vision", "Robotics", "Cybersecurity", "Databases",
]
LOCATION_QUERIES = [
    "Bangalore", "Mumbai", "Hyderabad", "Berlin", "London",
    "Toronto", "Singapore", "Pune",
]


def timed(url: str) -> float:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    start = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp.read()
    return time.perf_counter() - start


def bench_skill_filter(n_repeats: int = 3):
    times = []
    for _ in range(n_repeats):
        for q in SKILL_QUERIES:
            url = f"{BASE}/v1/persons?skill={urllib.parse.quote(q)}&limit=50"
            times.append(timed(url))
    return times


def bench_location_filter(n_repeats: int = 3):
    times = []
    for _ in range(n_repeats):
        for q in LOCATION_QUERIES:
            url = f"{BASE}/v1/query?q={urllib.parse.quote(q)}&discover=false"
            times.append(timed(url))
    return times


def report(name: str, times: list) -> None:
    s = sorted(times)
    p50 = s[len(s) // 2]
    p95 = s[int(len(s) * 0.95)] if len(s) > 1 else s[0]
    print(f"{name:<20} n={len(times):3d}  p50={p50*1000:7.1f}ms  "
          f"p95={p95*1000:7.1f}ms  max={max(times)*1000:7.1f}ms  "
          f"mean={statistics.mean(times)*1000:7.1f}ms")


if __name__ == "__main__":
    report("skill filter", bench_skill_filter())
    report("location filter", bench_location_filter())
