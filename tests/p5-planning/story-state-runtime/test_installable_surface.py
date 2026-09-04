from __future__ import annotations

import ast
import json
import re
import shutil
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).resolve().parents[3]
SDK_VERSION = "0.1.2"
SDK_HASH = "8510991547e84fdc6f9fcfb246858747d93390c2d02e6015d9e0b77c961dc9c4"
SDK_REQUIREMENT = f"plotpilot-plugin-sdk=={SDK_VERSION}"

LOCK_ENTRIES = {
    "attrs": (
        "26.1.0",
        "c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043ac309",
    ),
    "jsonschema": (
        "4.26.0",
        "d489f15263b8d200f8387e64b4c3a75f06629559fb73deb8fdfb525f2dab50ce",
    ),
    "jsonschema-specifications": (
        "2025.9.1",
        "98802fee3a11ee76ecaca44429fda8a41bff98b00a0f2838151b113f210cc6fe",
    ),
    "plotpilot-plugin-sdk": (
        SDK_VERSION,
        SDK_HASH,
    ),
    "referencing": (
        "0.37.0",
        "381329a9f99628c9069361716891d34ad94af76e461dcb0335825aecc7692231",
    ),
    "rpds-py": (
        "2026.6.3",
        "2c958bf94822e9290a40aaf2a822d4bc5c88099093e3948ad6c571eca9272e5f",
    ),
    "rfc8785": (
        "0.1.4",
        "520d690b448ecf0703691c76e1a34a24ddcd4fc5bc41d589cb7c58ec651bcd48",
    ),
    "typing-extensions": (
        "4.16.0",
        "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8",
    ),
}

PLUGINS = (
    {
        "root": ROOT / "first-party-plugins" / "project-planner",
        "distribution": "plotpilot-project-planner",
        "package": "plotpilot_project_planner",
        "plugin_id": "com.plotpilot.project-planner",
    },
    {
        "root": ROOT / "first-party-plugins" / "story-state",
        "distribution": "plotpilot-story-state",
        "package": "plotpilot_story_state",
        "plugin_id": "com.plotpilot.story-state",
    },
)


def _normalise_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_lock(path: Path) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    pattern = re.compile(
        r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)=="
        r"(?P<version>[A-Za-z0-9][A-Za-z0-9._+!-]*)"
        r"(?P<hashes>(?: --hash=sha256:[0-9a-f]{64})+)"
    )
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.fullmatch(line)
        assert match is not None, f"not a closed hash-pinned lock line: {line!r}"
        name = _normalise_distribution(match.group("name"))
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", match.group("hashes"))
        assert len(hashes) == 1, f"lock entry must have one verified hash: {line!r}"
        assert name not in entries, f"duplicate lock distribution: {name}"
        entries[name] = (match.group("version"), hashes[0])
    return entries


def _assert_no_argument_entrypoint(source: Path, callable_name: str) -> None:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    definitions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == callable_name
    ]
    assert len(definitions) == 1, (
        f"entrypoint callable is not uniquely defined: {callable_name}"
    )
    function = definitions[0]
    positional = [*function.args.posonlyargs, *function.args.args]
    required_positional = len(positional) - len(function.args.defaults)
    required_keyword_only = sum(
        default is None for default in function.args.kw_defaults
    )
    assert required_positional == 0, "entrypoint requires positional arguments"
    assert required_keyword_only == 0, "entrypoint requires keyword-only arguments"


def _read_sdk_pin(root: Path) -> tuple[str, str]:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    sdk_dependencies = [
        dependency
        for dependency in dependencies
        if dependency.startswith("plotpilot-plugin-sdk")
    ]
    assert len(sdk_dependencies) == 1, "package metadata must declare the SDK once"
    metadata_match = re.fullmatch(
        r"plotpilot-plugin-sdk==(?P<version>[A-Za-z0-9][A-Za-z0-9._+!-]*)",
        sdk_dependencies[0],
    )
    assert metadata_match is not None, "package metadata SDK dependency is not exact"

    lock = _parse_lock(root / "backend" / "requirements.lock")
    assert "plotpilot-plugin-sdk" in lock, "requirements lock omits the SDK"
    lock_version, lock_hash = lock["plotpilot-plugin-sdk"]
    metadata_version = metadata_match.group("version")
    assert metadata_version == lock_version, "package metadata and SDK lock disagree"
    return lock_version, lock_hash


