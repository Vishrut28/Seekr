import contextlib
import os
import threading

from .base import BaseConnector
from .dblp import DblpConnector
from .europepmc import EuropePmcConnector
from .exa import ExaConnector
from .github import GitHubConnector
from .huggingface import HuggingFaceConnector
from .openalex import OpenAlexConnector
from .orcid import OrcidConnector
from .semanticscholar import SemanticScholarConnector
from .stackoverflow import StackOverflowConnector
from .web import WebConnector
from .wikidata import WikidataConnector

CONNECTORS: dict[str, type[BaseConnector]] = {
    "web": WebConnector,
    "wikidata": WikidataConnector,
    "dblp": DblpConnector,
    "europepmc": EuropePmcConnector,
    "exa": ExaConnector,
    "github": GitHubConnector,
    "huggingface": HuggingFaceConnector,
    "openalex": OpenAlexConnector,
    "orcid": OrcidConnector,
    "semanticscholar": SemanticScholarConnector,
    "stackoverflow": StackOverflowConnector,
}

# Credentials a connector reads when it is built. Part of the cache key, so
# setting a token takes effect instead of being ignored by an instance built
# before it existed.
_CONFIG_ENV = ("GITHUB_TOKEN", "OPENALEX_MAILTO", "SEMANTIC_SCHOLAR_API_KEY",
               "STACKEXCHANGE_KEY", "EXA_API_KEY")
_instances: dict = {}
_instances_lock = threading.Lock()


def get_connector(source: str):
    """The shared connector for SOURCE in this process.

    Shared, not built per call: its rate-limit clock is what keeps concurrent
    fetches to one source polite, and its HTTP client is what keeps
    connections alive between them. A fresh instance per fetch had neither.
    """
    try:
        cls = CONNECTORS[source]
    except KeyError:
        raise ValueError(f"Unknown source '{source}'. Available: {sorted(CONNECTORS)}") from None
    key = (source, tuple(os.environ.get(name) for name in _CONFIG_ENV))
    with _instances_lock:
        connector = _instances.get(key)
        if connector is None:
            connector = _instances[key] = cls()
        return connector


def reset_connectors() -> None:
    """Drop shared connectors (tests, or after changing proxy settings)."""
    with _instances_lock:
        for connector in _instances.values():
            client = getattr(connector, "_client", None)
            if client is not None and hasattr(client, "close"):
                with contextlib.suppress(Exception):
                    client.close()
        _instances.clear()
