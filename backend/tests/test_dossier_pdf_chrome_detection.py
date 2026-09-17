"""_find_chrome_binary() locates a Chromium-based browser for the dossier
PDF endpoint. The original version only checked Mac/Linux paths — on
Windows it answered 501 unconditionally regardless of whether a browser was
actually installed, because none of its hardcoded paths could ever exist on
that platform. These tests simulate a Windows environment (mocking
shutil.which, pathlib.Path.exists, and the relevant environment variables)
to verify the fix actually finds a real Windows install location, since
this sandbox cannot run real Windows to test it directly.

Expected paths are built with the SAME os.path.join() calls the real
function uses, rather than hardcoded backslash strings — os.path.join uses
the CURRENT platform's separator, so a hardcoded Windows-style path string
only matches on a machine that's actually Windows. Building both sides the
same way keeps these tests meaningful on Linux, Mac, or Windows alike.
"""
import os

from rip.api import _find_chrome_binary


def test_chrome_binary_env_var_takes_priority(monkeypatch):
    """An explicit CHROME_BINARY override must win over everything else,
    even if it doesn't look like a real path — the operator said to use it."""
    monkeypatch.setenv("CHROME_BINARY", "/some/custom/path/to/chrome")
    assert _find_chrome_binary() == "/some/custom/path/to/chrome"


def test_finds_chrome_on_path_via_which(monkeypatch):
    """A bare command name resolvable via PATH (any platform) must be found
    without needing to guess an install directory at all."""
    monkeypatch.delenv("CHROME_BINARY", raising=False)
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/google-chrome" if name == "google-chrome" else None,
    )
    assert _find_chrome_binary() == "/usr/local/bin/google-chrome"


def test_finds_chrome_at_standard_windows_program_files_path(monkeypatch):
    """The actual reported bug: a real Windows Chrome install, at the
    standard 64-bit Program Files location, must now be found — the
    original path list had zero Windows entries."""
    monkeypatch.delenv("CHROME_BINARY", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)  # nothing on PATH
    program_files = r"C:\Program Files"
    monkeypatch.setenv("ProgramFiles", program_files)
    monkeypatch.setenv("ProgramFiles(x86)", r"C:\Program Files (x86)")
    monkeypatch.setenv("LocalAppData", r"C:\Users\Test\AppData\Local")

    expected = os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe")

    import pathlib

    def fake_exists(self):
        return str(self) == expected

    monkeypatch.setattr(pathlib.Path, "exists", fake_exists)
    assert _find_chrome_binary() == expected


def test_finds_chrome_at_per_user_appdata_windows_path(monkeypatch):
    """Chrome installed without admin rights lands in the current user's
    AppData folder, not Program Files — this must also be checked, since a
    non-admin Windows user is a completely normal, common case."""
    monkeypatch.delenv("CHROME_BINARY", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app_data = r"C:\Users\Test\AppData\Local"
    monkeypatch.setenv("LocalAppData", local_app_data)

    expected = os.path.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe")

    import pathlib

    def fake_exists(self):
        return str(self) == expected

    monkeypatch.setattr(pathlib.Path, "exists", fake_exists)
    assert _find_chrome_binary() == expected


def test_falls_back_to_edge_on_windows_when_chrome_is_absent(monkeypatch):
    """Microsoft Edge ships pre-installed on every modern Windows system
    and is Chromium-based — a Windows user with no separate Chrome install
    should still get PDF rendering to work via Edge."""
    monkeypatch.delenv("CHROME_BINARY", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    program_files_x86 = r"C:\Program Files (x86)"
    monkeypatch.setenv("ProgramFiles(x86)", program_files_x86)
    monkeypatch.setenv("LocalAppData", r"C:\Users\Test\AppData\Local")

    expected = os.path.join(program_files_x86, "Microsoft", "Edge", "Application", "msedge.exe")

    import pathlib

    def fake_exists(self):
        return str(self) == expected

    monkeypatch.setattr(pathlib.Path, "exists", fake_exists)
    assert _find_chrome_binary() == expected


def test_returns_none_when_nothing_is_found_anywhere(monkeypatch):
    """No browser at all: must return None cleanly (the caller turns this
    into a 501, not a crash) rather than raising or returning a garbage path."""
    monkeypatch.delenv("CHROME_BINARY", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    assert _find_chrome_binary() is None
