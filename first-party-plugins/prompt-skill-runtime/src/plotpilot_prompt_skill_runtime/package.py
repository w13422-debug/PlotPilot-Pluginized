"""Immutable Prompt/Skill package loading and v1 identity.

Only the public P0 package helpers are used for digest calculation.  This
module adds the small amount of release-domain policy that the SDK
deliberately leaves to a first-party runtime: strict ``skill.json`` loading,
legacy PlotPilot prompt-package compatibility, folder/ZIP reading and
read-only package objects.
"""

from __future__ import annotations

import ast
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from plotpilot_plugin_sdk import (
    ContractError,
    ContractValidationError,
    assert_valid,
    build_files_sha256,
    parse_json_bytes,
    sha256_hex,
    skill_package_hash,
    skill_release_id,
    verify_package_manifest,
    verify_skill_identity as _sdk_verify_skill_identity,
)
from plotpilot_plugin_sdk.package import normalize_relative_path


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")

# A Skill is data interpreted by the Runtime, never an executable plugin.
# This is intentionally a bounded policy rather than an antivirus scanner.
_EXECUTABLE_SUFFIXES = {
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".exe",
    ".msi",
    ".pyd",
    ".py",
    ".pyc",
    ".ps1",
    ".sh",
    ".so",
    ".whl",
}
_EXECUTABLE_SEGMENTS = {"__pycache__", ".git", "backend", "bin", "node_modules"}


def _strict_text(data: bytes, *, name: str) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        raise ContractValidationError(f"{name} must not contain a UTF-8 BOM")
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractValidationError(f"{name} must be UTF-8") from exc


def _yaml_scalar(raw: str) -> Any:
    """Parse the small scalar subset used by PlotPilot package metadata.

    Existing PlotPilot prompt packages use simple mappings/lists.  Keeping
    this parser dependency-free means installing a Skill never needs online
    package resolution or a root dependency change.
    """

    value = raw.strip()
    if value in {"", "~", "null", "Null", "NULL"}:
        return None if value else ""
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if (value.startswith("'") and value.endswith("'")) or (
        value.startswith('"') and value.endswith('"')
    ):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        # YAML's unquoted list values are close enough to a CSV for package
        # metadata.  Quoted JSON/Python lists are handled first.
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, list):
                return parsed
        except (SyntaxError, ValueError):
            pass
        return [_yaml_scalar(item) for item in value[1:-1].split(",") if item.strip()]
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def _parse_package_yaml(data: bytes) -> dict[str, Any]:
    """Parse a bounded package.yaml mapping without importing PyYAML."""

    text = _strict_text(data, name="package.yaml")
    result: dict[str, Any] = {}
    # Prompt Package metadata in the donor is a flat mapping for identity;
    # preserve indented nested blocks as scalar continuation instead of
    # accepting arbitrary code-like YAML constructs.
    last_key: str | None = None
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line[:1].isspace():
            if last_key and isinstance(result.get(last_key), str):
                result[last_key] = f"{result[last_key]} {stripped}".strip()
                continue
            raise ContractValidationError("package.yaml contains unsupported indentation")
        if ":" not in stripped:
            raise ContractValidationError("package.yaml mapping entry is missing ':'")
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        if not key:
            raise ContractValidationError("package.yaml key must not be blank")
        result[key] = _yaml_scalar(raw_value)
        last_key = key
    return result


def _manifest_from_files(files: Mapping[str, bytes]) -> dict[str, Any]:
    data = files.get("skill.json")
    if data is not None:
        parsed = parse_json_bytes(data)
        if not isinstance(parsed, dict):
            raise ContractValidationError("skill.json must contain an object")
        manifest = dict(parsed)
        assert_valid("plotpilot-skill/v1", manifest)
        return manifest

    # The pre-plugin PlotPilot Prompt Package format uses package.yaml plus
    # system.md/user.md.  It has no public Skill manifest, so map only its
    # identity fields to the frozen v1 manifest.  The original bytes remain in
    # the package file map and therefore still participate in the digest.
    metadata = files.get("package.yaml")
    if metadata is None:
        raise ContractValidationError("Skill package must contain skill.json")
    raw = _parse_package_yaml(metadata)
    skill_id = raw.get("skill_id", raw.get("id"))
    version = raw.get("version")
    display_name = raw.get("display_name", raw.get("name"))
    stage = raw.get("stage", "draft")
    actions = raw.get("actions", ["run"])
    manifest = {
        "schema": "plotpilot-skill/v1",
        "skill_id": skill_id,
        "version": version,
        "display_name": display_name,
        "stage": stage,
        "actions": actions,
    }
    assert_valid("plotpilot-skill/v1", manifest)
    return manifest


def _reject_executable_payload(files: Mapping[str, bytes]) -> None:
    for raw_path in files:
        normalized = normalize_relative_path(raw_path)
        segments = set(normalized.split("/"))
        if segments & _EXECUTABLE_SEGMENTS or Path(normalized).suffix.lower() in _EXECUTABLE_SUFFIXES:
            raise ContractError(
                1005,
                "Skill packages cannot contain executable payloads",
                path=normalized,
            )


