"""Installed optional-platform assets must exist even without Windows/Prime."""

import re
from importlib.resources import files


def test_prime_script_is_bundled_and_never_controls_application_lifetime() -> None:
    script = files("pymcdx").joinpath("prime/render.ps1").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in script.splitlines())
    assert code.strip(), "Prime automation resource must not be empty"
    # Shared COM cannot prove application ownership. These operations previously
    # affected a user's existing session; this guard runs without launching COM.
    prohibited = r"\.(?:Quit|Kill)\s*\(|\.Visible\s*=|\b(?:Stop-Process|taskkill)\b"
    assert not re.search(prohibited, code, re.IGNORECASE), "unsafe shared-Prime lifecycle operation"
