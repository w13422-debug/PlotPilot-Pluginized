"""Immutable Skill release history and active-version protection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from plotpilot_plugin_sdk import ContractValidationError


def _required(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field} must not be blank")
    return value.strip()


@dataclass(frozen=True, slots=True)
class ActiveVersion:
    """A CPMS/Skill version pointer with immutable payload metadata."""

    version_id: str
    payload_hash: str
    source: str = "system"
    release_id: str | None = None
    payload: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "version_id", _required(self.version_id, "version_id"))
        if len(self.payload_hash) != 64 or any(ch not in "0123456789abcdef" for ch in self.payload_hash):
            raise ContractValidationError("payload_hash must be lowercase SHA-256")
        if self.source not in {"system", "user", "legacy"}:
            raise ContractValidationError("version source must be system, user or legacy")
        if self.release_id is not None:
            _required(self.release_id, "release_id")
        if self.payload is not None:
            object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class VersionDecision:
    active: ActiveVersion
    changed: bool
    reason: str


def protect_active_version(current: ActiveVersion | None, incoming: ActiveVersion) -> VersionDecision:
    """Never let package synchronization replace a user-adopted version."""

    if not isinstance(incoming, ActiveVersion):
        raise TypeError("incoming must be ActiveVersion")
    if current is None:
        return VersionDecision(incoming, True, "initial-version")
    if not isinstance(current, ActiveVersion):
        raise TypeError("current must be ActiveVersion or None")
    if current.source == "user":
        return VersionDecision(current, False, "user-active-version-protected")
    if current.version_id == incoming.version_id and current.payload_hash == incoming.payload_hash:
        return VersionDecision(current, False, "unchanged")
    return VersionDecision(incoming, True, "system-version-updated")


# More descriptive alias used by sync callers.
sync_active_version = protect_active_version


@dataclass(frozen=True, slots=True)
class LegacyReadOnlyRecord:
    release_id: str
    package_hash: str
    receipt_ids: tuple[str, ...] = ()
    package_present: bool = True

    def __post_init__(self) -> None:
        _required(self.release_id, "release_id")
        if len(self.package_hash) != 64 or any(ch not in "0123456789abcdef" for ch in self.package_hash):
            raise ContractValidationError("package_hash must be lowercase SHA-256")
        object.__setattr__(self, "receipt_ids", tuple(self.receipt_ids))


@dataclass(frozen=True, slots=True)
class SkillReleaseHistory:
    """Append-only release/receipt history with package-only delete semantics."""

    releases: tuple[ActiveVersion, ...] = ()
    active_version_id: str | None = None
    legacy_records: tuple[LegacyReadOnlyRecord, ...] = ()

    def __post_init__(self) -> None:
        releases = tuple(self.releases)
        ids = [item.version_id for item in releases]
        if len(ids) != len(set(ids)):
            raise ContractValidationError("Skill version IDs must be unique")
        if self.active_version_id is not None and self.active_version_id not in ids:
            raise ContractValidationError("active version must exist in release history")
        object.__setattr__(self, "releases", releases)
        object.__setattr__(self, "legacy_records", tuple(self.legacy_records))

    @property
    def active(self) -> ActiveVersion | None:
        return next((item for item in self.releases if item.version_id == self.active_version_id), None)

    def add(self, version: ActiveVersion, *, activate: bool = False) -> SkillReleaseHistory:
        if any(item.version_id == version.version_id for item in self.releases):
            raise ContractValidationError("same Skill version ID cannot be replaced")
        return replace(
            self,
            releases=self.releases + (version,),
            active_version_id=version.version_id if activate else self.active_version_id,
        )

    def synchronize(self, incoming: ActiveVersion) -> tuple[SkillReleaseHistory, VersionDecision]:
        decision = protect_active_version(self.active, incoming)
        if not decision.changed:
            return self, decision
        if any(item.version_id == incoming.version_id for item in self.releases):
            # An existing immutable ID/hash can be selected, but never
            # overwritten with a different payload.
            existing = next(item for item in self.releases if item.version_id == incoming.version_id)
            if existing.payload_hash != incoming.payload_hash:
                raise ContractValidationError("same Skill version ID has a different payload")
            return replace(self, active_version_id=existing.version_id), VersionDecision(existing, True, "existing-version-selected")
        return self.add(incoming, activate=True), decision

    def delete_package(self, release_id: str) -> SkillReleaseHistory:
        """Drop executable/package bytes while retaining legacy receipt lookup."""

        release = next((item for item in self.releases if item.release_id == release_id), None)
        if release is None:
            raise ContractValidationError("cannot delete an unknown Skill release")
        record = LegacyReadOnlyRecord(release_id, release.payload_hash, package_present=False)
        return replace(
            self,
            legacy_records=self.legacy_records + (record,),
            active_version_id=None if self.active_version_id == release.version_id else self.active_version_id,
        )

    def read_legacy(self, release_id: str) -> LegacyReadOnlyRecord | None:
        return next((record for record in self.legacy_records if record.release_id == release_id), None)

    def can_rerun(self, release_id: str) -> bool:
        record = self.read_legacy(release_id)
        return record is None or record.package_present
