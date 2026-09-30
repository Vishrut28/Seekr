"""A name the console's code page cannot encode must still print.

On a Windows console (cp1252) judge_conflated stopped halfway on a Greek
letter in a title, and the CLI would on "Rodríguez‐Cerezo" (a Unicode
hyphen). Importing rip switches a non-UTF-8 console to UTF-8.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def test_importing_rip_lets_any_name_print():
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONPATH": str(BACKEND)}
    env.pop("PYTHONUTF8", None)
    name = "Emilio Rodríguez‐Cerezo, β-lactam"
    done = subprocess.run(
        [sys.executable, "-c", f"import rip; print({name!r})"],
        capture_output=True, env=env, cwd=BACKEND, timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert done.stdout.decode("utf-8").strip() == name


def test_without_it_that_name_did_not_print():
    """The test above is only worth something if cp1252 really refuses."""
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    env.pop("PYTHONUTF8", None)
    done = subprocess.run(
        [sys.executable, "-c", "print('Rodríguez‐Cerezo')"],
        capture_output=True, env=env, timeout=60,
    )
    assert done.returncode != 0
    assert b"UnicodeEncodeError" in done.stderr
