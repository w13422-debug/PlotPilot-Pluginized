"""Regression tests for import-order-independent production composition."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _run_fresh_python(source: str) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "backend")
    result = subprocess.run(
        [sys.executable, "-B", "-c", source],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "fresh process import failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_compose_job_runtime_is_cold_importable() -> None:
    _run_fresh_python(
        "from backend.plotpilot_core.jobs.http_rpc.composition "
        "import compose_job_runtime\n"
        "assert callable(compose_job_runtime)\n"
    )


def test_all_public_backup_exports_are_cold_importable() -> None:
    _run_fresh_python(
        "from backend.plotpilot_core import backup\n"
        "for name in backup.__all__:\n"
        "    getattr(backup, name)\n"
    )
