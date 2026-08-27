"""Offline PlotPilot plugin package reader and verifier.

This module is deliberately a thin control-plane adapter around the frozen
``plotpilot_plugin_sdk`` package and verifier.  It owns the parts which the
SDK cannot own: reading an untrusted folder/ZIP without following links or
writing files, validating the package layout/type gate, and producing an
immutable in-memory view for the package store.

The package format is intentionally not a signature or malware scanner.  A
package is accepted only when its ordinary-file tree, ``files.sha256``,
manifest, compatibility profile and calculated package/release identities all
agree.  ZIP metadata (timestamps, compression and member order) is not part
of the identity.
"""
from __future__ import annotations

import io
import os
import re
import stat
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO, Iterable, Mapping, MutableMapping, Sequence

from plotpilot_plugin_sdk.canonical import parse_json_bytes
from plotpilot_plugin_sdk.errors import ContractError, ContractValidationError, ErrorCode
from plotpilot_plugin_sdk.package import (
    build_files_sha256,
    digest_package,
    normalize_relative_path,
    unicode_nfc_casefold,
)
from plotpilot_plugin_sdk.verifier import (
    verify_manifest,
    verify_package_identity,
    verify_package_manifest,
)


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_DEFAULT_CORE_API = ">=1.0 <2.0"
_DEFAULT_PLUGIN_RPC = "1"
_DEFAULT_UI_HOST = "1"
_DEFAULT_PYTHON = "3.12.*"
_MANIFEST_NAME = "plugin.json"
_FILES_MANIFEST_NAME = "files.sha256"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400

# These limits are intentionally finite and boring.  They protect the host
# from an accidentally enormous archive without trying to become a scanner.
# Callers may lower (but not raise) them through PackageLimits when a product
# surface needs a stricter profile.
DEFAULT_MAX_FILES = 10_000
DEFAULT_MAX_FILE_SIZE = 64 * 1024 * 1024
DEFAULT_MAX_TOTAL_SIZE = 256 * 1024 * 1024
DEFAULT_MAX_PATH_LENGTH = 240

_EXECUTABLE_SUFFIXES = frozenset(
    {
        ".bat",
        ".cmd",
        ".com",
        ".cjs",
        ".dll",
        ".dylib",
        ".exe",
        ".js",
        ".jar",
        ".msi",
        ".mjs",
        ".pyd",
        ".py",
        ".pyc",
        ".ps1",
        ".scr",
        ".sh",
        ".so",
        ".vbs",
        ".wasm",
        ".whl",
    }
)


