"""Regression coverage for the SDK wheel's verifier schema payload."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import venv
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SDK_SOURCE = ROOT / "backend" / "plotpilot_plugin_sdk"
AUTHORITATIVE_SCHEMA_DIR = ROOT / "contracts" / "json-schema"
WHEEL_SCHEMA_PREFIX = "plotpilot_plugin_sdk-0.1.2.data/data/Lib/contracts/json-schema/"


def _temporary_root() -> Path:
    """Keep Windows wheel paths short while retaining one disposable root."""
    if os.name == "nt":
        system_drive = (os.environ.get("SystemDrive") or "C:").rstrip("\\/") + "\\"
        return Path(tempfile.mkdtemp(prefix="pp-sdk-wheel-", dir=system_drive))
    return Path(tempfile.mkdtemp(prefix="pp-sdk-wheel-"))


def _subprocess_env(temp_root: Path) -> dict[str, str]:
    temp_dir = temp_root / "subprocess-tmp"
    temp_dir.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PIP_CACHE_DIR": str(temp_root / "pip-cache"),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONUTF8": "1",
            "TEMP": str(temp_dir),
            "TMP": str(temp_dir),
            "TMPDIR": str(temp_dir),
        }
    )
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
    )
    assert result.returncode == 0, (
        f"command failed ({result.returncode}): {' '.join(command)}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return result


def _pep517_builder(*, env: dict[str, str], cwd: Path) -> str:
    """Return an interpreter with the local setuptools/wheel backend available."""
    candidate = os.environ.get("PLOTPILOT_PEP517_PYTHON", sys.executable)
    result = subprocess.run(
        [candidate, "-c", "import setuptools, wheel"],
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
    )
    if result.returncode:
        pytest.fail(
            "PEP 517 builder lacks setuptools/wheel; set "
            "PLOTPILOT_PEP517_PYTHON to the configured bundled Python.\n"
            f"stderr:\n{result.stderr}"
        )
    return candidate


def _materialize_source(temp_root: Path) -> Path:
    source_root = temp_root / "source"
    package_root = source_root / "backend" / "plotpilot_plugin_sdk"
    shutil.copytree(
        SDK_SOURCE,
        package_root,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "build", "dist", "*.egg-info"),
    )
    shutil.copytree(AUTHORITATIVE_SCHEMA_DIR, source_root / "contracts" / "json-schema")
    return package_root


def _venv_python(venv_root: Path) -> Path:
    return venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _probe_script(expected_schema_names: list[str]) -> str:
    expected = json.dumps(expected_schema_names)
    return textwrap.dedent(
        f"""\
        import json
        import sys
        from pathlib import Path

        import plotpilot_plugin_sdk
        from plotpilot_plugin_sdk.errors import ContractValidationError
        from plotpilot_plugin_sdk.rpc import build_meta, build_request
        from plotpilot_plugin_sdk.verifier import SCHEMA_DIR, assert_valid, validate_rpc_request, validate_rpc_response

        expected = {expected}
        schema_dir = Path(sys.prefix) / "Lib" / "contracts" / "json-schema"
        assert SCHEMA_DIR == schema_dir, (SCHEMA_DIR, schema_dir)
        actual = sorted(path.name for path in schema_dir.glob("*.json"))
        assert actual == expected, (actual, expected)

        release_id = "a" * 64
        request = build_request(
            "runtime.handshake",
            {{
                "host_protocol": "1",
                "generation_id": "generation-wheel",
                "plugin_release_id": release_id,
                "data_generation_id": "data-generation-wheel",
            }},
            build_meta(
                "control",
                generation_id="generation-wheel",
                plugin_release_id=release_id,
                deadline_at="2030-01-02T03:04:05Z",
                operation_id="wheel-schema-probe",
            ),
            request_id="123e4567-e89b-42d3-a456-426614174000",
        )
        validate_rpc_request(request)
        response = {{
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {{"code": 1001, "message": "incompatible", "data": None}},
        }}
        validate_rpc_response(response, request=request)

        try:
            assert_valid("unknown-contract/v1", {{}})
        except ContractValidationError:
            pass
        else:
            raise AssertionError("unknown contract ids must fail closed")

        print(
            json.dumps(
                {{
                    "module_file": str(Path(plotpilot_plugin_sdk.__file__).resolve()),
                    "schema_dir": str(schema_dir),
                    "schema_names": actual,
                }},
                sort_keys=True,
            )
        )
        """
    )


def test_real_sdk_wheel_installs_authoritative_verifier_schemas() -> None:
    """Build, inspect, install, and execute the actual 0.1.2 SDK distribution."""
    expected_schema_names = sorted(path.name for path in AUTHORITATIVE_SCHEMA_DIR.glob("*.json"))
    assert expected_schema_names

    temp_root = _temporary_root()
    try:
        env = _subprocess_env(temp_root)
        package_root = _materialize_source(temp_root)
        wheel_dir = temp_root / "wheel"
        wheel_dir.mkdir()
        builder = _pep517_builder(env=env, cwd=package_root)
        _run(
            [
                builder,
                "-m",
                "pip",
                "wheel",
                "--use-pep517",
                "--no-build-isolation",
                "--no-deps",
                "--no-index",
                "--wheel-dir",
                str(wheel_dir),
                ".",
            ],
            cwd=package_root,
            env=env,
        )

        wheels = sorted(wheel_dir.glob("plotpilot_plugin_sdk-0.1.2-*.whl"))
        assert len(wheels) == 1
        wheel = wheels[0]
        with zipfile.ZipFile(wheel) as archive:
            wheel_schema_names = sorted(
                member.removeprefix(WHEEL_SCHEMA_PREFIX)
                for member in archive.namelist()
                if member.startswith(WHEEL_SCHEMA_PREFIX)
            )
            assert wheel_schema_names == expected_schema_names
            for schema_name in expected_schema_names:
                assert archive.read(WHEEL_SCHEMA_PREFIX + schema_name) == (
                    AUTHORITATIVE_SCHEMA_DIR / schema_name
                ).read_bytes()

        venv_root = temp_root / "venv"
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(venv_root)
        venv_python = _venv_python(venv_root)
        _run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-index",
                str(wheel),
            ],
            cwd=temp_root,
            env=env,
        )

        probe = temp_root / "installed_probe.py"
        probe.write_text(_probe_script(expected_schema_names), encoding="utf-8")
        result = _run([str(venv_python), str(probe)], cwd=temp_root, env=env)
        proof = json.loads(result.stdout)
        assert Path(proof["schema_dir"]).resolve() == (
            venv_root / "Lib" / "contracts" / "json-schema"
        ).resolve()
        assert proof["schema_names"] == expected_schema_names
        assert Path(proof["module_file"]).resolve().is_relative_to(venv_root.resolve())
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