def _normalise_files(files: Mapping[str, bytes]) -> tuple[dict[str, bytes], bytes | None]:
    if not isinstance(files, Mapping):
        raise ContractValidationError("Skill package files must be a mapping")
    material: dict[str, bytes] = {}
    supplied_manifest: bytes | None = None
    for raw_path, content in files.items():
        if not isinstance(raw_path, str) or not isinstance(content, bytes):
            raise ContractValidationError("Skill package paths must map to raw bytes")
        path = normalize_relative_path(raw_path)
        if path == "files.sha256":
            if supplied_manifest is not None:
                raise ContractValidationError("Skill package contains duplicate files.sha256")
            supplied_manifest = content
            continue
        if path in material:
            raise ContractValidationError(f"Skill package contains duplicate path: {path}")
        material[path] = bytes(content)
    _reject_executable_payload(material)
    if "skill.json" not in material and "package.yaml" not in material:
        raise ContractValidationError("Skill package must contain skill.json or package.yaml")
    return material, supplied_manifest


@dataclass(frozen=True, slots=True)
class SkillIdentity:
    """Content-addressed Skill release identity."""

    skill_id: str
    version: str
    package_hash: str
    release_id: str
    files_sha256: bytes

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.skill_id):
            raise ContractValidationError("skill_id is not a v1 namespaced ID")
        if not _HASH.fullmatch(self.package_hash) or not _HASH.fullmatch(self.release_id):
            raise ContractValidationError("Skill identity hashes must be lowercase SHA-256")
        if not isinstance(self.files_sha256, bytes) or not self.files_sha256.endswith(b"\n"):
            raise ContractValidationError("files_sha256 must be UTF-8 LF bytes")

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "skill_package_hash": self.package_hash,
            "skill_release_id": self.release_id,
            "files_sha256": self.files_sha256.decode("utf-8"),
        }

    # Common aliases make the identity explicit at call sites without
    # introducing a second hash formula.
    @property
    def skill_package_hash(self) -> str:
        return self.package_hash

    @property
    def skill_release_id(self) -> str:
        return self.release_id


def calculate_skill_identity(
    files: Mapping[str, bytes],
    skill_id: str,
    version: str,
    *,
    expected_files_sha256: bytes | None = None,
) -> SkillIdentity:
    """Calculate and optionally verify the frozen v1 Skill identity."""

    material, supplied_manifest = _normalise_files(files)
    manifest = build_files_sha256(material)
    expected = expected_files_sha256 if expected_files_sha256 is not None else supplied_manifest
    if expected is not None:
        verify_package_manifest(material, expected)
        if manifest != expected:
            raise ContractValidationError("Skill files.sha256 bytes mismatch")
    digest = skill_package_hash(material)
    return SkillIdentity(skill_id, version, digest, skill_release_id(skill_id, version, digest), manifest)


