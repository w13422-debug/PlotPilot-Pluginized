"""Closed, source-local transport projections for the read-only Plugin API.

These models are deliberately not public contracts.  P0 still owns public
error-family publication and runtime composition.  Keeping the projections
closed here prevents unvalidated authority mappings while that gate is open.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any, NoReturn

from plotpilot_core.plugins.package import VerifiedPackage
from plotpilot_core.supervisor import WorkerState, WorkerStatus
from plotpilot_plugin_sdk.errors import ContractValidationError
from plotpilot_plugin_sdk.verifier import assert_valid

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_HASH = re.compile(r"^[0-9a-f]{64}$")

LOCAL_ERROR_SCHEMA = "plugin-api-local-error/v1"


class PluginApiFault(Exception):
    """One finite, closed local HTTP failure pending P0 error publication."""

    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        super().__init__(message)

    def payload(self) -> dict[str, object]:
        return {
            "schema": LOCAL_ERROR_SCHEMA,
            "error_code": self.error_code,
            "message": self.message,
            "retryable": False,
        }


def _invalid(error_code: str, message: str) -> NoReturn:
    raise PluginApiFault(422, error_code, message)


def require_v1_id(value: str, *, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _invalid("invalid_identifier", f"{field} is not a v1 identifier")
    return value


def require_semver(value: str, *, field: str = "version") -> str:
    if not isinstance(value, str) or _SEMVER.fullmatch(value) is None:
        _invalid("invalid_semver", f"{field} is not an exact SemVer")
    return value


def require_sha256(value: str, *, field: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        _invalid("invalid_sha256", f"{field} is not lowercase SHA-256")
    return value


def _authority_contract_failure(message: str) -> NoReturn:
    raise PluginApiFault(502, "authority_contract_error", message)


def _closed_json(value: Any, *, label: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        _authority_contract_failure(f"{label} is not finite closed JSON: {exc}")


def project_release(package: VerifiedPackage) -> dict[str, object]:
    """Project one reverified PackageStore result without creating authority."""
    if not isinstance(package, VerifiedPackage):
        _authority_contract_failure("PackageStore returned an untyped release")
    try:
        plugin_id = require_v1_id(package.plugin_id, field="release.plugin_id")
        version = require_semver(package.version, field="release.version")
        package_hash = require_sha256(
            package.package_hash, field="release.package_hash"
        )
        release_id = require_sha256(package.release_id, field="release.release_id")
    except PluginApiFault as exc:
        _authority_contract_failure(exc.message)
    manifest = _closed_json(dict(package.manifest), label="release.manifest")
    try:
        assert_valid("plugin-manifest/v1", manifest)
    except ContractValidationError as exc:
        _authority_contract_failure(f"release.manifest failed its frozen validator: {exc}")
    if (
        manifest.get("plugin_id") != plugin_id
        or manifest.get("version") != version
        or manifest.get("kind") != package.kind
    ):
        _authority_contract_failure("release identity differs from its manifest")
    return {
        "schema": "plugin-release-view/v1",
        "plugin_id": plugin_id,
        "version": version,
        "kind": package.kind,
        "package_hash": package_hash,
        "release_id": release_id,
        "manifest": manifest,
    }


def project_release_list(
    packages: tuple[VerifiedPackage, ...],
) -> dict[str, object]:
    releases = [project_release(package) for package in packages]
    identities = [
        (item["plugin_id"].encode("utf-8"), item["version"].encode("utf-8"))
        for item in releases
    ]
    if identities != sorted(identities) or len(identities) != len(set(identities)):
        _authority_contract_failure(
            "PackageStore release inventory is not unique and canonically ordered"
        )
    return {"schema": "plugin-release-list/v1", "releases": releases}


def project_worker_status(status: WorkerStatus) -> dict[str, object]:
    """Project the closed Supervisor status DTO, including canonical Base64."""
    if not isinstance(status, WorkerStatus):
        _authority_contract_failure("Supervisor returned an untyped worker status")
    try:
        worker_id = require_v1_id(status.worker_id, field="status.worker_id")
        lifecycle_id = require_v1_id(
            status.lifecycle_id, field="status.lifecycle_id"
        )
        release_id = require_sha256(status.release_id, field="status.release_id")
        if status.worker_instance_id is not None:
            require_v1_id(
                status.worker_instance_id, field="status.worker_instance_id"
            )
    except PluginApiFault as exc:
        _authority_contract_failure(exc.message)
    if not isinstance(status.state, WorkerState):
        _authority_contract_failure("status.state is not a closed WorkerState")
    for field, value, minimum in (
        ("pin_epoch", status.pin_epoch, 1),
        ("retire_epoch", status.retire_epoch, 1),
        ("clients", status.clients, 0),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            _authority_contract_failure(f"status.{field} is outside its v1 range")
    if status.failure is not None and not isinstance(status.failure, str):
        _authority_contract_failure("status.failure is not a string or null")
    if status.exit_code is not None and (
        not isinstance(status.exit_code, int) or isinstance(status.exit_code, bool)
    ):
        _authority_contract_failure("status.exit_code is not an integer or null")
    if not isinstance(status.stderr_tail, bytes):
        _authority_contract_failure("status.stderr_tail is not bytes")
    stderr_tail_base64 = base64.b64encode(status.stderr_tail).decode("ascii")
    if base64.b64decode(stderr_tail_base64, validate=True) != status.stderr_tail:
        _authority_contract_failure("status.stderr_tail Base64 is not canonical")
    return {
        "schema": "plugin-worker-status-view/v1",
        "worker_id": worker_id,
        "lifecycle_id": lifecycle_id,
        "state": status.state.value,
        "release_id": release_id,
        "pin_epoch": status.pin_epoch,
        "retire_epoch": status.retire_epoch,
        "clients": status.clients,
        "worker_instance_id": status.worker_instance_id,
        "failure": status.failure,
        "exit_code": status.exit_code,
        "stderr_tail_base64": stderr_tail_base64,
    }


__all__ = [
    "LOCAL_ERROR_SCHEMA",
    "PluginApiFault",
    "project_release",
    "project_release_list",
    "project_worker_status",
    "require_semver",
    "require_sha256",
    "require_v1_id",
]