class PackageError(ContractError):
    """A package cannot be safely read or does not satisfy the v1 contract."""

    def __init__(
        self,
        message: str,
        *,
        code: int | ErrorCode = ErrorCode.ASSET_ERROR,
        path: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(code, message, path=path, details=details)


class PackagePathError(PackageError):
    """A package member is not in the frozen Windows relative-path profile."""


class PackageIntegrityError(PackageError):
    """A package digest, manifest or immutable snapshot does not match."""


class PackageTypeError(PackageError):
    """The package content does not satisfy its data/code type gate."""


class PackageCompatibilityError(PackageError):
    """The package is valid but cannot run against the selected v1 host."""

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(
            message,
            code=ErrorCode.INCOMPATIBLE_GENERATION,
            details=details,
        )


@dataclass(frozen=True)
class PackageLimits:
    """Stable input limits for folder/ZIP reading.

    ``max_path_length`` is also enforced by the SDK's frozen path validator.
    The field is kept here so a caller can describe the complete reader
    policy, and so a future contract revision can change it in one place.
    """

    max_files: int = DEFAULT_MAX_FILES
    max_file_size: int = DEFAULT_MAX_FILE_SIZE
    max_total_size: int = DEFAULT_MAX_TOTAL_SIZE
    max_path_length: int = DEFAULT_MAX_PATH_LENGTH

    def __post_init__(self) -> None:
        # These are policy inputs, not arbitrary arithmetic values.  In
        # particular, ``bool`` is an ``int`` subclass and floats such as
        # ``inf`` would otherwise pass the old positivity checks.  Refuse
        # subclasses as well as non-integers and do not permit a caller to
        # raise the frozen host limits.
        bounds = (
            ("max_files", self.max_files, DEFAULT_MAX_FILES),
            ("max_file_size", self.max_file_size, DEFAULT_MAX_FILE_SIZE),
            ("max_total_size", self.max_total_size, DEFAULT_MAX_TOTAL_SIZE),
            ("max_path_length", self.max_path_length, DEFAULT_MAX_PATH_LENGTH),
        )
        for name, value, upper in bounds:
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError(
                    f"{name} must be an integer in the range 1..{upper}"
                )


@dataclass(frozen=True)
class CompatibilityProfile:
    """The host compatibility values used by the v1 package gate."""

    core_api: str = _DEFAULT_CORE_API
    plugin_rpc: str = _DEFAULT_PLUGIN_RPC
    ui_host: str = _DEFAULT_UI_HOST
    python: str = _DEFAULT_PYTHON

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "CompatibilityProfile":
        if value is None:
            return cls()
        aliases = {
            "core_api": "core_api",
            "core_api_version": "core_api",
            "plugin_rpc": "plugin_rpc",
            "plugin_rpc_version": "plugin_rpc",
            "ui_host": "ui_host",
            "ui_host_version": "ui_host",
            "python": "python",
            "python_version": "python",
        }
        values: dict[str, str] = {}
        for key, target in aliases.items():
            if key in value:
                raw = value[key]
                if not isinstance(raw, str):
                    raise PackageCompatibilityError(f"compatibility value {key!r} must be a string")
                values[target] = raw
        return cls(**values)


@dataclass(frozen=True)
class PackageFiles:
    """A normalized package tree, excluding the generated files manifest."""

    files: Mapping[str, bytes]
    files_sha256: bytes


@dataclass(frozen=True)
class VerifiedPackage:
    """Verified package identity and an immutable in-memory file view."""

    plugin_id: str
    version: str
    kind: str
    package_hash: str
    release_id: str
    files_sha256: bytes
    manifest: Mapping[str, Any]
    files: Mapping[str, bytes]
    source: Path | None = None

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.plugin_id, self.version, self.package_hash

    @property
    def package_id(self) -> tuple[str, str, str]:
        """Alias used by package-store callers."""

        return self.identity

    @property
    def manifest_bytes(self) -> bytes:
        return self.files[_MANIFEST_NAME]

    def read_bytes(self, relative_path: str) -> bytes:
        """Read a member using the same canonical path profile as the verifier."""

        canonical = normalize_relative_path(relative_path)
        key = unicode_nfc_casefold(canonical)
        for path, content in self.files.items():
            if unicode_nfc_casefold(path) == key:
                return content
        raise FileNotFoundError(relative_path)


# Common spelling used by callers which prefer ``PackageVerification``.
PackageVerification = VerifiedPackage


def _error_from_contract(exc: ContractError, *, path: str | None = None) -> PackageError:
    return PackageError(str(exc), code=exc.code, path=path or exc.path, details=exc.details)


def _validate_limits(limits: PackageLimits | None) -> PackageLimits:
    return limits if limits is not None else PackageLimits()


def _is_reparse_or_link(path: Path, st: os.stat_result) -> bool:
    return path.is_symlink() or bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _canonical_member_name(
    raw_name: str,
    *,
    directory: bool = False,
    max_path_length: int = DEFAULT_MAX_PATH_LENGTH,
) -> str | None:
    """Return a normalized member name, preserving directory safety checks.

    ZIP directory records conventionally end in one slash.  The slash is a
    container marker rather than a path segment; all other empty segments are
    rejected by ``normalize_relative_path``.
    """

    if not isinstance(raw_name, str):
        raise PackagePathError("package member name must be text")
    name = raw_name[:-1] if directory and raw_name.endswith("/") else raw_name
    if not name:
        return None
    try:
        canonical = normalize_relative_path(name)
    except ContractError as exc:
        raise PackagePathError(str(exc), path=raw_name) from exc
    if len(canonical) > max_path_length:
        raise PackagePathError(
            f"package member path exceeds {max_path_length} characters",
            path=raw_name,
        )
    # ``files.sha256`` is a generated control file.  Do not canonicalize a
    # case-equivalent spelling into the control name: doing so would make a
    # non-canonical folder/ZIP entry appear valid and would erase the
    # producer's exact path choice before the control-file gate sees it.
    if (
        unicode_nfc_casefold(canonical) == unicode_nfc_casefold(_FILES_MANIFEST_NAME)
        and canonical != _FILES_MANIFEST_NAME
    ):
        raise PackagePathError(
            "files.sha256 must use its canonical lowercase name",
            path=raw_name,
        )
    return canonical


def _insert_file(
    files: MutableMapping[str, bytes],
    seen_kinds: MutableMapping[str, str],
    raw_path: str,
    content: bytes,
    *,
    limits: PackageLimits,
) -> None:
    if not isinstance(content, bytes):
        raise PackageError("package content must be raw bytes", path=raw_path)
    if len(content) > limits.max_file_size:
        raise PackageError("package member exceeds the maximum file size", path=raw_path)
    canonical = _canonical_member_name(
        raw_path,
        max_path_length=limits.max_path_length,
    )
    if canonical is None:
        raise PackagePathError("empty package member path", path=raw_path)
    key = unicode_nfc_casefold(canonical)
    previous = seen_kinds.get(key)
    if previous is not None:
        raise PackagePathError("NFC/casefold package member collision", path=raw_path)
    seen_kinds[key] = "file"
    # Keep the canonical path emitted by the first member.  A casefold
    # collision has already failed above, so this assignment is non-replacing.
    files[canonical] = content


def _finish_files(files: MutableMapping[str, bytes], *, limits: PackageLimits, total_size: int) -> dict[str, bytes]:
    if len(files) > limits.max_files:
        raise PackageError("package contains too many files")
    if total_size > limits.max_total_size:
        raise PackageError("package exceeds the maximum uncompressed size")
    return dict(files)


def read_folder(path: str | os.PathLike[str], *, limits: PackageLimits | None = None) -> dict[str, bytes]:
    """Read a package folder without following symlinks or special files."""

    limits = _validate_limits(limits)
    root = Path(path)
    try:
        root_lstat = root.lstat()
    except OSError as exc:
        raise PackageError(f"cannot stat package folder: {exc}", path=str(root)) from exc
    if _is_reparse_or_link(root, root_lstat) or not stat.S_ISDIR(root_lstat.st_mode):
        raise PackageError("package source must be a real directory", path=str(root))

    files: dict[str, bytes] = {}
    seen_kinds: dict[str, str] = {}
    total_size = 0

    def visit(directory: Path, relative_prefix: str) -> None:
        nonlocal total_size
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.encode("utf-8"))
        except OSError as exc:
            raise PackageError(f"cannot enumerate package folder: {exc}", path=str(directory)) from exc
        for entry in entries:
            try:
                st = entry.lstat()
            except OSError as exc:
                raise PackageError(f"cannot stat package member: {exc}", path=str(entry)) from exc
            if _is_reparse_or_link(entry, st):
                raise PackageError("symlink/reparse-point package members are forbidden", path=str(entry))
            relative = f"{relative_prefix}/{entry.name}" if relative_prefix else entry.name
            if stat.S_ISDIR(st.st_mode):
                # Validate directories too: a malicious empty directory must
                # not provide a path that would later be treated differently
                # by a Windows extractor.
                try:
                    _canonical_member_name(
                        relative + "/",
                        directory=True,
                        max_path_length=limits.max_path_length,
                    )
                except ContractError as exc:
                    raise PackagePathError(str(exc), path=relative) from exc
                visit(entry, relative)
                continue
            if not stat.S_ISREG(st.st_mode):
                raise PackageError("only ordinary files and directories are allowed", path=relative)
            if st.st_size > limits.max_file_size:
                raise PackageError("package member exceeds the maximum file size", path=relative)
            try:
                content = entry.read_bytes()
            except OSError as exc:
                raise PackageError(f"cannot read package member: {exc}", path=relative) from exc
            # A file can grow between stat and read; the post-read check keeps
            # that race fail-closed instead of allowing an unbounded read.
            if len(content) != st.st_size:
                raise PackageError("package member changed while being read", path=relative)
            total_size += len(content)
            _insert_file(files, seen_kinds, relative.replace("\\", "/"), content, limits=limits)
            if len(files) > limits.max_files or total_size > limits.max_total_size:
                _finish_files(files, limits=limits, total_size=total_size)

    visit(root, "")
    return _finish_files(files, limits=limits, total_size=total_size)


