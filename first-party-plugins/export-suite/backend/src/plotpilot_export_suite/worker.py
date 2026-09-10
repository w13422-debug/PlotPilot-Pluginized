"""Installable Host-Asset worker for the immutable Export Suite.

The worker deliberately owns no Core state.  The shared framed worker binds
the host-verified RunSnapshot; this adapter reads the snapshot-selected Assets,
delegates rendering to the accepted integration, and creates the immutable
export Asset, its ResultBundle Asset, and one Host terminal completion.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from .integration import (
    ExportPortError,
    ExportResult,
    build_export_from_snapshot,
    export_to_asset,
)

PLUGIN_ID = "com.plotpilot.export-suite"
PLUGIN_VERSION = "1.0.0"
CAPABILITY_ID = "writing.export/v1"
RESULT_CONTRACT = "artifact-bundle/v1"


class ExportWorkerError(ValueError):
    """Fail-closed worker context or Host-Asset adapter error."""


def _bound_snapshot(context: object) -> Mapping[str, Any]:
    snapshot = getattr(context, "run_snapshot", None)
    identity = getattr(context, "identity", None)
    if not isinstance(snapshot, Mapping) or identity is None:
        raise ExportWorkerError("Export worker requires a bound RunSnapshot identity")
    scope = snapshot.get("scope")
    if not isinstance(scope, Mapping) or scope.get("operation") != CAPABILITY_ID:
        raise ExportWorkerError("RunSnapshot operation is not Export")
    if getattr(identity, "capability_id", None) != CAPABILITY_ID:
        raise ExportWorkerError("worker capability is not Export")
    for snapshot_name, identity_name in (
        ("workspace_id", "workspace_id"),
        ("snapshot_id", "run_snapshot_id"),
        ("snapshot_hash", "run_snapshot_hash"),
    ):
        value = snapshot.get(snapshot_name)
        if not isinstance(value, str) or value != getattr(identity, identity_name, None):
            raise ExportWorkerError(f"RunSnapshot {snapshot_name} mismatches worker identity")
    return snapshot


def _identity_text(identity: object, name: str) -> str:
    value = getattr(identity, name, None)
    if not isinstance(value, str) or not value:
        raise ExportWorkerError(f"worker identity {name} is missing")
    return value


def _identity_epoch(identity: object) -> int:
    value = getattr(identity, "lease_epoch", None)
    if type(value) is not int or value < 1:
        raise ExportWorkerError("worker identity lease_epoch is invalid")
    return value


def _operation_key(prefix: str, *parts: str) -> str:
    try:
        material = "\n".join(parts).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ExportWorkerError(f"{prefix} identity is not ASCII") from exc
    return f"{prefix}-{sha256(material).hexdigest()}"


class _WorkerAssetPort:
    """Translate the shared HostAssetClient into accepted Export Asset seams."""

    def __init__(self, context: object, snapshot: Mapping[str, Any]) -> None:
        self._assets = getattr(context, "assets", None)
        self._snapshot_hash = snapshot["snapshot_hash"]

    @staticmethod
    def _asset_parts(asset: object, expected_id: str) -> tuple[bytes, str]:
        asset_id = getattr(asset, "asset_id", None)
        content = getattr(asset, "content", None)
        content_hash = getattr(asset, "sha256", None)
        if asset_id != expected_id or not isinstance(content, bytes):
            raise ExportWorkerError("Host Asset read does not preserve immutable identity")
        if not isinstance(content_hash, str) or sha256(content).hexdigest() != content_hash:
            raise ExportWorkerError("Host Asset read hash does not match immutable bytes")
        return content, content_hash

    def read_asset(self, asset_id: str) -> tuple[bytes, Mapping[str, object]]:
        reader = getattr(self._assets, "read", None)
        if not callable(reader):
            raise ExportWorkerError("Export worker requires host.asset.read/v1")
        content, content_hash = self._asset_parts(reader(asset_id), asset_id)
        return content, {
            "schema": "asset-metadata/v1",
            "asset_id": asset_id,
            "sha256": content_hash,
            "size": len(content),
        }

    def create_asset(self, content: bytes, *, mime: str) -> Mapping[str, object]:
        creator = getattr(self._assets, "create", None)
        if not callable(creator):
            raise ExportWorkerError("Export worker requires host.asset.create/v1")
        data = bytes(content)
        content_hash = sha256(data).hexdigest()
        operation_key = _operation_key(
            "export-output", self._snapshot_hash, content_hash
        )
        asset = creator(data, mime=mime, operation_key=operation_key)
        asset_id = getattr(asset, "asset_id", None)
        returned_content, returned_hash = self._asset_parts(asset, asset_id)
        if returned_content != data or returned_hash != content_hash:
            raise ExportWorkerError("Host Asset create changed Export bytes or identity")
        return {
            "schema": "asset-metadata/v1",
            "asset_id": asset_id,
            "sha256": returned_hash,
            "mime": mime,
            "size": len(data),
            "logical_role": "export_output",
            "provenance": "plotpilot:export-suite",
            "rebuildable": True,
        }


def export_from_worker_context(
    context: object,
    *,
    novel_id: str = "novel",
    title: str = "",
    author: str = "",
    premise: str = "",
    export_format: str = "markdown",
    document_id: str | None = None,
) -> ExportResult:
    """Render through the accepted integration and create only an output Asset."""

    snapshot = _bound_snapshot(context)
    assets = _WorkerAssetPort(context, snapshot)
    prepared = build_export_from_snapshot(
        snapshot,
        assets,
        novel_id=novel_id,
        title=title,
        author=author,
        premise=premise,
        export_format=export_format,
        document_id=document_id,
    )
    return export_to_asset(prepared, assets)


def _sdk_bundle_tools() -> tuple[object, object]:
    """Resolve the public canonical/result verifier only when a Job runs."""

    try:
        from plotpilot_plugin_sdk.canonical import canonical_bytes
        from plotpilot_plugin_sdk.verifier import verify_result_bundle
    except ModuleNotFoundError:  # pragma: no cover - repository-source fallback
        from backend.plotpilot_plugin_sdk.canonical import canonical_bytes
        from backend.plotpilot_plugin_sdk.verifier import verify_result_bundle
    return canonical_bytes, verify_result_bundle


def _result_bundle(
    result: ExportResult,
    identity: object,
) -> Mapping[str, object]:
    """Project the one immutable export into the public artifact contract."""

    workspace_id = _identity_text(identity, "workspace_id")
    run_snapshot_hash = _identity_text(identity, "run_snapshot_hash")
    receipt = result.receipt
    source_refs = [
        {
            "workspace_id": workspace_id,
            "source_type": "core.chapter",
            "source_id": document_id,
            "revision_or_hash": f"{revision_id}:{content_hash}",
        }
        for document_id, revision_id, content_hash in receipt.source_revisions
    ]
    bundle = {
        "schema": "result-bundle/v1",
        "contract_id": RESULT_CONTRACT,
        "bundle_id": f"export-bundle-{receipt.receipt_hash}",
        "bundle_type": "artifact",
        "producer": {
            "plugin_id": PLUGIN_ID,
            "release_id": _identity_text(identity, "plugin_release_id"),
            "capability_id": _identity_text(identity, "capability_id"),
            "job_id": _identity_text(identity, "job_id"),
            "step_id": _identity_text(identity, "step_id"),
            "attempt_id": _identity_text(identity, "attempt_id"),
            "lease_epoch": _identity_epoch(identity),
        },
        "input_snapshot_hash": run_snapshot_hash,
        "items": [
            {
                "schema": "artifact-item/v1",
                "item_id": f"export-artifact-{result.output_hash}",
                "artifact_kind": CAPABILITY_ID,
                "payload_asset_id": result.asset_id,
                "payload_hash": result.output_hash,
                "mime": result.asset_mime,
                "source_refs": source_refs,
                "status": "complete",
            }
        ],
        "warnings": [],
        "partial": False,
        "provenance_receipt_id": receipt.receipt_id,
        "skill_chain_result_refs": [],
    }
    _, verify_result_bundle = _sdk_bundle_tools()
    try:
        verify_result_bundle(
            bundle,
            snapshot_workspace_id=workspace_id,
            snapshot_hash_value=run_snapshot_hash,
        )
    except Exception as exc:
        raise ExportWorkerError("Export ResultBundle is invalid") from exc
    return bundle


def _persist_result_bundle(
    context: object,
    bundle: Mapping[str, object],
    identity: object,
) -> str:
    canonical_bytes, _ = _sdk_bundle_tools()
    try:
        data = canonical_bytes(bundle)
    except Exception as exc:
        raise ExportWorkerError("Export ResultBundle cannot be canonicalized") from exc
    expected_hash = sha256(data).hexdigest()
    creator = getattr(getattr(context, "assets", None), "create", None)
    if not callable(creator):
        raise ExportWorkerError("Export worker requires host.asset.create/v1")
    asset = creator(
        data,
        mime="application/json",
        operation_key=_operation_key(
            "export-result-bundle",
            _identity_text(identity, "run_snapshot_hash"),
            expected_hash,
        ),
    )
    asset_id = getattr(asset, "asset_id", None)
    content = getattr(asset, "content", None)
    content_hash = getattr(asset, "sha256", None)
    if (
        not isinstance(asset_id, str)
        or content != data
        or content_hash != expected_hash
    ):
        raise ExportWorkerError("Host Asset create changed Export ResultBundle bytes or identity")
    return asset_id


def _run_export_job(
    _params: Mapping[str, Any],
    context: object,
) -> Mapping[str, object]:
    """Complete one host-bound export without Candidate, Publication, or file I/O."""

    result = export_from_worker_context(context)
    identity = getattr(context, "identity", None)
    if identity is None:
        raise ExportWorkerError("Export worker lost its Attempt identity")
    bundle = _result_bundle(result, identity)
    bundle_asset_id = _persist_result_bundle(context, bundle, identity)
    host_call = getattr(getattr(context, "host", None), "call", None)
    if not callable(host_call):
        raise ExportWorkerError("Export worker requires host.job.complete/v1")
    completion = host_call(
        "host.job.complete/v1",
        {
            "operation_key": _operation_key(
                "export-complete",
                _identity_text(identity, "operation_id"),
                result.receipt.receipt_id,
                bundle_asset_id,
            ),
            "worker_run_id": _identity_text(identity, "operation_id"),
            "outcome": "succeeded",
            "result_bundle_asset_id": bundle_asset_id,
            "candidate_stage_operation_key": None,
            "terminal_detail_asset_id": None,
            "local_seq": 1,
        },
    )
    if (
        not isinstance(completion, Mapping)
        or completion.get("accepted") is not True
        or completion.get("provenance_receipt_id") != result.receipt.receipt_id
    ):
        raise ExportWorkerError("Host completion did not preserve Export receipt")
    return {
        "accepted": True,
        "worker_run_id": _identity_text(identity, "operation_id"),
        "provenance_receipt_id": result.receipt.receipt_id,
        "output_streams": [],
    }


def capability_descriptor(*, release_id: str) -> Mapping[str, object]:
    """Describe the one deterministic Asset/receipt Export capability."""

    return {
        "schema": "capability-provider/v1",
        "capability_id": CAPABILITY_ID,
        "provider": {"plugin_id": PLUGIN_ID, "release_id": release_id},
        "input_schema": "run-snapshot/v1",
        "output_schema": "export-receipt/v1",
        "result_contract": RESULT_CONTRACT,
        "supports": ["run"],
        "deterministic": True,
        "accepted_data_formats": [],
    }


def create_worker() -> object:
    """Create the sole shared framed-stdio production worker."""

    try:
        from plotpilot_plugin_sdk.stdio_worker import FramedStdioWorker
    except ModuleNotFoundError:  # pragma: no cover - repository-source fallback
        from backend.plotpilot_plugin_sdk.stdio_worker import FramedStdioWorker

    worker = FramedStdioWorker(plugin_id=PLUGIN_ID, plugin_version=PLUGIN_VERSION)
    worker.register_domain(
        CAPABILITY_ID,
        descriptor=lambda release_id: capability_descriptor(release_id=release_id),
        operations=(CAPABILITY_ID,),
        start=_run_export_job,
    )
    return worker


def main() -> None:
    """Run the package entrypoint declared by ``plugin.json``."""

    create_worker().serve()


__all__ = [
    "CAPABILITY_ID",
    "PLUGIN_ID",
    "RESULT_CONTRACT",
    "ExportPortError",
    "ExportWorkerError",
    "capability_descriptor",
    "create_worker",
    "export_from_worker_context",
    "main",
]
