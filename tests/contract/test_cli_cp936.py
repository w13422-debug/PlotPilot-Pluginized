from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "integration" / "verify_contracts.py"


@pytest.mark.skipif(os.name != "nt", reason="CP936 console regression is Windows-specific")
def test_verify_contracts_all_succeeds_from_real_cp936_cmd_without_utf8_overrides() -> None:
    """The published Python gate must not require PYTHONUTF8/PYTHONIOENCODING."""

    env = os.environ.copy()
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONIOENCODING", None)
    # Force the legacy console path so the child observes the code page set by
    # cmd.exe instead of inheriting the UTF-8 console mode of the test runner.
    env["PYTHONLEGACYWINDOWSSTDIO"] = "1"
    # These two paths are space-free on the supported Windows checkout.  Do
    # not wrap the whole /c command in another quote pair: cmd.exe's /s quote
    # stripping otherwise turns the executable into a literal `\"...` token.
    command = f"chcp 936>nul & {sys.executable} -B {SCRIPT} --all"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", command],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, (
        f"CP936 verifier failed with exit={completed.returncode}\n"
        f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
    )
    assert b"UnicodeEncodeError" not in completed.stderr
    assert completed.stdout.lstrip().startswith(b"{")