def _zip_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0o170000


def read_zip(
    source: str | os.PathLike[str] | bytes | bytearray | BinaryIO,
    *,
    limits: PackageLimits | None = None,
) -> dict[str, bytes]:
    """Read a ZIP/``.ppplugin`` source with traversal and link hard gates."""

    limits = _validate_limits(limits)
    close_after = False
    zf: zipfile.ZipFile
    source_path: Path | None = None
    if isinstance(source, (str, os.PathLike)):
        source_path = Path(source)
        try:
            st = source_path.lstat()
        except OSError as exc:
            raise PackageError(f"cannot stat package archive: {exc}", path=str(source_path)) from exc
        if _is_reparse_or_link(source_path, st) or not stat.S_ISREG(st.st_mode):
            raise PackageError("package archive must be a real regular file", path=str(source_path))
        try:
            zf = zipfile.ZipFile(source_path, "r")
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise PackageError(f"invalid plugin ZIP archive: {exc}", path=str(source_path)) from exc
        close_after = True
    else:
        try:
            if isinstance(source, (bytes, bytearray)):
                source = io.BytesIO(bytes(source))
            zf = zipfile.ZipFile(source, "r")
        except (OSError, TypeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise PackageError(f"invalid plugin ZIP archive: {exc}") from exc

    files: dict[str, bytes] = {}
    seen_kinds: dict[str, str] = {}
    total_size = 0
    try:
        infos = zf.infolist()
        for info in infos:
            raw_name = info.filename
            mode = _zip_mode(info)
            directory = info.is_dir() or raw_name.endswith("/") or mode == stat.S_IFDIR
            if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise PackageError("ZIP symlink/special-file members are forbidden", path=raw_name)
            if info.flag_bits & 0x1:
                raise PackageError("encrypted ZIP members are not supported", path=raw_name)
            canonical = _canonical_member_name(
                raw_name,
                directory=directory,
                max_path_length=limits.max_path_length,
            )
            if canonical is None:
                continue
            key = unicode_nfc_casefold(canonical)
            if directory:
                if key in seen_kinds:
                    # Explicit duplicate directory records are not part of a
                    # deterministic package tree and can hide a file/member
                    # collision on case-insensitive hosts.
                    raise PackagePathError("duplicate/colliding ZIP directory", path=raw_name)
                seen_kinds[key] = "directory"
                continue
            if info.file_size < 0 or info.file_size > limits.max_file_size:
                raise PackageError("ZIP member exceeds the maximum file size", path=raw_name)
            total_size += int(info.file_size)
            if total_size > limits.max_total_size:
                raise PackageError("ZIP exceeds the maximum uncompressed size")
            try:
                content = zf.read(info)
            except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
                raise PackageError(f"cannot read ZIP member: {exc}", path=raw_name) from exc
            if len(content) != info.file_size:
                raise PackageError("ZIP member size changed or is inconsistent", path=raw_name)
            _insert_file(files, seen_kinds, canonical, content, limits=limits)
            if len(files) > limits.max_files:
                raise PackageError("package contains too many files")
    finally:
        if close_after:
            zf.close()
    return _finish_files(files, limits=limits, total_size=total_size)


def read_package_files(
    source: str | os.PathLike[str] | bytes | bytearray | BinaryIO | Mapping[str, bytes],
    *,
    limits: PackageLimits | None = None,
) -> dict[str, bytes]:
    """Read and normalize a folder, ZIP or already materialized file map."""

    limits = _validate_limits(limits)
    if isinstance(source, Mapping):
        files: dict[str, bytes] = {}
        seen: dict[str, str] = {}
        total_size = 0
        for raw_path, content in source.items():
            if not isinstance(raw_path, str):
                raise PackagePathError("package file path must be text")
            if not isinstance(content, bytes):
                raise PackageError("package content must be raw bytes", path=raw_path)
            total_size += len(content)
            # A mapping is already a package-level path representation.  Do
            # not silently turn an invalid backslash path into a different
            # identity; only the physical folder reader translates host
            # separators before entering this function.
            _insert_file(files, seen, raw_path, content, limits=limits)
        return _finish_files(files, limits=limits, total_size=total_size)
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        try:
            st = path.lstat()
        except OSError as exc:
            raise PackageError(f"cannot stat package source: {exc}", path=str(path)) from exc
        if _is_reparse_or_link(path, st):
            raise PackageError("symlink/reparse-point package sources are forbidden", path=str(path))
        if stat.S_ISDIR(st.st_mode):
            return read_folder(path, limits=limits)
        if stat.S_ISREG(st.st_mode):
            return read_zip(path, limits=limits)
        raise PackageError("package source must be a folder or ZIP file", path=str(path))
    return read_zip(source, limits=limits)


def _split_control_files(
    files: Mapping[str, bytes],
    *,
    limits: PackageLimits | None = None,
) -> PackageFiles:
    max_path_length = (
        DEFAULT_MAX_PATH_LENGTH if limits is None else limits.max_path_length
    )
    manifest_key = None
    files_manifest_key = None
    normalized: dict[str, bytes] = {}
    seen: set[str] = set()
    for raw_path, content in files.items():
        try:
            path = normalize_relative_path(raw_path)
        except ContractError as exc:
            raise PackagePathError(str(exc), path=str(raw_path)) from exc
        if len(path) > max_path_length:
            raise PackagePathError(
                f"package member path exceeds {max_path_length} characters",
                path=raw_path,
            )
        key = unicode_nfc_casefold(path)
        if key in seen:
            raise PackagePathError("NFC/casefold package member collision", path=raw_path)
        seen.add(key)
        if not isinstance(content, bytes):
            raise PackageError("package content must be raw bytes", path=path)
        if key == unicode_nfc_casefold(_MANIFEST_NAME):
            if path != _MANIFEST_NAME:
                raise PackagePathError("plugin.json must use its canonical name", path=path)
            manifest_key = path
        elif key == unicode_nfc_casefold(_FILES_MANIFEST_NAME):
            if path != _FILES_MANIFEST_NAME:
                raise PackagePathError(
                    "files.sha256 must use its canonical lowercase name",
                    path=path,
                )
            files_manifest_key = path
        else:
            normalized[path] = content
    if manifest_key is None:
        raise PackageIntegrityError("plugin.json is required")
    if files_manifest_key is None:
        raise PackageIntegrityError("files.sha256 is required")
    # Keep plugin.json in the ordinary file map; only the generated files
    # manifest is excluded from package hashing per §13.4.
    normalized[_MANIFEST_NAME] = files[manifest_key]
    # The map is intentionally rebuilt in canonical UTF-8 order only for
    # deterministic diagnostics.  build_files_sha256 itself performs the
    # authoritative sort and digest.
    return PackageFiles(normalized, files[files_manifest_key])


def _lookup_path(files: Mapping[str, bytes], declared: str, *, label: str) -> str:
    try:
        canonical = normalize_relative_path(declared)
    except ContractError as exc:
        raise PackageTypeError(f"{label} is not a valid package path", path=declared) from exc
    folded = unicode_nfc_casefold(canonical)
    for path in files:
        if unicode_nfc_casefold(path) == folded:
            return path
    raise PackageTypeError(f"{label} is missing from the package", path=declared)


def _has_prefix(files: Mapping[str, bytes], declared: str) -> bool:
    try:
        canonical = normalize_relative_path(declared)
    except ContractError:
        return False
    folded = unicode_nfc_casefold(canonical).rstrip("/") + "/"
    return any(unicode_nfc_casefold(path).startswith(folded) for path in files)


def _check_executable_suffix(path: str) -> bool:
    folded = unicode_nfc_casefold(path)
    return any(folded.endswith(suffix) for suffix in _EXECUTABLE_SUFFIXES)


def _validate_type_gate(manifest: Mapping[str, Any], files: Mapping[str, bytes]) -> None:
    kind = manifest["kind"]
    if kind == "data":
        data = manifest.get("data")
        if not isinstance(data, Mapping):
            raise PackageTypeError("data plugin must declare a data root")
        _lookup_path(files, data["root"], label="data.root")
        for path in files:
            folded = unicode_nfc_casefold(path)
            top = folded.split("/", 1)[0]
            if top in {"backend", "ui", "migrations", "wheels", "wheelhouse"}:
                raise PackageTypeError("data plugins cannot carry backend/UI/migration/wheel content", path=path)
            if _check_executable_suffix(path):
                raise PackageTypeError("data plugins cannot carry executable content", path=path)
        return

    if kind != "code":
        raise PackageTypeError(f"unsupported plugin kind: {kind!r}")
    backend = manifest.get("backend")
    if not isinstance(backend, Mapping):
        raise PackageTypeError("code plugin backend declaration is required")
    wheel_path = _lookup_path(files, backend["wheel"], label="backend.wheel")
    if not unicode_nfc_casefold(wheel_path).endswith(".whl"):
        raise PackageTypeError("backend.wheel must point to a .whl file", path=wheel_path)
    _lookup_path(files, backend["requirements_lock"], label="backend.requirements_lock")
    # A wheelhouse may be empty for a wheel with no third-party dependencies;
    # when entries exist, all of them must be ordinary wheel archives.
    wheelhouse = backend["wheelhouse"]
    try:
        wheelhouse_norm = normalize_relative_path(wheelhouse)
    except ContractError as exc:
        raise PackageTypeError("backend.wheelhouse is not a valid path", path=str(wheelhouse)) from exc
    wheelhouse_folded = unicode_nfc_casefold(wheelhouse_norm).rstrip("/") + "/"
    for path in files:
        folded = unicode_nfc_casefold(path)
        if folded.startswith(wheelhouse_folded) and not folded.endswith(".whl"):
            raise PackageTypeError("backend.wheelhouse may contain only wheel files", path=path)

    storage = manifest.get("storage")
    if isinstance(storage, Mapping):
        migration = _lookup_path(files, storage["migration_manifest"], label="storage.migration_manifest")
        if _check_executable_suffix(migration):
            raise PackageTypeError("migration manifest cannot be executable", path=migration)
    settings = manifest.get("settings")
    if isinstance(settings, Mapping):
        _lookup_path(files, settings["schema"], label="settings.schema")
        _lookup_path(files, settings["defaults"], label="settings.defaults")
        migration = settings.get("migration_manifest")
        if migration is not None:
            migration_path = _lookup_path(files, migration, label="settings.migration_manifest")
            if _check_executable_suffix(migration_path):
                raise PackageTypeError("settings migration manifest cannot be executable", path=migration_path)
    ui = manifest.get("ui")
    if isinstance(ui, Mapping):
        _lookup_path(files, ui["entry"], label="ui.entry")

    # Declarative migration directories may contain JSON/schema files but not
    # executable source.  This is a type gate, not a code scanner.
    for path in files:
        folded = unicode_nfc_casefold(path)
        if folded.startswith("migrations/") and _check_executable_suffix(path):
            raise PackageTypeError("migrations must be declarative", path=path)


def _assert_compatibility(
    manifest: Mapping[str, Any],
    profile: CompatibilityProfile,
) -> None:
    compatibility = manifest["compatibility"]
    mismatches: dict[str, tuple[Any, str]] = {}
    for key, expected in (
        ("core_api", profile.core_api),
        ("plugin_rpc", profile.plugin_rpc),
        ("ui_host", profile.ui_host),
    ):
        if compatibility.get(key) != expected:
            mismatches[key] = (compatibility.get(key), expected)
    if manifest["kind"] == "code" and compatibility.get("python") != profile.python:
        mismatches["python"] = (compatibility.get("python"), profile.python)
    if mismatches:
        raise PackageCompatibilityError(
            "package compatibility does not match the selected host",
            details={"mismatches": mismatches},
        )


def _parse_manifest(manifest_bytes: bytes) -> dict[str, Any]:
    try:
        value = parse_json_bytes(manifest_bytes)
    except ContractError as exc:
        raise PackageIntegrityError(f"plugin.json is not strict UTF-8 JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise PackageIntegrityError("plugin.json must contain a JSON object")
    # The frozen §13.4 data golden predates the schema's explicit nullable
    # ``settings`` field and intentionally omits it.  Validate a compatibility
    # copy while preserving the exact manifest object/bytes used for hashing;
    # all other shape errors remain closed-schema failures.
    schema_value = dict(value)
    if schema_value.get("kind") == "data" and "settings" not in schema_value:
        schema_value["settings"] = None
    try:
        verify_manifest(schema_value)
    except ContractError as exc:
        raise PackageIntegrityError(str(exc)) from exc
    return dict(value)


def verify_package(
    source: str | os.PathLike[str] | bytes | bytearray | BinaryIO | Mapping[str, bytes] | VerifiedPackage,
    *,
    expected_package_hash: str | None = None,
    expected_release_id: str | None = None,
    expected_files_sha256: bytes | None = None,
    limits: PackageLimits | None = None,
    compatibility: CompatibilityProfile | Mapping[str, Any] | None = None,
    core_api_version: str | None = None,
    plugin_rpc_version: str | None = None,
    ui_host_version: str | None = None,
    python_version: str | None = None,
) -> VerifiedPackage:
    """Read and verify a complete plugin package.

    The returned file map is copied into a read-only ``MappingProxyType``;
    callers can safely pass it to a package store without retaining a mutable
    view of the source directory or ZIP reader.
    """

    if isinstance(source, VerifiedPackage):
        # A VerifiedPackage is a convenience view, not an authority.  It is
        # publicly constructible (and dataclasses.replace can produce one),
        # so reconstruct the generated control member and run the exact same
        # verifier as an untrusted folder/ZIP before accepting it.  The
        # control member is intentionally absent from ``source.files`` in the
        # public view; adding it here makes the re-verification path complete.
        if not isinstance(source.files, Mapping):
            raise PackageIntegrityError("verified package files must be a mapping")
        for field_name in ("plugin_id", "version", "kind", "package_hash", "release_id"):
            if not isinstance(getattr(source, field_name), str):
                raise PackageIntegrityError(
                    f"verified package {field_name} must be text"
                )
        try:
            files = dict(source.files)
        except (TypeError, ValueError) as exc:
            raise PackageIntegrityError("verified package files must be a mapping") from exc
        if _FILES_MANIFEST_NAME in files:
            supplied_manifest = files.pop(_FILES_MANIFEST_NAME)
            if not isinstance(supplied_manifest, bytes) or supplied_manifest != source.files_sha256:
                raise PackageIntegrityError(
                    "verified package files.sha256 does not match its control bytes"
                )
        if not isinstance(source.files_sha256, bytes):
            raise PackageIntegrityError("verified package files.sha256 must be raw bytes")
        files[_FILES_MANIFEST_NAME] = source.files_sha256
        if expected_package_hash is not None and source.package_hash != expected_package_hash:
            raise PackageIntegrityError("package_hash does not match the expected identity")
        if expected_release_id is not None and source.release_id != expected_release_id:
            raise PackageIntegrityError("release_id does not match the expected identity")
        reverified = _verify_file_map(
            files,
            source=source.source,
            expected_package_hash=(
                expected_package_hash
                if expected_package_hash is not None
                else source.package_hash
            ),
            expected_release_id=(
                expected_release_id
                if expected_release_id is not None
                else source.release_id
            ),
            expected_files_sha256=(
                expected_files_sha256
                if expected_files_sha256 is not None
                else source.files_sha256
            ),
            limits=limits,
            compatibility=compatibility,
            core_api_version=core_api_version,
            plugin_rpc_version=plugin_rpc_version,
            ui_host_version=ui_host_version,
            python_version=python_version,
        )
        if reverified.identity != source.identity or reverified.kind != source.kind:
            raise PackageIntegrityError(
                "verified package identity metadata does not match its content"
            )
        return reverified
    files = read_package_files(source, limits=limits)
    source_path = Path(source) if isinstance(source, (str, os.PathLike)) else None
    return _verify_file_map(
        files,
        source=source_path,
        expected_package_hash=expected_package_hash,
        expected_release_id=expected_release_id,
        expected_files_sha256=expected_files_sha256,
        limits=limits,
        compatibility=compatibility,
        core_api_version=core_api_version,
        plugin_rpc_version=plugin_rpc_version,
        ui_host_version=ui_host_version,
        python_version=python_version,
    )


def _verify_file_map(
    files: Mapping[str, bytes],
    *,
    source: Path | None,
    expected_package_hash: str | None,
    expected_release_id: str | None,
    expected_files_sha256: bytes | None,
    limits: PackageLimits | None,
    compatibility: CompatibilityProfile | Mapping[str, Any] | None,
    core_api_version: str | None,
    plugin_rpc_version: str | None,
    ui_host_version: str | None,
    python_version: str | None,
) -> VerifiedPackage:
    limits = _validate_limits(limits)
    controls = _split_control_files(files, limits=limits)
    manifest_bytes = controls.files[_MANIFEST_NAME]
    manifest = _parse_manifest(manifest_bytes)
    if expected_files_sha256 is not None and not isinstance(expected_files_sha256, bytes):
        raise PackageIntegrityError("expected files.sha256 must be raw bytes")
    expected_manifest = controls.files_sha256 if expected_files_sha256 is None else expected_files_sha256
    try:
        verify_package_manifest(controls.files, expected_manifest)
    except ContractError as exc:
        raise PackageIntegrityError(str(exc)) from exc
    try:
        digest = digest_package(controls.files, manifest["plugin_id"], manifest["version"])
    except ContractError as exc:
        raise PackageIntegrityError(str(exc)) from exc
    # Keep the SDK's identity verifier as the sole digest/release formula
    # implementation.  Calling it here also catches accidental future drift
    # between digest_package and the verifier.
    try:
        verify_package_identity(
            controls.files,
            manifest["plugin_id"],
            manifest["version"],
            digest.package_hash,
            digest.release_id,
            expected_files_sha256=expected_manifest,
        )
    except ContractError as exc:
        raise PackageIntegrityError(str(exc)) from exc
    if expected_package_hash is not None:
        if (
            not isinstance(expected_package_hash, str)
            or not _HEX64.fullmatch(expected_package_hash)
            or digest.package_hash != expected_package_hash
        ):
            raise PackageIntegrityError("package_hash does not match the expected identity")
    if expected_release_id is not None:
        if (
            not isinstance(expected_release_id, str)
            or not _HEX64.fullmatch(expected_release_id)
            or digest.release_id != expected_release_id
        ):
            raise PackageIntegrityError("release_id does not match the expected identity")

    _validate_type_gate(manifest, controls.files)
    if compatibility is None:
        profile = CompatibilityProfile()
    elif isinstance(compatibility, CompatibilityProfile):
        profile = compatibility
    elif isinstance(compatibility, Mapping):
        profile = CompatibilityProfile.from_mapping(compatibility)
    else:
        raise PackageCompatibilityError("compatibility profile must be a mapping")
    overrides = {
        key: value
        for key, value in (
            ("core_api", core_api_version),
            ("plugin_rpc", plugin_rpc_version),
            ("ui_host", ui_host_version),
            ("python", python_version),
        )
        if value is not None
    }
    if overrides:
        profile = CompatibilityProfile(
            core_api=overrides.get("core_api", profile.core_api),
            plugin_rpc=overrides.get("plugin_rpc", profile.plugin_rpc),
            ui_host=overrides.get("ui_host", profile.ui_host),
            python=overrides.get("python", profile.python),
        )
    _assert_compatibility(manifest, profile)
    immutable_files = MappingProxyType(dict(controls.files))
    immutable_manifest = MappingProxyType(dict(manifest))
    return VerifiedPackage(
        plugin_id=manifest["plugin_id"],
        version=manifest["version"],
        kind=manifest["kind"],
        package_hash=digest.package_hash,
        release_id=digest.release_id,
        files_sha256=bytes(expected_manifest),
        manifest=immutable_manifest,
        files=immutable_files,
        source=source,
    )


# Compatibility aliases used by older control-plane call sites.
load_package = verify_package
read_package = verify_package
verify_release = verify_package
verify_package_release = verify_package
read_package_folder = read_folder
read_package_zip = read_zip
collect_package_files = read_package_files


def package_files_sha256(files: Mapping[str, bytes]) -> bytes:
    """Return the exact generated files manifest for a package file map."""
    # This helper is also useful while constructing a package, before the
    # generated ``files.sha256`` member exists.  If the caller supplied that
    # member, validate its control-file shape and generate the exact bytes
    # from the remaining ordinary files; otherwise normalize the supplied
    # ordinary map directly.
    if any(
        isinstance(path, str) and unicode_nfc_casefold(path) == unicode_nfc_casefold(_FILES_MANIFEST_NAME)
        for path in files
    ):
        return build_files_sha256(_split_control_files(files).files)
    normalized = read_package_files(files)
    return build_files_sha256(normalized)


__all__ = [
    "CompatibilityProfile",
    "DEFAULT_MAX_FILE_SIZE",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_PATH_LENGTH",
    "DEFAULT_MAX_TOTAL_SIZE",
    "PackageError",
    "PackageFiles",
    "PackageIntegrityError",
    "PackageLimits",
    "PackagePathError",
    "PackageTypeError",
    "PackageVerification",
    "PackageCompatibilityError",
    "VerifiedPackage",
    "collect_package_files",
    "load_package",
    "package_files_sha256",
    "read_folder",
    "read_package",
    "read_package_files",
    "read_package_folder",
    "read_package_zip",
    "read_zip",
    "verify_package",
    "verify_package_release",
    "verify_release",
]
