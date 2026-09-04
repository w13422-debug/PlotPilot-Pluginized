from __future__ import annotations

import ast
import json
import re
import shutil
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).resolve().parents[3]

LOCK_ENTRIES = {
    "attrs": (
        "26.1.0",
        "ef619faa51e446092512f9026dc8f03e370988635f3bca13a5464f3d196b9551",
    ),
    "jsonschema": (
        "4.26.0",
        "6f3e27fd3e27051319195e2e9a42212413242ffef4f46d00356eba45993a5c77",
    ),
    "jsonschema-specifications": (
        "2025.9.1",
        "71e4c4430d70c48f081d702cab84e5d8e3949fad23865932c06494e1b65da841",
    ),
    "plotpilot-plugin-sdk": (
        "0.1.0",
        "61f61bc332a7b5dfe31a6209f15d296e9e4ce8d72e40cba591dfdb09aa1dc120",
    ),
    "referencing": (
        "0.37.0",
        "731bdff179bde892adb919278c9292645006684ce96e67583087ee0b96aab52c",
    ),
    "rpds-py": (
        "2026.6.3",
        "addd2e232ccd57e8cf520d3e3d8e6256561dd5e8de1a191d50a0e3df9a126afb",
    ),
    "rfc8785": (
        "0.1.4",
        "ac4c0f2ba7fe3f4c05612bb35fc10fd3ef689cd32c13aaf7dc31e0315f10bb37",
    ),
    "typing-extensions": (
        "4.16.0",
        "f12a230d3c7ecc72c766807836c26ef0d96df0e9fbb9e18b40e3f6385ec3cb64",
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
    assert project["dependencies"] == ["plotpilot-plugin-sdk==0.1.0"]
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


def test_both_locks_are_byte_identical_to_the_accepted_offline_closure() -> None:
    accepted = (
        ROOT / "first-party-plugins" / "prompt-skill-runtime" / "requirements.lock"
    ).read_bytes()
    for plugin in PLUGINS:
        root = plugin["root"]
        assert isinstance(root, Path)
        assert (root / "backend" / "requirements.lock").read_bytes() == accepted


@pytest.mark.parametrize(
    "mutation",
    [
        "remove_dependency",
        "remove_hash",
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
