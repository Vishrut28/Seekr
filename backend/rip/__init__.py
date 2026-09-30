"""Seekr — resource intelligence platform.

A data layer that discovers, normalizes, and maintains evidence-backed
profiles of publicly discoverable people. It deliberately contains no
ranking, scoring, or recommendation logic - that lives in the downstream
internal ranking tool, which consumes this platform's API.
"""

import contextlib
import sys

__version__ = "0.1.0"


def _utf8_console() -> None:
    """Print people's names whatever the console's code page.

    A Windows console defaults to cp1252, and the corpus holds names it cannot
    encode ("Rodríguez‐Cerezo", with a Unicode hyphen; Greek letters in
    titles): a command printing one stopped halfway with UnicodeEncodeError.
    Every command and script imports this package, so this is where it is fixed.
    Only a stream that can be reconfigured and is not UTF-8 already is touched.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if reconfigure is not None and encoding != "utf8":
            # a stream that refuses keeps what it had
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")


_utf8_console()
