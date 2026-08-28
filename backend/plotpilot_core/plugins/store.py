"""Content-addressed, non-replacing storage for verified plugin packages."""

from __future__ import annotations

import json
import os
import shutil
import stat
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import quote

from plotpilot_plugin_sdk.canonical import parse_json_bytes
from plotpilot_plugin_sdk.errors import ContractError

from .package import (
    PackageError,
    PackageIntegrityError,
    PackageLimits,
    VerifiedPackage,
    verify_package,
)


class PackageStoreError(PackageError):
    """A package-store operation failed closed."""


class PackageConflictError(PackageStoreError):
    """The same plugin/version was presented with a different package hash."""


_REGISTRY_SCHEMA = "plotpilot-package-store/v1"
_REGISTRY_NAME = "registry.json"
_LOCK_NAME = ".registry.lock"
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(root: Path) -> threading.RLock:
    key = str(root)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _component(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PackageStoreError(f"{label} must be a non-empty string")
    # Keep common IDs human-readable while escaping slash/colon and all other
    # filesystem-significant characters.  This is a path component, not an
    # identity transformation: the registry always retains the original ID.
    return quote(value, safe=".-_~")


def _safe_child(root: Path, child: Path) -> Path:
    try:
        child.relative_to(root)
    except ValueError as exc:
        raise PackageStoreError("package path escapes the store root") from exc
    current = root
    for part in child.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise PackageStoreError(
                "symlink package-store path is forbidden", path=str(current)
            )
    return child


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


@contextmanager
def _registry_file_lock(path: Path) -> Iterator[None]:
    """Serialize registry mutations across processes using the host OS lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class PackageStore:
    """Store verified packages at ``packages/<id>/<version>/<hash>``.

    Publication is content-addressed and non-replacing.  The registry is an
    append-only identity index used to reject a second hash for one
    ``plugin_id + version``.  Package files themselves are never modified once
    the directory is published.
    """

    def __init__(
        self, root: str | os.PathLike[str], *, limits: PackageLimits | None = None
    ) -> None:
        self.root = Path(root).absolute()
        self.packages_root = self.root / "packages"
        self.staging_root = self.root / "staging"
        self.registry_path = self.root / _REGISTRY_NAME
        self.registry_lock_path = self.root / _LOCK_NAME
        self.limits = limits
        self.root.mkdir(parents=True, exist_ok=True)
        self.packages_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self._lock = _lock_for(self.root)
        # Validate existing registry at construction.  A corrupt identity
        # index must never be silently rebuilt over a possibly active store.
        self._registry = self._load_registry()

    def _load_registry(self) -> dict[tuple[str, str], dict[str, str]]:
        if not self.registry_path.exists():
            return {}
        try:
            raw = parse_json_bytes(self.registry_path.read_bytes())
        except Exception as exc:
            raise PackageStoreError(
                f"invalid package registry: {exc}", path=str(self.registry_path)
            ) from exc
        if (
            not isinstance(raw, Mapping)
            or raw.get("schema") != _REGISTRY_SCHEMA
            or not isinstance(raw.get("releases"), list)
        ):
            raise PackageStoreError(
                "invalid package registry shape", path=str(self.registry_path)
            )
        result: dict[tuple[str, str], dict[str, str]] = {}
        for entry in raw["releases"]:
            if not isinstance(entry, Mapping):
                raise PackageStoreError("invalid package registry entry")
            fields = ("plugin_id", "version", "package_hash", "release_id")
            if any(not isinstance(entry.get(field), str) for field in fields):
                raise PackageStoreError("package registry entry has invalid identity")
            key = (entry["plugin_id"], entry["version"])
            value = {field: entry[field] for field in fields}
            old = result.get(key)
            if old is not None and old != value:
                raise PackageConflictError(
                    "package registry contains conflicting identities"
                )
            result[key] = value
        return result

    def _write_registry(self) -> None:
        entries = sorted(
            self._registry.values(),
            key=lambda item: (
                item["plugin_id"].encode("utf-8"),
                item["version"].encode("utf-8"),
            ),
        )
        payload = {"schema": _REGISTRY_SCHEMA, "releases": entries}
        tmp = self.registry_path.with_name(
            f".{self.registry_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with tmp.open("xb") as handle:
                handle.write(_json_bytes(payload))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.registry_path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def package_path(self, plugin_id: str, version: str, package_hash: str) -> Path:
        if (
            not isinstance(package_hash, str)
            or len(package_hash) != 64
            or any(c not in "0123456789abcdef" for c in package_hash)
        ):
            raise PackageStoreError("package_hash must be lowercase SHA-256")
        return _safe_child(
            self.root,
            self.packages_root
            / _component(plugin_id, label="plugin_id")
            / _component(version, label="version")
            / package_hash,
        )

    path_for = package_path

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        with self._lock, _registry_file_lock(self.registry_lock_path):
            # Refresh under the cross-process lock so no publisher can
            # overwrite a registry entry committed by another process.
            self._registry = self._load_registry()
            yield

    def _materialize(self, package: VerifiedPackage, destination: Path) -> None:
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        # Keep the temporary component short and close to the store root.
        # Appending hash + UUID below the final identity path exceeds the
        # classic Windows MAX_PATH limit for otherwise valid packages.
        temp = self.staging_root / f"pub-{uuid.uuid4().hex}"
        _safe_child(self.root, temp)
        temp.mkdir(parents=True, exist_ok=False)
        try:
            materialized_files = dict(package.files)
            # ``VerifiedPackage.files`` intentionally excludes the generated
            # control manifest for digest calculations; the published tree
            # must still retain the exact bytes so it can be re-verified.
            materialized_files["files.sha256"] = package.files_sha256
            for relative, content in materialized_files.items():
                target = _safe_child(temp, temp / Path(*relative.split("/")))
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                except OSError:
                    pass
            # A POSIX rename of a directory is atomic.  The process lock keeps
            # same-process publishers from racing; if another process wins,
            # rename raises or the postcondition check below rejects overwrite.
            temp.rename(destination)
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        for directory, dirs, _ in os.walk(destination, topdown=False):
            for name in dirs:
                try:
                    Path(directory, name).chmod(
                        stat.S_IRUSR
                        | stat.S_IXUSR
                        | stat.S_IRGRP
                        | stat.S_IXGRP
                        | stat.S_IROTH
                        | stat.S_IXOTH
                    )
                except OSError:
                    pass
        try:
            destination.chmod(
                stat.S_IRUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH
            )
        except OSError:
            pass

    def _entry_for(self, package: VerifiedPackage) -> dict[str, str]:
        return {
            "plugin_id": package.plugin_id,
            "version": package.version,
            "package_hash": package.package_hash,
            "release_id": package.release_id,
        }

    def publish(
        self,
        package: VerifiedPackage | str | os.PathLike[str] | Mapping[str, bytes],
        *,
        expected_package_hash: str | None = None,
        expected_release_id: str | None = None,
    ) -> VerifiedPackage:
        """Verify then publish a package without ever replacing an existing tree."""

        # Never trust a caller-supplied VerifiedPackage as an authority.  It
        # is a public dataclass and can be forged/replaced; verify_package()
        # reconstructs and checks its generated files.sha256 before any
        # destination or registry mutation occurs.
        verified = verify_package(
            package,
            expected_package_hash=expected_package_hash,
            expected_release_id=expected_release_id,
            limits=self.limits,
        )
        if (
            expected_package_hash is not None
            and verified.package_hash != expected_package_hash
        ):
            raise PackageIntegrityError("package_hash does not match expected identity")
        if (
            expected_release_id is not None
            and verified.release_id != expected_release_id
        ):
            raise PackageIntegrityError("release_id does not match expected identity")
        with self._exclusive():
            key = (verified.plugin_id, verified.version)
            entry = self._entry_for(verified)
            old = self._registry.get(key)
            if old is not None and old["package_hash"] != verified.package_hash:
                raise PackageConflictError(
                    "same plugin_id/version already has a different package hash",
                    details={
                        "existing": old["package_hash"],
                        "incoming": verified.package_hash,
                    },
                )
            destination = self.package_path(*verified.identity)
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_dir():
                    raise PackageStoreError(
                        "published package path is not a directory",
                        path=str(destination),
                    )
                try:
                    existing = verify_package(destination, limits=self.limits)
                except ContractError as exc:
                    raise PackageIntegrityError(
                        f"published package failed integrity verification: {exc}"
                    ) from exc
                if (
                    existing.identity != verified.identity
                    or existing.release_id != verified.release_id
                ):
                    raise PackageConflictError(
                        "published package identity does not match destination"
                    )
                if old is None:
                    self._registry[key] = entry
                    self._write_registry()
                return existing
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._materialize(verified, destination)
            except FileExistsError:
                # A cross-process publisher may have won the race.  Never
                # replace it; verify and adopt only the exact same identity.
                if not destination.is_dir():
                    raise PackageStoreError(
                        "package publication raced with a non-directory"
                    )
                existing = verify_package(destination, limits=self.limits)
                if existing.identity != verified.identity:
                    raise PackageConflictError(
                        "package publication raced with a different identity"
                    )
                verified = existing
            self._registry[key] = entry
            try:
                self._write_registry()
            except Exception:
                # The package remains immutable and unregistered.  It is not
                # returned as active; a later explicit publish may adopt the
                # exact identity after registry recovery.
                self._registry.pop(key, None)
                raise
            return verified

    put = publish
    install = publish

    def _find_entry(
        self, plugin_id: str, version: str, package_hash: str | None
    ) -> dict[str, str]:
        self._registry = self._load_registry()
        key = (plugin_id, version)
        entry = self._registry.get(key)
        if entry is not None:
            if package_hash is not None and package_hash != entry["package_hash"]:
                raise PackageConflictError(
                    "requested package hash differs from the registered release"
                )
            return entry
        # Permit explicit recovery of a store created before registry.json,
        # but require exactly one candidate and verify it before adoption.
        parent = (
            self.packages_root
            / _component(plugin_id, label="plugin_id")
            / _component(version, label="version")
        )
        _safe_child(self.root, parent)
        if not parent.is_dir() or parent.is_symlink():
            raise FileNotFoundError((plugin_id, version))
        candidates = [
            child
            for child in parent.iterdir()
            if child.is_dir() and not child.is_symlink()
        ]
        if package_hash is not None:
            candidates = [child for child in candidates if child.name == package_hash]
        if len(candidates) != 1:
            raise PackageStoreError("package identity is missing or ambiguous")
        candidate = verify_package(candidates[0], limits=self.limits)
        if (
            candidate.plugin_id != plugin_id
            or candidate.version != version
            or (package_hash and candidate.package_hash != package_hash)
        ):
            raise PackageIntegrityError(
                "package path does not match its manifest identity"
            )
        return self._entry_for(candidate)

    def get(
        self, plugin_id: str, version: str, package_hash: str | None = None
    ) -> VerifiedPackage:
        with self._lock:
            entry = self._find_entry(plugin_id, version, package_hash)
            path = self.package_path(plugin_id, version, entry["package_hash"])
            if not path.is_dir() or path.is_symlink():
                raise PackageIntegrityError("registered package content is missing")
            return verify_package(
                path,
                expected_package_hash=entry["package_hash"],
                expected_release_id=entry["release_id"],
                limits=self.limits,
            )

    load = get
    resolve = get

    def require_release(
        self,
        *,
        release_id: str,
        plugin_id: str,
        package_hash: str,
    ) -> VerifiedPackage:
        """Resolve and reverify one exact executable release identity."""
        with self._lock:
            self._registry = self._load_registry()
            matches = [
                entry
                for entry in self._registry.values()
                if entry["release_id"] == release_id
                and entry["plugin_id"] == plugin_id
                and entry["package_hash"] == package_hash
            ]
            if len(matches) != 1:
                raise PackageStoreError("exact executable release is unavailable")
            entry = matches[0]
            return self.get(plugin_id, entry["version"], package_hash)

    def has(
        self, plugin_id: str, version: str, package_hash: str | None = None
    ) -> bool:
        try:
            self.get(plugin_id, version, package_hash)
        except (ContractError, OSError):
            return False
        return True

    contains = has

    def read_file(
        self,
        plugin_id: str,
        version: str,
        relative_path: str,
        package_hash: str | None = None,
    ) -> bytes:
        package = self.get(plugin_id, version, package_hash)
        return package.read_bytes(relative_path)

    open_file = read_file

    def list_releases(
        self, *, verify: bool = True
    ) -> tuple[VerifiedPackage | Mapping[str, str], ...]:
        with self._lock:
            self._registry = self._load_registry()
            entries = sorted(
                self._registry.values(),
                key=lambda item: (
                    item["plugin_id"].encode("utf-8"),
                    item["version"].encode("utf-8"),
                ),
            )
            if not verify:
                return tuple(MappingProxyType(dict(entry)) for entry in entries)
            return tuple(
                self.get(entry["plugin_id"], entry["version"], entry["package_hash"])
                for entry in entries
            )

    releases = list_releases

    def package_url(
        self,
        plugin_id: str,
        version: str,
        relative_path: str,
        package_hash: str | None = None,
    ) -> str:
        package = self.get(plugin_id, version, package_hash)
        # This is an immutable logical URL used by the Core HTTP adapter; the
        # store itself never serves HTTP.
        from plotpilot_plugin_sdk.package import normalize_relative_path

        path = normalize_relative_path(relative_path)
        if path not in package.files:
            package.read_bytes(path)  # raise the canonical FileNotFoundError
        return f"/__plotpilot/plugin/{package.release_id}/{package.package_hash}/{quote(path, safe='/')}"

    def remove_package_content(
        self,
        plugin_id: str,
        version: str,
        package_hash: str,
        *,
        expected_release_id: str,
    ) -> bool:
        """Delete exact package bytes while retaining the registry tombstone.

        The lifecycle retirement barrier must already have fenced every
        executable reference.  Keeping the immutable identity entry ensures
        historical receipts remain resolvable even though execution is no
        longer possible.  Repeating an already completed deletion is safe.
        """
        with self._exclusive():
            entry = self._registry.get((plugin_id, version))
            if entry is None:
                raise PackageStoreError("release identity is not registered")
            if (
                entry["package_hash"] != package_hash
                or entry["release_id"] != expected_release_id
            ):
                raise PackageConflictError(
                    "retirement identity does not match registered package"
                )
            destination = self.package_path(plugin_id, version, package_hash)
            if not destination.exists():
                # Deletion is nonreplace and retry-safe.  Absence after a crash
                # between filesystem removal and retirement-row commit is the
                # same successful postcondition as removing it now.
                return True
            if destination.is_symlink() or not destination.is_dir():
                raise PackageStoreError("retirement package path is not a directory")
            verified = verify_package(
                destination,
                expected_package_hash=package_hash,
                expected_release_id=expected_release_id,
                limits=self.limits,
            )
            if verified.plugin_id != plugin_id or verified.version != version:
                raise PackageConflictError(
                    "retirement package path contains a different release"
                )
            for directory, dirs, files in os.walk(destination, topdown=False):
                for name in files:
                    try:
                        Path(directory, name).chmod(stat.S_IWRITE | stat.S_IREAD)
                    except OSError:
                        pass
                for name in dirs:
                    try:
                        Path(directory, name).chmod(
                            stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC
                        )
                    except OSError:
                        pass
            try:
                destination.chmod(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except OSError:
                pass
            shutil.rmtree(destination)
            return True


__all__ = ["PackageConflictError", "PackageStore", "PackageStoreError"]