def _assert_exact_sdk_pins(roots: list[Path]) -> None:
    pins = [_read_sdk_pin(root) for root in roots]
    assert len(set(pins)) == 1, "Project Planner and Story State SDK pins disagree"
    assert pins == [(SDK_VERSION, SDK_HASH)] * len(roots), (
        "package SDK pin differs from the deterministic wheel authority"
    )


def _assert_plugin_source(plugin: dict[str, object]) -> None:
    root = plugin["root"]
    assert isinstance(root, Path)
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    backend = manifest["backend"]
    distribution = plugin["distribution"]
    package = plugin["package"]
    plugin_id = plugin["plugin_id"]
    assert isinstance(distribution, str) and isinstance(package, str)
    assert isinstance(plugin_id, str)

    assert pyproject["build-system"] == {
        "requires": ["setuptools>=68", "wheel"],
        "build-backend": "setuptools.build_meta",
    }
    assert project["name"] == distribution, (
        "distribution identity differs from the accepted package source"
    )
    assert project["version"] == manifest["version"] == "1.0.0", (
        "exact version identity drifted"
    )
    assert manifest["plugin_id"] == plugin_id
    assert project["requires-python"] == ">=3.12,<3.13"
    assert project["dependencies"] == [SDK_REQUIREMENT]
    assert pyproject["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]

    entrypoint = backend["entrypoint"]
    assert entrypoint == f"{package}.worker:main"
    module_name, callable_name = entrypoint.split(":")
    source = root / "src" / Path(*module_name.split("."))
    source = source.with_suffix(".py")
    assert source.is_file(), f"entrypoint source is missing: {source}"
    _assert_no_argument_entrypoint(source, callable_name)

    wheel = backend["wheel"]
    assert isinstance(wheel, str)
    wheel_path = Path(*wheel.split("/"))
    assert not wheel_path.is_absolute() and ".." not in wheel_path.parts
    assert wheel_path.parts == ("backend", wheel_path.name)
    expected_stem = f"{distribution.replace('-', '_')}-{manifest['version']}-"
    assert wheel_path.name.startswith(expected_stem)
    assert wheel_path.name.endswith("-py3-none-any.whl")
    assert _normalise_distribution(
        wheel_path.name.split("-1.0.0-", 1)[0]
    ) == _normalise_distribution(project["name"])
    assert backend["requirements_lock"] == "backend/requirements.lock"
    assert backend["wheelhouse"] == "backend/wheels"

    lock = _parse_lock(root / backend["requirements_lock"])
    assert lock == LOCK_ENTRIES, "complete dependency closure or verified hash drifted"


@pytest.mark.parametrize("plugin", PLUGINS, ids=["project-planner", "story-state"])
def test_source_package_identity_and_no_argument_entrypoint(
    plugin: dict[str, object],
) -> None:
    _assert_plugin_source(plugin)


def test_both_package_sources_bind_the_deterministic_sdk_wheel() -> None:
    roots: list[Path] = []
    lock_bytes: list[bytes] = []
    parsed_locks: list[dict[str, tuple[str, str]]] = []
    for plugin in PLUGINS:
        root = plugin["root"]
        assert isinstance(root, Path)
        roots.append(root)
        lock_bytes.append((root / "backend" / "requirements.lock").read_bytes())
        parsed = _parse_lock(root / "backend" / "requirements.lock")
        assert len(parsed) == 8
        assert tuple(parsed.items()) == tuple(LOCK_ENTRIES.items())
        parsed_locks.append(parsed)
    _assert_exact_sdk_pins(roots)
    assert len(set(lock_bytes)) == 1
    assert parsed_locks[0] == parsed_locks[1]


@pytest.mark.parametrize(
    "mutation",
    [
        "sdk_0_1_1",
        "unpinned_sdk",
        "wrong_hash",
        "duplicate_sdk",
        "planner_story_disagreement",
    ],
)
def test_sdk_pin_drift_is_rejected(tmp_path: Path, mutation: str) -> None:
    roots: list[Path] = []
    for plugin in PLUGINS:
        source = plugin["root"]
        assert isinstance(source, Path)
        root = tmp_path / source.name
        (root / "backend").mkdir(parents=True)
        for relative in ("pyproject.toml", "backend/requirements.lock"):
            (root / relative).write_bytes((source / relative).read_bytes())
        roots.append(root)

    planner, story = roots
    target = planner
    if mutation == "sdk_0_1_1":
        for relative in ("pyproject.toml", "backend/requirements.lock"):
            path = target / relative
            path.write_text(
                path.read_text(encoding="utf-8").replace("0.1.2", "0.1.1"),
                encoding="utf-8",
            )
    elif mutation == "unpinned_sdk":
        path = target / "pyproject.toml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                SDK_REQUIREMENT, "plotpilot-plugin-sdk>=0.1.2"
            ),
            encoding="utf-8",
        )
    elif mutation == "wrong_hash":
        path = target / "backend" / "requirements.lock"
        path.write_text(
            path.read_text(encoding="utf-8").replace(SDK_HASH, "0" * 64),
            encoding="utf-8",
        )
    elif mutation == "duplicate_sdk":
        path = target / "backend" / "requirements.lock"
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{SDK_REQUIREMENT} --hash=sha256:{SDK_HASH}\n")
    else:
        for relative in ("pyproject.toml", "backend/requirements.lock"):
            path = story / relative
            path.write_text(
                path.read_text(encoding="utf-8").replace("0.1.2", "0.1.1"),
                encoding="utf-8",
            )

    with pytest.raises(AssertionError):
        _assert_exact_sdk_pins(roots)


