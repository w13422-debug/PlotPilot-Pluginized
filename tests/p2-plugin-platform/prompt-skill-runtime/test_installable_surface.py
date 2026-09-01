from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import os
import shutil
import subprocess
import json
import re
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from plotpilot_plugin_sdk import ContractError, assert_valid, build_files_sha256
from plotpilot_plugin_sdk.framing import FrameDecoder, encode_frame
from plotpilot_plugin_sdk.rpc import build_meta, build_request
from plotpilot_plugin_sdk.verifier import validate_rpc_response
from plotpilot_core.plugins.package import PackageError, verify_package
from plotpilot_core.supervisor.models import RuntimeRoute
from plotpilot_core.supervisor.process import isolated_environment
from plotpilot_core.supervisor.venv import OfflineVenvProvisioner, validate_offline_lock

PLUGIN = ROOT / "first-party-plugins" / "prompt-skill-runtime"


def _write_wheel(
    destination: Path,
    *,
    distribution: str,
    version: str,
    members: dict[str, bytes],
    requires: tuple[str, ...] = (),
    tag: str = "py3-none-any",
) -> None:
    """Write a deterministic, self-contained wheel without a build backend."""

    dist_info = f"{distribution.replace('-', '_')}-{version}.dist-info"
    metadata = (
        "Metadata-Version: 2.3\n"
        f"Name: {distribution}\n"
        f"Version: {version}\n"
        "Summary: deterministic PlotPilot install fixture\n"
        "Requires-Python: >=3.12,<3.13\n"
        + "".join(f"Requires-Dist: {requirement}\n" for requirement in requires)
    ).encode("utf-8")
    pure = tag == "py3-none-any"
    wheel = (
        "Wheel-Version: 1.0\n"
        "Generator: plotpilot-installable-surface-test\n"
        f"Root-Is-Purelib: {'true' if pure else 'false'}\n"
        f"Tag: {tag}\n"
    ).encode("ascii")
    entries = dict(members)
    entries[f"{dist_info}/METADATA"] = metadata
    entries[f"{dist_info}/WHEEL"] = wheel
    record_lines: list[str] = []
    for name in sorted(entries):
        digest = base64.urlsafe_b64encode(hashlib.sha256(entries[name]).digest()).rstrip(b"=").decode("ascii")
        record_lines.append(f"{name},sha256={digest},{len(entries[name])}")
    record_lines.append(f"{dist_info}/RECORD,,")
    entries[f"{dist_info}/RECORD"] = ("\n".join(record_lines) + "\n").encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, entries[name])


def _installed_distribution_members(distribution: str) -> dict[str, bytes]:
    record = importlib.metadata.distribution(distribution)
    root = Path(record.locate_file(""))
    members: dict[str, bytes] = {}
    for relative in record.files or ():
        path = Path(str(relative))
        # ``importlib.metadata`` exposes entry-point scripts as paths outside
        # site-packages (for example ``../../Scripts/jsonschema.exe``).  Such
        # paths are valid RECORD entries for an installed distribution, but
        # they are not package members and pip quite correctly rejects a
        # fixture wheel that tries to install them through traversal.
        if (
            ".." in path.parts
            or ".dist-info" in path.parts
            or ".data" in path.parts
            or path.suffix == ".pyc"
        ):
            continue
        source = root / path
        if source.is_file():
            members[path.as_posix()] = source.read_bytes()
    if not members:
        raise AssertionError(f"installed fixture distribution has no importable files: {distribution}")
    return members


def _source_package_members(source_root: Path, package_name: str) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    for source in source_root.rglob("*"):
        if not source.is_file() or "__pycache__" in source.parts:
            continue
        relative = source.relative_to(source_root).as_posix()
        members[f"{package_name}/{relative}"] = source.read_bytes()
    return members


