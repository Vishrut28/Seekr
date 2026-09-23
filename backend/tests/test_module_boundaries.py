"""The seam between parsing and live discovery, which is only a seam while
nothing crosses it.

nlq.py was 4,591 lines doing two unrelated jobs: turning a question into
filters against the corpus we already hold, and asking OpenAlex, GitHub,
ORCID and six others about people we do not. The second job is now
rip/discovery.py.

A split like this decays in two ways, and both are silent:

  * a name creeps back the other way — nlq grows a call into discovery, and
    answering a query from the local corpus starts depending on code whose
    whole purpose is to make network requests;
  * nlq keeps a convenience re-export, and then `monkeypatch.setattr(nlq,
    "SUGGESTION_SEARCHERS", fake)` patches a name nothing reads. The test
    goes green and the searcher it meant to replace talks to the real
    internet, at real cost, from a test run.

The second is why there is no compatibility shim: every stale reference was
repointed at the time of the split, and any that was missed raises
AttributeError instead of quietly reaching the network.
"""

import ast
import pathlib

RIP = pathlib.Path(__file__).resolve().parent.parent / "rip"


def imports_of(module: str) -> set[str]:
    """Every sibling module named by an import anywhere in the file, including
    the function-local imports this package uses to break cycles."""
    tree = ast.parse((RIP / f"{module}.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("rip."):
                    found.add(alias.name.split(".")[1])
    return found


def test_the_dependency_runs_one_way():
    """discovery may ask the parser what a query meant; the parser may not ask
    the network anything."""
    assert "nlq" in imports_of("discovery")
    assert "discovery" not in imports_of("nlq")


def test_nlq_does_not_re_export_what_moved():
    """A shim here would make every stale `monkeypatch.setattr(nlq, ...)` in
    the suite pass while patching nothing."""
    from rip import discovery, nlq

    moved = ("SUGGESTION_SEARCHERS", "discovery_suggestions", "queue_suggestions",
             "persist_suggestions", "worth_searching_live", "LIVE_BUDGET_SECONDS",
             "LIVE_SEARCH_SECONDS", "_store_results", "_fetch_profiles",
             "_source_throttled", "_search_openalex", "_search_github")
    for name in moved:
        assert hasattr(discovery, name), f"{name} is not in discovery"
        assert not hasattr(nlq, name), f"nlq still re-exports {name}"


def test_what_discovery_borrows_back_is_a_short_list():
    """Six names. If this grows, the two halves are becoming one again and the
    boundary needs redrawing rather than widening."""
    tree = ast.parse((RIP / "discovery.py").read_text(encoding="utf-8"))
    borrowed = {alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module == "nlq"
                for alias in node.names}
    assert borrowed == {"NLQuery", "NOISE_WORDS", "ROLE_MODIFIERS",
                        "_could_be_a_name", "_looks_like_a_person"}


def test_answering_from_the_corpus_needs_no_network_code():
    """The point of the split: `import rip.nlq` must not drag in the module
    that holds every outbound call."""
    import subprocess
    import sys

    probe = ("import sys, rip.nlq; "
             "sys.exit(1 if 'rip.discovery' in sys.modules else 0)")
    assert subprocess.run([sys.executable, "-c", probe], cwd=str(RIP.parent)).returncode == 0