@dataclass(frozen=True, slots=True)
class SkillPackage:
    """Read-only materialized Skill package and its identity."""

    manifest: Mapping[str, Any]
    files: Mapping[str, bytes]
    identity: SkillIdentity
    supplied_files_sha256: bytes | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest", MappingProxyType(dict(self.manifest)))
        object.__setattr__(self, "files", MappingProxyType({k: bytes(v) for k, v in self.files.items()}))

    @classmethod
    def from_files(
        cls,
        files: Mapping[str, bytes],
        *,
        expected_package_hash: str | None = None,
        expected_release_id: str | None = None,
        expected_files_sha256: bytes | None = None,
    ) -> "SkillPackage":
        material, supplied_manifest = _normalise_files(files)
        # Every loadable package must carry the canonical control manifest.
        # ``calculate_skill_identity`` remains usable for callers that are
        # explicitly constructing an identity from an already verified map,
        # but package materialization never accepts an implicit manifest.
        if supplied_manifest is None and expected_files_sha256 is None:
            raise ContractValidationError("Skill package requires files.sha256")
        manifest = _manifest_from_files(material)
        # Validate an ordinary dictionary before SkillPackage.__post_init__
        # freezes it with MappingProxyType.  jsonschema deliberately expects
        # a plain mapping and this also keeps the validation order explicit.
        assert_valid("plotpilot-skill/v1", dict(manifest))
        identity = calculate_skill_identity(
            material,
            manifest["skill_id"],
            manifest["version"],
            expected_files_sha256=expected_files_sha256 or supplied_manifest,
        )
        package = cls(manifest, material, identity, supplied_manifest)
        package.verify(
            expected_package_hash=expected_package_hash,
            expected_release_id=expected_release_id,
            expected_files_sha256=expected_files_sha256 or supplied_manifest,
        )
        return package

    @classmethod
    def from_directory(cls, root: str | Path) -> "SkillPackage":
        base = Path(root)
        if not base.is_dir():
            raise FileNotFoundError(f"Skill package directory not found: {base}")
        files: dict[str, bytes] = {}
        for entry in base.rglob("*"):
            if entry.is_dir():
                continue
            if entry.is_symlink():
                raise ContractError(1005, "Skill package symlinks are not allowed", path=str(entry))
            relative = entry.relative_to(base).as_posix()
            files[relative] = entry.read_bytes()
        return cls.from_files(files)

    @classmethod
    def from_zip(cls, archive: str | Path) -> "SkillPackage":
        with zipfile.ZipFile(archive, "r") as handle:
            files: dict[str, bytes] = {}
            for info in handle.infolist():
                if info.is_dir():
                    continue
                # Unix mode 0120000 denotes a symbolic link.  Do not resolve
                # links while reading an untrusted archive.
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ContractError(1005, "Skill package ZIP symlinks are not allowed", path=info.filename)
                if info.filename in files:
                    raise ContractValidationError(f"Skill ZIP contains duplicate path: {info.filename}")
                files[info.filename] = handle.read(info)
        return cls.from_files(files)

    @classmethod
    def load(cls, source: str | Path) -> "SkillPackage":
        path = Path(source)
        if path.is_dir():
            return cls.from_directory(path)
        if path.is_file() and zipfile.is_zipfile(path):
            return cls.from_zip(path)
        raise ContractError(1005, "Skill package source must be a folder or ZIP", path=str(path))

    @property
    def skill_id(self) -> str:
        return self.manifest["skill_id"]

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def package_hash(self) -> str:
        return self.identity.package_hash

    @property
    def release_id(self) -> str:
        return self.identity.release_id

    @property
    def prompt_text(self) -> str:
        """Return the package prompt without evaluating arbitrary templates."""

        if "prompt.txt" in self.files:
            return _strict_text(self.files["prompt.txt"], name="prompt.txt")
        parts: list[str] = []
        for name in ("system.md", "user.md"):
            if name in self.files:
                parts.append(_strict_text(self.files[name], name=name))
        return "\n".join(parts)

    def render_prompt(self, values: Mapping[str, object] | None = None) -> str:
        """Perform deterministic ``{{key}}`` substitution only.

        No expression, attribute, include or function evaluation is allowed;
        unknown placeholders are retained, which keeps package output
        reproducible and lets a later Core layer decide whether a variable is
        available.
        """

        text = self.prompt_text
        if not values:
            return text
        normalized = {str(k): str(v) for k, v in values.items()}
        return re.sub(r"\{\{\s*([A-Za-z0-9_.:/-]+)\s*\}\}", lambda m: normalized.get(m.group(1), m.group(0)), text)

    def verify(
        self,
        *,
        expected_package_hash: str | None = None,
        expected_release_id: str | None = None,
        expected_files_sha256: bytes | None = None,
    ) -> None:
        expected_hash = expected_package_hash or self.package_hash
        expected_release = expected_release_id or self.release_id
        _sdk_verify_skill_identity(
            self.files,
            self.skill_id,
            self.version,
            expected_hash,
            expected_release,
            expected_files_sha256=expected_files_sha256,
        )
        # Rehydrate the frozen view as a plain dict for schema validation.
        assert_valid("plotpilot-skill/v1", dict(self.manifest))


@dataclass(frozen=True, slots=True)
class PromptPackage:
    """Compatibility view over a PlotPilot Prompt Package/Skill package."""

    skill: SkillPackage

    @classmethod
    def load(cls, source: str | Path) -> "PromptPackage":
        return cls(SkillPackage.load(source))

    @classmethod
    def from_files(cls, files: Mapping[str, bytes]) -> "PromptPackage":
        return cls(SkillPackage.from_files(files))

    @property
    def manifest(self) -> Mapping[str, Any]:
        return self.skill.manifest

    @property
    def prompt_text(self) -> str:
        return self.skill.prompt_text

    def render(self, values: Mapping[str, object] | None = None) -> str:
        return self.skill.render_prompt(values)

    def verify(self) -> None:
        self.skill.verify()


def load_skill_package(source: str | Path) -> SkillPackage:
    return SkillPackage.load(source)


def verify_skill_golden(root: str | Path | None = None) -> SkillIdentity:
    """Recompute the repository's frozen cross-language Skill golden."""

    golden = Path(root) if root is not None else Path(__file__).resolve().parents[4] / "contracts" / "golden" / "skill"
    files = {
        "skill.json": (golden / "skill.json").read_bytes(),
        "prompt.txt": (golden / "prompt.txt").read_bytes(),
        "files.sha256": (golden / "files.sha256").read_bytes(),
    }
    expected = json.loads((golden / "expected.json").read_text(encoding="utf-8"))
    package = SkillPackage.from_files(files)
    expected_values = expected["expected_from_design"]
    package.verify(
        expected_package_hash=expected_values["skill_package_hash"],
        expected_release_id=expected_values["skill_release_id"],
        expected_files_sha256=files["files.sha256"],
    )
    if package.identity.as_dict()["files_sha256"] != expected["files_sha256"]:
        raise ContractValidationError("Skill golden files.sha256 text mismatch")
    return package.identity


# Name used by delivery/test reports.
verify_golden_skill = verify_skill_golden