def _build_fixture_wheels(tmp_path: Path) -> Path:
    wheelhouse = tmp_path / "release" / "backend" / "wheels"
    wheelhouse.mkdir(parents=True)
    versions = {
        name: importlib.metadata.version(name)
        for name in (
            "attrs",
            "jsonschema",
            "jsonschema-specifications",
            "referencing",
            "rpds-py",
            "rfc8785",
            "typing-extensions",
        )
    }
    dependencies = {
        "attrs": (),
        "jsonschema": (
            "attrs>=22.2.0",
            "jsonschema-specifications>=2023.03.6",
            "referencing>=0.28.4",
            "rpds-py>=0.7.1",
        ),
        "jsonschema-specifications": (),
        "referencing": ("attrs>=22.2.0", "rpds-py>=0.7.0", "typing-extensions>=4.4.0"),
        "rpds-py": (),
        "rfc8785": (),
        "typing-extensions": (),
    }
    for name, version in versions.items():
        normalized = name.replace("-", "_")
        tag = "cp312-cp312-win_amd64" if name == "rpds-py" and os.name == "nt" else "py3-none-any"
        filename = f"{normalized}-{version}-{tag}.whl"
        _write_wheel(
            wheelhouse / filename,
            distribution=name,
            version=version,
            members=_installed_distribution_members(name),
            requires=dependencies[name],
            tag=tag,
        )

    sdk_members = _source_package_members(ROOT / "backend" / "plotpilot_plugin_sdk", "plotpilot_plugin_sdk")
    for schema in sorted((ROOT / "contracts" / "json-schema").glob("*.schema.json")):
        sdk_members[
            f"plotpilot_plugin_sdk-0.1.0.data/data/Lib/contracts/json-schema/{schema.name}"
        ] = schema.read_bytes()
    sdk_wheel = wheelhouse / "plotpilot_plugin_sdk-0.1.0-py3-none-any.whl"
    _write_wheel(
        sdk_wheel,
        distribution="plotpilot-plugin-sdk",
        version="0.1.0",
        members=sdk_members,
        requires=("jsonschema>=4.20", "rfc8785>=0.1.4"),
    )

    plugin_members = _source_package_members(
        PLUGIN / "src" / "plotpilot_prompt_skill_runtime",
        "plotpilot_prompt_skill_runtime",
    )
    # ``pyproject.toml`` publishes the install-time manifest through
    # ``data-files``.  Keep the hand-built wheel fixture faithful to that
    # layout; the source-tree copy is not installed into the venv by pip.
    plugin_members[
        "plotpilot_prompt_skill_runtime-1.0.0.data/data/migrations/manifest.json"
    ] = (PLUGIN / "migrations" / "manifest.json").read_bytes()
    plugin_wheel = tmp_path / "release" / "backend" / "plotpilot_prompt_skill_runtime-1.0.0-py3-none-any.whl"
    _write_wheel(
        plugin_wheel,
        distribution="plotpilot-prompt-skill-runtime",
        version="1.0.0",
        members=plugin_members,
        requires=("plotpilot-plugin-sdk==0.1.0",),
    )

    return tmp_path / "release"


def _refresh_files_manifest(release_root: Path) -> None:
    ordinary = {
        source.relative_to(release_root).as_posix(): source.read_bytes()
        for source in release_root.rglob("*")
        if source.is_file() and source.name != "files.sha256"
    }
    (release_root / "files.sha256").write_bytes(build_files_sha256(ordinary))


def _manifest_lock_resource() -> Path:
    manifest = json.loads((PLUGIN / "plugin.json").read_bytes())
    resource = manifest["backend"]["requirements_lock"]
    if not isinstance(resource, str) or not resource or resource.startswith("/") or "\\" in resource:
        raise AssertionError("plugin manifest requirements_lock is not a relative resource")
    relative = Path(*resource.split("/"))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise AssertionError("plugin manifest requirements_lock escapes the package root")
    source = PLUGIN / relative
    if not source.is_file():
        raise AssertionError("plugin manifest requirements_lock is missing from the source package")
    return source


def _materialize_release_fixture(tmp_path: Path) -> Path:
    release_root = _build_fixture_wheels(tmp_path)
    for relative in ("plugin.json", "migrations/manifest.json"):
        destination = release_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((PLUGIN / relative).read_bytes())
    source_lock = _manifest_lock_resource()
    release_lock = release_root / source_lock.relative_to(PLUGIN)
    release_lock.parent.mkdir(parents=True, exist_ok=True)
    # The release fixture consumes the committed lock as raw bytes.  It must
    # never synthesize a test-only lock from the temporary wheelhouse.
    release_lock.write_bytes(source_lock.read_bytes())
    if release_lock.read_bytes() != source_lock.read_bytes():
        raise AssertionError("release fixture lock bytes drifted from the committed source")
    _refresh_files_manifest(release_root)
    return release_root


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        source.relative_to(root).as_posix(): source.read_bytes()
        for source in root.rglob("*")
        if source.is_file()
    }