@pytest.mark.parametrize(
    "mutation",
    [
        "remove_dependency",
        "remove_hash",
        "wrong_third_party_hash",
        "duplicate_third_party",
        "third_party_version_drift",
        "wrong_version",
        "wrong_wheel",
        "wrong_entrypoint",
    ],
)
def test_source_validation_rejects_package_identity_and_lock_drift(
    tmp_path: Path, mutation: str
) -> None:
    source = PLUGINS[0]
    root = tmp_path / "project-planner"
    for relative in ("plugin.json", "pyproject.toml", "backend/requirements.lock"):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source["root"] / relative).read_bytes())
    source_module = root / "src" / "plotpilot_project_planner" / "worker.py"
    source_module.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        source["root"] / "src" / "plotpilot_project_planner" / "worker.py",
        source_module,
    )

    if mutation == "remove_dependency":
        lock = root / "backend" / "requirements.lock"
        lock.write_text(
            "\n".join(
                line
                for line in lock.read_text(encoding="utf-8").splitlines()
                if not line.startswith("attrs==")
            )
            + "\n",
            encoding="utf-8",
        )
    elif mutation == "remove_hash":
        lock = root / "backend" / "requirements.lock"
        lock.write_text(
            re.sub(
                r" --hash=sha256:[0-9a-f]{64}",
                "",
                lock.read_text(encoding="utf-8"),
                count=1,
            ),
            encoding="utf-8",
        )
    elif mutation == "wrong_third_party_hash":
        lock = root / "backend" / "requirements.lock"
        lock.write_text(
            lock.read_text(encoding="utf-8").replace(
                LOCK_ENTRIES["attrs"][1], "0" * 64
            ),
            encoding="utf-8",
        )
    elif mutation == "duplicate_third_party":
        lock = root / "backend" / "requirements.lock"
        version, digest = LOCK_ENTRIES["attrs"]
        with lock.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"attrs=={version} --hash=sha256:{digest}\n")
    elif mutation == "third_party_version_drift":
        lock = root / "backend" / "requirements.lock"
        lock.write_text(
            lock.read_text(encoding="utf-8").replace("attrs==26.1.0", "attrs==26.0.0"),
            encoding="utf-8",
        )
    elif mutation == "wrong_version":
        pyproject = root / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text(encoding="utf-8").replace(
                'version = "1.0.0"', 'version = "1.0.1"'
            ),
            encoding="utf-8",
        )
    elif mutation == "wrong_wheel":
        manifest = root / "plugin.json"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "plotpilot_project_planner-1.0.0", "other-1.0.0"
            ),
            encoding="utf-8",
        )
    else:
        manifest = root / "plugin.json"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "plotpilot_project_planner.worker:main",
                "plotpilot_project_planner.worker:missing",
            ),
            encoding="utf-8",
        )

    mutated = {**source, "root": root}
    with pytest.raises((AssertionError, KeyError)):
        _assert_plugin_source(mutated)


def test_source_check_does_not_create_forbidden_package_artifacts() -> None:
    forbidden = {".whl", ".ppplugin", "files.sha256"}
    before = {
        path.relative_to(ROOT)
        for path in ROOT.glob("**/*")
        if path.is_file() and (path.suffix in forbidden or path.name in forbidden)
    }
    for plugin in PLUGINS:
        _assert_plugin_source(plugin)
    after = {
        path.relative_to(ROOT)
        for path in ROOT.glob("**/*")
        if path.is_file() and (path.suffix in forbidden or path.name in forbidden)
    }
    assert after == before
    assert all(
        not path.exists()
        for plugin in PLUGINS
        for path in (plugin["root"] / "backend" / "wheels",)
    )