def _build_ppplugin(package_root: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for source in sorted((item for item in package_root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(package_root).as_posix()):
            name = source.relative_to(package_root).as_posix()
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes())


def _read_child_frame(process: subprocess.Popen[bytes]) -> dict[str, object]:
    assert process.stdout is not None
    decoder = FrameDecoder()
    while True:
        chunk = process.stdout.read1(65536)
        if not chunk:
            raise AssertionError(f"worker exited before responding: {process.poll()}")
        messages = decoder.feed(chunk)
        if messages:
            assert len(messages) == 1
            return messages[0]


def test_prompt_skill_plugin_manifest_consumes_the_frozen_v2_contract() -> None:
    manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    assert_valid("plotpilot-plugin/v1", manifest)
    assert manifest["plugin_id"] == "com.plotpilot.prompt-skill-runtime"
    assert manifest["version"] == "1.0.0"
    assert manifest["capabilities"] == [{
        "capability_id": "prompt.skill.execute/v2",
        "operations": ["run", "resume", "cancel"],
        "result_contract": "artifact-bundle/v1",
    }]
    assert manifest["backend"]["entrypoint"] == "plotpilot_prompt_skill_runtime.worker:main"
    assert manifest["storage"]["migration_manifest"] == "migrations/manifest.json"


def test_source_package_is_isolated_and_migration_manifest_is_content_addressed() -> None:
    pyproject = tomllib.loads((PLUGIN / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["dependencies"] == ["plotpilot-plugin-sdk==0.1.0"]
    assert pyproject["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]
    migration = json.loads((PLUGIN / "migrations" / "manifest.json").read_text(encoding="utf-8"))
    assert migration["schema"] == "p3-module-migrations/v1"
    assert re.fullmatch(r"[0-9a-f]{64}", migration["from_schema"])
    assert re.fullmatch(r"[0-9a-f]{64}", migration["to_schema"])
    step = migration["steps"][0]
    sql = PLUGIN / "src" / "plotpilot_prompt_skill_runtime" / "migrations" / "001_prompt_skill_runtime.sql"
    assert set(step) == {"step_id", "file", "sha256", "from_schema", "to_schema"}
    assert step["file"] == "plotpilot_prompt_skill_runtime/migrations/001_prompt_skill_runtime.sql"
    assert step["from_schema"] == migration["from_schema"]
    assert step["to_schema"] == migration["to_schema"]
    assert step["sha256"] == hashlib.sha256(sql.read_bytes()).hexdigest()


def test_offline_lock_is_complete_exact_and_hash_pinned() -> None:
    lines = [
        line.strip()
        for line in (PLUGIN / "requirements.lock").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    expected = {
        "plotpilot-plugin-sdk",
        "jsonschema",
        "rfc8785",
        "attrs",
        "jsonschema-specifications",
        "referencing",
        "rpds-py",
        "typing-extensions",
    }
    assert {line.split("==", 1)[0] for line in lines} == expected
    assert all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9._+!-]*(?: --hash=sha256:[0-9a-f]{64})+", line) for line in lines)


def test_surface_has_no_plugin_local_execute_schema_or_public_http_route() -> None:
    source = (PLUGIN / "src" / "plotpilot_prompt_skill_runtime" / "host_adapter.py").read_text(encoding="utf-8")
    worker = (PLUGIN / "src" / "plotpilot_prompt_skill_runtime" / "worker.py").read_text(encoding="utf-8")
    assert "prompt_skill_rpc_v2" in source + worker
    assert "PromptSkillExecuteRequest" in source
    assert "sqlite3" not in source + worker
    assert "api/v1" not in source + worker
    assert "class Skill" not in source + worker
    assert "class PromptSkillRuntime" not in source + worker


def test_committed_lock_is_the_raw_deterministic_release_source_of_truth(tmp_path: Path) -> None:
    first_root = _materialize_release_fixture(tmp_path / "first")
    second_root = _materialize_release_fixture(tmp_path / "second")
    source_lock = _manifest_lock_resource()
    lock_relative = source_lock.relative_to(PLUGIN)
    source_bytes = source_lock.read_bytes()
    assert (first_root / lock_relative).read_bytes() == source_bytes
    assert (second_root / lock_relative).read_bytes() == source_bytes
    validate_offline_lock(first_root / lock_relative, first_root / "backend" / "wheels")
    validate_offline_lock(second_root / lock_relative, second_root / "backend" / "wheels")


def test_release_fixture_is_byte_deterministic_across_two_builds(tmp_path: Path) -> None:
    first_root = _materialize_release_fixture(tmp_path / "first")
    second_root = _materialize_release_fixture(tmp_path / "second")
    assert _tree_bytes(first_root) == _tree_bytes(second_root)

    first = verify_package(first_root)
    second = verify_package(second_root)
    assert first.package_hash == second.package_hash
    assert first.release_id == second.release_id
    assert first.files_sha256 == second.files_sha256

    first_archive = tmp_path / "first.ppplugin"
    second_archive = tmp_path / "second.ppplugin"
    _build_ppplugin(first_root, first_archive)
    _build_ppplugin(second_root, second_archive)
    assert first_archive.read_bytes() == second_archive.read_bytes()

    plugin_wheel = first_root / "backend" / "plotpilot_prompt_skill_runtime-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(plugin_wheel) as archive:
        metadata = archive.read("plotpilot_prompt_skill_runtime-1.0.0.dist-info/METADATA").decode("utf-8")
    assert "Requires-Dist: plotpilot-plugin-sdk==0.1.0\n" in metadata


def test_release_and_lock_verifiers_reject_tamper_missing_and_non_wheel_inputs(tmp_path: Path) -> None:
    base = _materialize_release_fixture(tmp_path / "base")

    tampered = tmp_path / "tampered"
    shutil.copytree(base, tampered)
    plugin_wheel = tampered / "backend" / "plotpilot_prompt_skill_runtime-1.0.0-py3-none-any.whl"
    wheel_bytes = plugin_wheel.read_bytes()
    plugin_wheel.write_bytes(wheel_bytes[:-1] + bytes([wheel_bytes[-1] ^ 1]))
    with pytest.raises(PackageError, match="files.sha256|hash|manifest"):
        verify_package(tampered)

    missing_manifest = tmp_path / "missing-manifest"
    shutil.copytree(base, missing_manifest)
    (missing_manifest / "files.sha256").unlink()
    with pytest.raises(PackageError, match="files.sha256"):
        verify_package(missing_manifest)

    missing_wheel = tmp_path / "missing-wheel"
    shutil.copytree(base, missing_wheel)
    (missing_wheel / "backend" / "plotpilot_prompt_skill_runtime-1.0.0-py3-none-any.whl").unlink()
    with pytest.raises(PackageError, match="files.sha256|manifest|backend.wheel|hash"):
        verify_package(missing_wheel)

    non_wheel = tmp_path / "non-wheel"
    shutil.copytree(base, non_wheel)
    (non_wheel / "backend" / "wheels" / "README.txt").write_text("not a wheel\n", encoding="utf-8")
    _refresh_files_manifest(non_wheel)
    with pytest.raises(PackageError, match="wheelhouse"):
        verify_package(non_wheel)

    missing_dependency = tmp_path / "missing-dependency"
    shutil.copytree(base, missing_dependency)
    (missing_dependency / "backend" / "wheels" / "attrs-26.1.0-py3-none-any.whl").unlink()
    with pytest.raises(ContractError, match="satisfied by verified local"):
        validate_offline_lock(missing_dependency / "requirements.lock", missing_dependency / "backend" / "wheels")

    bad_lock = tmp_path / "bad-lock"
    shutil.copytree(base, bad_lock)
    lock = bad_lock / "requirements.lock"
    lock.write_text(
        re.sub(
            r"(?<=--hash=sha256:)[0-9a-f]{64}",
            "0" * 64,
            lock.read_text(encoding="utf-8"),
            count=1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="satisfied by verified local"):
        validate_offline_lock(lock, bad_lock / "backend" / "wheels")


def test_real_release_is_verifiable_offline_and_bootstraps_an_isolated_worker(tmp_path: Path) -> None:
    release_root = _materialize_release_fixture(tmp_path)

    verified = verify_package(release_root)
    archive_path = tmp_path / "prompt-skill-runtime.ppplugin"
    _build_ppplugin(release_root, archive_path)
    archived = verify_package(archive_path)
    assert archived.package_hash == verified.package_hash
    assert archived.release_id == verified.release_id
    assert archived.files_sha256 == verified.files_sha256

    manifest = json.loads((release_root / "plugin.json").read_text(encoding="utf-8"))
    source_lock = _manifest_lock_resource()
    release_lock = release_root / manifest["backend"]["requirements_lock"]
    assert release_lock.read_bytes() == source_lock.read_bytes()
    validate_offline_lock(release_lock, release_root / "backend" / "wheels")
    migration = json.loads((release_root / "migrations" / "manifest.json").read_text(encoding="utf-8"))
    working_root = tmp_path / "working"
    working_root.mkdir()
    venv_parent = Path(
        tempfile.mkdtemp(
            prefix="ppa-venv-",
            dir=(os.environ.get("SystemDrive") or "C:").rstrip("\\/") + "\\" if os.name == "nt" else None,
        )
    )
    venv_root = venv_parent / verified.plugin_id / verified.package_hash
    python_executable = venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    route = RuntimeRoute(
        worker_id="fixture-worker",
        plugin_id=verified.plugin_id,
        version=verified.version,
        generation_id="fixture-generation",
        release_id=verified.release_id,
        package_hash=verified.package_hash,
        data_generation_id=None,
        retire_epoch=1,
        package_root=release_root,
        venv_root=venv_root,
        working_root=working_root,
        private_data_root=None,
        python_executable=python_executable,
        entrypoint=manifest["backend"]["entrypoint"],
        ui_entry=None,
        ui_bundle_hash=None,
    )
    try:
        installed_python = OfflineVenvProvisioner(base_python=Path(sys.executable)).ensure(route, verified)
    except BaseException:
        # Provisioning can fail before the process-exchange ``finally`` below
        # is entered.  Do not leave a failed content-addressed venv on the
        # host (or make a later retry appear to be a stale valid install).
        shutil.rmtree(venv_parent, ignore_errors=True)
        raise
    assert installed_python == python_executable.resolve()

    migration_material = subprocess.run(
        [
            str(installed_python),
            "-I",
            "-c",
            "from plotpilot_prompt_skill_runtime.persistence import _migration_material; print(_migration_material()[1])",
        ],
        cwd=str(working_root),
        env=isolated_environment(installed_python),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
    )
    assert migration_material.stdout.decode("ascii").strip() == migration["steps"][0]["sha256"]

    bootstrap = ROOT / "backend" / "plotpilot_core" / "supervisor" / "worker_bootstrap.py"
    process: subprocess.Popen[bytes] | None = None

    def exchange(request: dict[str, object]) -> dict[str, object]:
        assert process is not None and process.stdin is not None
        process.stdin.write(encode_frame(request))
        process.stdin.flush()
        response = _read_child_frame(process)
        validate_rpc_response(response, request=request)
        return response

    try:
        process = subprocess.Popen(
            [str(installed_python), "-I", "-u", str(bootstrap), manifest["backend"]["entrypoint"]],
            cwd=str(working_root),
            env=isolated_environment(installed_python),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
        )
        handshake = exchange(
            build_request(
                "runtime.handshake",
                {
                    "host_protocol": "1",
                    "generation_id": "fixture-generation",
                    "plugin_release_id": verified.release_id,
                    "data_generation_id": None,
                },
                build_meta(
                    "control",
                    generation_id="fixture-generation",
                    plugin_release_id=verified.release_id,
                    deadline_at="2026-09-01T00:00:00Z",
                    operation_id="fixture-handshake",
                ),
                request_id="123e4567-e89b-12d3-a456-426614174100",
            )
        )
        assert handshake["result"]["plugin_id"] == verified.plugin_id  # type: ignore[index]

        health = exchange(
            build_request(
                "runtime.health",
                {"probe_id": "fixture-probe", "db_lease_id": None, "db_lease_epoch": None, "owner_instance_id": None},
                build_meta(
                    "control",
                    generation_id="fixture-generation",
                    plugin_release_id=verified.release_id,
                    deadline_at="2026-09-01T00:00:00Z",
                    operation_id="fixture-health",
                ),
                request_id="123e4567-e89b-12d3-a456-426614174102",
            )
        )
        assert health["result"]["status"] == "ok"  # type: ignore[index]
        shutdown = exchange(
            build_request(
                "runtime.shutdown",
                {"reason": "fixture-test", "deadline_at": "2026-09-01T00:00:00Z"},
                build_meta(
                    "control",
                    generation_id="fixture-generation",
                    plugin_release_id=verified.release_id,
                    deadline_at="2026-09-01T00:00:00Z",
                    operation_id="fixture-shutdown",
                ),
                request_id="123e4567-e89b-12d3-a456-426614174103",
            )
        )
        assert shutdown["result"]["accepted"] is True  # type: ignore[index]
        assert process.poll() is None
    finally:
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if process.poll() is None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
            assert process.stderr is not None
            stderr = process.stderr.read()
            assert process.returncode == 0, stderr.decode("utf-8", "replace")
            assert stderr == b""
        shutil.rmtree(venv_parent, ignore_errors=True)
