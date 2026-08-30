"""Run the complete P0 contract, golden and corpus verification matrix.

This command is intentionally dependency-light and does not start PlotPilot or
touch any user data. It is the machine-readable M0 gate used by both CI and
the final M0-OPEN manifest.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from jsonschema import Draft202012Validator  # noqa: E402

from plotpilot_plugin_sdk.canonical import canonical_bytes, sha256_hex  # noqa: E402
from plotpilot_plugin_sdk.errors import ContractError, ContractValidationError, ErrorCode  # noqa: E402
from plotpilot_plugin_sdk.fake_provider import FakeProvider  # noqa: E402
from plotpilot_plugin_sdk.fixtures import PluginUIHostFixture  # noqa: E402
from plotpilot_plugin_sdk.context_identity import (  # noqa: E402
    derive_operation_context_identity,
    operation_context_projection,
)
from plotpilot_plugin_sdk.core_api import (  # noqa: E402
    CoreHttpContractFixture,
    parse_asset_contract,
    parse_core_authority,
    parse_core_http_request_error,
    parse_core_http_request_failure_policy,
    parse_export_current_revisions,
    parse_publication,
    verify_export_current_revisions_asset,
    verify_asset_metadata_range_pair,
)
from plotpilot_plugin_sdk.core_api_v2 import (  # noqa: E402
    parse_candidate_query_result_v2,
    parse_candidate_review_v2,
    parse_candidate_v2,
    parse_core_authority_v2,
    parse_job_event_page_v2,
    parse_job_http_v2,
    parse_job_snapshot_v2,
    parse_job_sse_recovery_v2,
    parse_plugin_api_v2,
    validate_candidate_v2,
    validate_plugin_lifecycle_v2,
    validate_publication_v2,
    validate_story_state_projection_v2,
)
from plotpilot_plugin_sdk.m4_m5_http_v2 import (  # noqa: E402
    OperationKeyLedgerV2,
    parse_http_request,
    parse_http_response,
    validate_http_exchange,
)
from plotpilot_plugin_sdk.package import build_files_sha256, normalize_relative_path, package_hash  # noqa: E402
from plotpilot_plugin_sdk.rpc import (  # noqa: E402
    ChunkUploadLedger,
    OperationLedger,
    build_meta,
    build_notification,
    build_request,
    enforce_lease,
)
from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    EXPECTED_ERROR_CODES,
    EXPECTED_HOST_METHODS,
    EXPECTED_WORKER_METHODS,
    assert_valid,
    hash_without_field,
    load_strict_json,
    request_key_bytes,
    validate_rpc_request,
    validate_rpc_response,
    validate_rpc_result,
    verify_backup,
    verify_checkpoint,
    verify_capability_descriptor,
    verify_compatibility,
    verify_core_snapshot,
    verify_data_bundle,
    verify_manifest,
    verify_package_identity,
    verify_package_manifest,
    verify_plan,
    verify_provenance_receipt,
    verify_result_bundle,
    verify_restore_report,
    verify_skill_chain,
    verify_skill_identity,
    verify_skill_receipt,
    verify_snapshot,
    verify_sse_recovery,
    verify_stream_prefix,
    verify_settings_validation_receipt,
    verify_job_snapshot,
    verify_history_bytes,
    snapshot_hash,
)

SCHEMA_DIR = ROOT / "contracts" / "json-schema"
EXAMPLES_DIR = ROOT / "contracts" / "examples"
FIXTURES_DIR = EXAMPLES_DIR / "fixtures"
GOLDEN_DIR = ROOT / "contracts" / "golden"
CORPUS_DIR = ROOT / "contracts" / "corpus"


# These small deterministic models are intentionally local to the contract
# gate.  They model the durable boundaries described by §84.13 without
# pretending to be the product's database or UI implementation.  Every crash
# probe first mutates a stateful transaction and then attempts the operation
# that must be fenced; therefore a probe can only pass when the model reaches
# the unsafe state through the transition under test.
@dataclass
class InstallTransactionModel:
    current_generation_id: str = "generation-current"
    lkg_generation_id: str | None = "generation-lkg"
    state: str = "selected"
    base_generation_id: str | None = None
    target_generation_id: str | None = None
    package_store_status: str = "absent"
    settings_valid: bool = False
    projection_ready: bool = False
    safe_mode: bool = False
    reconciliation_required: bool = False
    reconciled: bool = False
    rollback_attempt: int = 0
    rollback_token: str | None = None
    crash_point: str | None = None
    history: list[str] = field(default_factory=list)

    _PHASES = (
        "staged",
        "package_published",
        "env_prepared",
        "shadow_prepared",
        "migrated",
        "settings_validated",
        "qualified",
        "pending_apply",
        "current_committed",
        "lkg_pending",
        "lkg_promoted",
    )

    def _fail(self, code: ErrorCode, message: str) -> None:
        raise ContractError(code, message)

    def begin(self, base_generation_id: str, target_generation_id: str = "generation-next") -> None:
        if self.state != "selected":
            self._fail(ErrorCode.INVALID_TRANSITION, "install transition is not selectable")
        if base_generation_id != self.current_generation_id:
            self._fail(ErrorCode.INVALID_TRANSITION, "install CAS base generation changed")
        self.base_generation_id = base_generation_id
        self.target_generation_id = target_generation_id
        self.state = "staged"
        self.package_store_status = "staged"
        self.history.append(self.state)

    def advance(self, phase: str) -> None:
        if phase not in self._PHASES:
            raise ValueError(f"unknown install phase: {phase}")
        expected_index = self._PHASES.index(self.state) + 1 if self.state in self._PHASES else 0
        if self._PHASES.index(phase) != expected_index:
            self._fail(ErrorCode.INVALID_TRANSITION, f"install phase {self.state}->{phase} is not durable order")
        self.state = phase
        if phase == "package_published":
            self.package_store_status = "published"
        if phase == "settings_validated":
            self.settings_valid = True
        if phase in {"qualified", "pending_apply", "current_committed", "lkg_pending", "lkg_promoted"}:
            self.projection_ready = True
        self.history.append(phase)

    def _mark_crashed(self, point: str) -> None:
        self.crash_point = point
        self.safe_mode = True
        self.reconciliation_required = True
        self.reconciled = False
        self.state = "safe_mode"
        self.history.append(f"crash:{point}")

    def install_until_crash(self, base_generation_id: str, point: str) -> None:
        points = {
            "before_prepare": None,
            "after_staging": "staged",
            "after_activate": "current_committed",
            "after_settings": "settings_validated",
            "after_projection": "qualified",
        }
        if point not in points:
            raise ValueError(f"unknown install crash point: {point}")
        if point == "before_prepare":
            if base_generation_id != self.current_generation_id:
                self._fail(ErrorCode.INVALID_TRANSITION, "install CAS base generation changed")
            self._mark_crashed(point)
            return
        self.begin(base_generation_id)
        desired = points[point]
        assert desired is not None
        # ``begin`` durably reaches ``staged``.  Advance through the desired
        # phase (inclusive); replaying ``staged`` would be an invalid second
        # transition and would make the crash probe test the fixture setup
        # rather than the requested durable boundary.
        for phase in self._PHASES[1:]:
            self.advance(phase)
            if phase == desired:
                break
        self._mark_crashed(point)

    def reconcile(self) -> None:
        if not self.safe_mode or not self.reconciliation_required:
            self._fail(ErrorCode.INVALID_TRANSITION, "install has no safe-mode transaction to reconcile")
        # Startup reconciliation never activates a partially installed
        # generation.  It restores the durable current/LKG pointers and arms
        # a single rollback token when a transition had reached activation.
        self.rollback_token = self.rollback_token or f"rollback:{self.crash_point}"
        self.rollback_attempt = min(self.rollback_attempt, 1)
        self.reconciliation_required = False
        self.reconciled = True
        self.state = "rollback_armed"
        self.history.append("reconciled")

    def exit_safe_mode(self) -> None:
        if not self.safe_mode:
            self._fail(ErrorCode.INVALID_TRANSITION, "install is not in safe mode")
        if self.reconciliation_required or not self.reconciled:
            self._fail(ErrorCode.INVALID_TRANSITION, "safe-mode exit requires reconciliation")
        self.safe_mode = False
        self.state = "rolled_back"
        self.history.append("safe_mode_exited")

    def rollback_once(self) -> None:
        if not self.safe_mode or not self.reconciled or self.rollback_token is None:
            self._fail(ErrorCode.INVALID_TRANSITION, "rollback requires a reconciled safe-mode transition")
        if self.rollback_attempt >= 1:
            self._fail(ErrorCode.INVALID_TRANSITION, "rollback is one-shot")
        self.rollback_attempt = 1
        self.current_generation_id = self.base_generation_id or self.current_generation_id
        self.state = "rolled_back"
        self.history.append("rollback")

    def validate_settings(self, receipt: Mapping[str, Any], settings_revision: Mapping[str, Any]) -> None:
        verify_settings_validation_receipt(receipt)
        assert_valid("settings-revision/v1", settings_revision)
        if receipt["settings_revision_id"] != settings_revision["settings_revision_id"]:
            self._fail(ErrorCode.RESULT_CONTRACT_MISMATCH, "settings validation receipt is not bound to the revision")
        if receipt["plugin_release_id"] != settings_revision["plugin_release_id"]:
            self._fail(ErrorCode.RESULT_CONTRACT_MISMATCH, "settings validation receipt is not bound to the release")
        if not receipt["valid"]:
            self._fail(ErrorCode.SETTINGS_INVALID, "settings validator rejected the revision")
        self.settings_valid = True


@dataclass
class PublicationModel:
    release_id: str
    state: str = "installed"
    package_present: bool = True
    retire_epoch: int = 1
    active_pins: int = 0
    recoverable_attempts: int = 0
    retained_receipts: set[str] = field(default_factory=set)
    published_candidates: set[str] = field(default_factory=set)

    def pin(self) -> None:
        if self.state != "installed":
            raise ContractError(ErrorCode.RELEASE_RETIRING, "release is retiring and cannot receive a new pin")
        self.active_pins += 1

    def add_recoverable_attempt(self) -> None:
        self.recoverable_attempts += 1

    def retire(self) -> None:
        if self.state != "installed":
            raise ContractError(ErrorCode.RELEASE_RETIRING, "release is already retiring")
        if self.active_pins or self.recoverable_attempts:
            raise ContractError(ErrorCode.RELEASE_RETIRING, "recoverable references block retirement")
        self.state = "retiring"
        self.retire_epoch += 1

    def retain_provenance(self, receipt: Mapping[str, Any]) -> None:
        verify_provenance_receipt(receipt)
        if receipt["release_id"] != self.release_id:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "retained provenance belongs to another release")
        self.retained_receipts.add(receipt["receipt_id"])

    def delete_package(self) -> None:
        if self.state not in {"retiring", "retired"} or self.active_pins or self.recoverable_attempts:
            raise ContractError(ErrorCode.RELEASE_RETIRING, "release still has executable references")
        self.package_present = False
        self.state = "retired"

    def publish(self, bundle: Mapping[str, Any], receipt: Mapping[str, Any], *, snapshot: Mapping[str, Any]) -> None:
        verify_result_bundle(
            bundle,
            snapshot_workspace_id=snapshot["workspace_id"],
            snapshot_hash_value=snapshot["snapshot_hash"],
        )
        verify_provenance_receipt(receipt)
        if bundle["provenance_receipt_id"] != receipt["receipt_id"]:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "publication bundle did not propagate its provenance receipt")
        if not self.package_present and receipt["receipt_id"] not in self.retained_receipts:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "publication requires retained provenance after package deletion")
        if receipt["release_id"] != self.release_id:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "publication provenance release does not match")
        self.published_candidates.update(item["item_id"] for item in bundle["items"])


@dataclass
class CoreEventTransactionModel:
    aggregate_id: str
    durable_revision: int = 0
    aggregate_revision: int = 0
    durable_event_seq: int = 0
    pending_event: Mapping[str, Any] | None = None
    crash_point: str | None = None
    broadcasts: list[Mapping[str, Any]] = field(default_factory=list)

    def append(self, event: Mapping[str, Any], *, crash_point: str | None = None) -> None:
        assert_valid("core-event/v1", event)
        if event["aggregate_id"] != self.aggregate_id:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Core Event aggregate is not bound")
        if event["aggregate_revision"] != self.durable_revision + 1 or event["core_event_seq"] != self.durable_event_seq + 1:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "Core Event revision is not the next durable revision")
        self.pending_event = copy.deepcopy(dict(event))
        self.crash_point = crash_point
        next_revision = self.durable_revision + 1
        if crash_point == "event-crash-after-write":
            # Simulate the aggregate page being written before the Event row;
            # the transaction remains uncommitted and must not be broadcast.
            self.aggregate_revision = next_revision
            return
        if crash_point == "aggregate-event-crash":
            return
        if crash_point is not None:
            raise ValueError(f"unknown Core Event crash point: {crash_point}")
        self.aggregate_revision = next_revision
        self.durable_revision = next_revision
        self.durable_event_seq = int(event["core_event_seq"])
        self.pending_event = None
        self.broadcasts.append(copy.deepcopy(dict(event)))

    def publish_sse(self) -> None:
        if self.pending_event is not None or self.aggregate_revision != self.durable_revision:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "Core Event transaction is not durably committed")
        if not self.broadcasts:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "Core Event has no committed broadcast")

    def recover(self) -> None:
        if self.pending_event is None and self.aggregate_revision == self.durable_revision:
            return
        self.aggregate_revision = self.durable_revision
        self.pending_event = None
        self.crash_point = None


@dataclass
class TerminalHydrationModel:
    snapshot: Mapping[str, Any]
    job_snapshot: Mapping[str, Any]
    durable_bundle: Mapping[str, Any] | None = None
    terminal_snapshot: Mapping[str, Any] | None = None

    def commit(self, bundle: Mapping[str, Any]) -> None:
        verify_result_bundle(
            bundle,
            snapshot_workspace_id=self.snapshot["workspace_id"],
            snapshot_hash_value=self.snapshot["snapshot_hash"],
        )
        terminal = copy.deepcopy(dict(self.job_snapshot))
        terminal["job_state"] = "succeeded"
        terminal["job_revision"] = int(terminal["job_revision"]) + 1
        terminal["candidate_ids"] = [item["item_id"] for item in bundle["items"]]
        terminal["steps"] = [
            {**step, "state": "succeeded", "revision": int(step["revision"]) + 1}
            for step in terminal["steps"]
        ]
        terminal["attempts"] = [
            {**attempt, "state": "succeeded"}
            for attempt in terminal["attempts"]
        ]
        terminal["snapshot_hash"] = hash_without_field(terminal, "snapshot_hash", "job-snapshot/v1")
        verify_job_snapshot(terminal)
        self.terminal_snapshot = terminal
        self.durable_bundle = copy.deepcopy(dict(bundle))

    def hydrate(self) -> dict[str, Any]:
        if self.durable_bundle is None or self.terminal_snapshot is None:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "terminal view has no durable bundle or snapshot")
        verify_job_snapshot(self.terminal_snapshot)
        verify_result_bundle(
            self.durable_bundle,
            snapshot_workspace_id=self.snapshot["workspace_id"],
            snapshot_hash_value=self.snapshot["snapshot_hash"],
        )
        if self.terminal_snapshot["job_state"] not in {"succeeded", "partial", "failed", "cancelled"}:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "terminal hydration did not reconstruct terminal state")
        return copy.deepcopy(dict(self.durable_bundle))


@dataclass
class RestoreTransactionModel:
    source_root_id: str
    target_root_id: str
    state: str = "selected"
    projection_markers: tuple[tuple[str, str], ...] = ()
    rebuilt_projections: set[tuple[str, str]] = field(default_factory=set)
    recovery_required: bool = False

    def stage(self, backup: Mapping[str, Any], *, crash_at: str | None = None) -> None:
        verify_backup(backup)
        if self.source_root_id == self.target_root_id:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "restore staging must use a distinct target root")
        self.state = "staged"
        self.projection_markers = tuple(
            (entry["plugin_id"], entry["release_id"])
            for entry in backup["projection_rebuild_required"]
        )
        stages = ("manifest_verified", "snapshot_verified", "assets_verified", "plugins_resolved")
        for stage in stages:
            if crash_at == stage:
                self.state = "staging"
                self.recovery_required = True
                return
            self.state = stage
        self.state = "restore_ready"

    def rebuild_projections(self) -> None:
        if self.state not in {"plugins_resolved", "restore_ready"}:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "projection rebuild requires verified restore staging")
        self.rebuilt_projections.update(self.projection_markers)
        if self.state == "plugins_resolved":
            self.state = "restore_ready"

    def switch(self) -> None:
        if self.recovery_required or self.state != "restore_ready":
            raise ContractError(ErrorCode.INVALID_TRANSITION, "restore staging requires recovery before switching")
        if set(self.projection_markers) != self.rebuilt_projections:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "restore omitted required projection rebuild")
        self.state = "switched"


@dataclass
class WorkerRuntimeModel:
    worker_url: str
    csp: str
    navigation_limit: int = 3
    navigation_count: int = 0
    stopped: bool = False

    STRICT_CSP = "default-src 'none'; connect-src 'none'; script-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'none'"

    def receive(self, worker_url: str, csp: str) -> None:
        if worker_url != self.worker_url:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "worker URL is immutable for a generation")
        if csp != self.csp:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "worker CSP is immutable")

    def navigate(self) -> int:
        if self.stopped:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "navigation watchdog already stopped the worker")
        self.navigation_count += 1
        if self.navigation_count > self.navigation_limit:
            self.stopped = True
            raise ContractError(ErrorCode.INVALID_TRANSITION, "UI navigation watchdog stopped a render loop")
        return self.navigation_count


ExecutableCase = Callable[[], Any]
ExecutableCaseFactory = Callable[[Mapping[str, Any]], ExecutableCase]

# The registry is an extension seam for future §84.13 corpus groups.  The
# checked-in legacy cases remain local to verify_negative_cases because they
# need its fixture context; callers may register additional executable cases
# without changing this verifier or the formal corpus schema.
EXECUTABLE_CASE_REGISTRY: dict[str, ExecutableCase] = {}
EXECUTABLE_KIND_REGISTRY: dict[str, ExecutableCaseFactory] = {}


def register_executable_case(case_id: str, action: ExecutableCase, *, replace: bool = False) -> None:
    """Register a callable for a corpus ``case_id``.

    Registration is deliberately process-local: it does not mutate the
    corpus, fixtures or any generated manifest.  ``replace=True`` is explicit
    so a downstream expanded corpus cannot silently shadow an existing case.
    """
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("executable case_id must be a non-empty string")
    if not callable(action):
        raise TypeError(f"executable case {case_id!r} must be callable")
    if case_id in EXECUTABLE_CASE_REGISTRY and not replace:
        raise ValueError(f"executable case already registered: {case_id}")
    EXECUTABLE_CASE_REGISTRY[case_id] = action


def register_executable_kind(kind: str, factory: ExecutableCaseFactory, *, replace: bool = False) -> None:
    """Register a reusable factory for an expanded corpus ``kind``.

    A corpus entry is intentionally identified by both ``case_id`` and
    ``kind``.  Downstream corpus expansions can therefore add a new case ID
    while reusing an existing executable assertion, without this gate having
    to grow another fixed case-ID allow-list.
    """
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("executable case kind must be a non-empty string")
    if not callable(factory):
        raise TypeError(f"executable case kind {kind!r} must be callable")
    if kind in EXECUTABLE_KIND_REGISTRY and not replace:
        raise ValueError(f"executable case kind already registered: {kind}")
    EXECUTABLE_KIND_REGISTRY[kind] = factory


def _corpus_group_sort_key(group_id: str) -> tuple[int, int]:
    match = re.fullmatch(r"84\.13-(\d+)", group_id)
    if match is None:
        raise AssertionError(f"invalid expanded §84.13 group id: {group_id!r}")
    return (int(match.group(1)), len(match.group(1)))


def validate_executable_case_registry(
    registry: Mapping[str, ExecutableCase] | None = None,
    *,
    corpus_groups: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, int]:
    """Validate the executable-case seam against any current corpus expansion.

    No fixed list of old case IDs is used.  When ``corpus_groups`` is given,
    every negative entry must resolve to a callable and a case cannot appear
    in two groups.  The return value is suitable for gate evidence.
    """
    selected = dict(EXECUTABLE_CASE_REGISTRY if registry is None else registry)
    for case_id, action in selected.items():
        if not isinstance(case_id, str) or not case_id.strip() or not callable(action):
            raise AssertionError(f"invalid executable case registry entry: {case_id!r}")
    if corpus_groups is None:
        return {"registered_cases": len(selected), "corpus_groups": 0, "corpus_cases": 0}
    seen_groups: set[str] = set()
    seen_cases: set[str] = set()
    group_count = 0
    case_count = 0
    for group in corpus_groups:
        group_id = group.get("group_id")
        if not isinstance(group_id, str) or group_id in seen_groups:
            raise AssertionError(f"duplicate/invalid executable corpus group: {group_id!r}")
        _corpus_group_sort_key(group_id)
        seen_groups.add(group_id)
        group_count += 1
        negative = group.get("negative")
        if not isinstance(negative, list) or not negative:
            raise AssertionError(f"{group_id} must contain negative cases")
        for case in negative:
            case_id = case.get("case_id") if isinstance(case, Mapping) else None
            if not isinstance(case_id, str) or case_id not in selected:
                raise AssertionError(f"no executable mapping for {case_id!r} in {group_id}")
            if case_id in seen_cases:
                raise AssertionError(f"negative case is assigned to multiple groups: {case_id}")
            seen_cases.add(case_id)
            case_count += 1
    return {"registered_cases": len(selected), "corpus_groups": group_count, "corpus_cases": case_count}


def build_executable_case_registry(
    corpus_groups: Iterable[Mapping[str, Any]],
    *,
    registry: Mapping[str, ExecutableCase] | None = None,
    kind_registry: Mapping[str, ExecutableCaseFactory] | None = None,
) -> dict[str, ExecutableCase]:
    """Resolve corpus entries to executable actions through ID or kind.

    Explicit case-ID registrations always win.  Missing IDs are filled from
    the reusable kind factories.  This is the extension seam used by the
    expanded §84.13 corpus and is deliberately independent of the current
    fourteen group names.
    """
    selected = dict(EXECUTABLE_CASE_REGISTRY if registry is None else registry)
    factories = dict(EXECUTABLE_KIND_REGISTRY if kind_registry is None else kind_registry)
    for group in corpus_groups:
        negative = group.get("negative")
        if not isinstance(negative, list):
            continue
        for case in negative:
            if not isinstance(case, Mapping):
                continue
            case_id = case.get("case_id")
            if not isinstance(case_id, str) or case_id in selected:
                continue
            kind = case.get("kind")
            factory = factories.get(kind) if isinstance(kind, str) else None
            if factory is None:
                continue
            action = factory(case)
            if not callable(action):
                raise TypeError(f"executable case factory {kind!r} did not return a callable")
            selected[case_id] = action
    return selected


def _regenerated_self_hash(filename: str, field: str, prefix: str) -> dict[str, Any]:
    """Load a self-hashed positive fixture without repairing its digest.

    ``field`` and ``prefix`` are retained in the helper signature so callers
    state the exact v1 formula next to the fixture.  The verifier itself must
    see the checked-in digest; silently regenerating it would turn a tampered
    positive fixture into a false positive.
    """
    del field, prefix
    return load_strict_json(FIXTURES_DIR / filename)


def _expect_failure(function: Callable[[], Any], code: int | ErrorCode | None = None) -> None:
    try:
        function()
    except (ContractError, ContractValidationError) as exc:
        if code is not None and exc.code != int(code):
            raise AssertionError(f"expected error {int(code)}, got {exc.code}: {exc}") from exc
        return
    except Exception:
        if code is None:
            return
        raise
    raise AssertionError("negative fixture unexpectedly succeeded")


def verify_schemas() -> dict[str, Any]:
    generator = ROOT / "tools" / "integration" / "generate_contract_schemas.py"
    check = subprocess.run(
        [sys.executable, str(generator), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check.returncode:
        raise AssertionError(check.stdout + check.stderr)
    paths = sorted(SCHEMA_DIR.glob("*.schema.json"))
    v1_paths = [path for path in paths if not path.name.endswith("-v2.schema.json")]
    v2_paths = [path for path in paths if path.name.endswith("-v2.schema.json")]
    if len(v1_paths) != 55 or len(v2_paths) != 6:
        raise AssertionError(f"expected 55 v1 + 6 v2 Draft 2020-12 schemas, found {len(v1_paths)} + {len(v2_paths)}")
    for path in paths:
        schema = load_strict_json(path)
        Draft202012Validator.check_schema(schema)

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object" and node.get("additionalProperties") is not False:
                    raise AssertionError(f"open object in {path}")
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(schema)
    for name in ("plugin-manifest-v1.schema.json", "rpc-request-v1.schema.json", "rpc-envelope-v1.schema.json", "plugin-ui-message-v1.schema.json", "core-authority-command-query-v1.schema.json", "publication-command-result-v1.schema.json", "asset-metadata-v1.schema.json", "operation-context-identity-v1.schema.json"):
        schema = load_strict_json(SCHEMA_DIR / name)
        if "oneOf" in schema and schema.get("unevaluatedProperties") is not False:
            raise AssertionError(f"union root {name} must set unevaluatedProperties:false")
    return {"schemas": len(paths), "v1_schemas": len(v1_paths), "v2_schemas": len(v2_paths), "generator_check": check.stdout.strip()}


def verify_contract_manifest() -> dict[str, Any]:
    """Verify the checked-in content inventory and its generated hashes."""
    generator = ROOT / "tools" / "integration" / "generate_contract_manifest.py"
    check = subprocess.run(
        [sys.executable, str(generator), "--all", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check.returncode:
        raise AssertionError(check.stdout + check.stderr)
    manifest_path = ROOT / "contracts" / "manifest-v1.json"
    manifest = load_strict_json(manifest_path)
    if manifest.get("schema") != "plotpilot-contract-manifest/v1":
        raise AssertionError("contract manifest schema drift")
    if manifest.get("source", {}).get("sha256") != "e70f450b75cfa753148cf620d13855d0073f7d61294e7599ea9cc98f3e612b7b":
        raise AssertionError("formal design hash drift")
    records = manifest.get("files")
    if not isinstance(records, list) or len(records) != manifest["inventory"]["file_count_excluding_manifest"]:
        raise AssertionError("contract manifest file inventory count drift")
    seen: set[str] = set()
    for record in records:
        path = ROOT / record["path"]
        if record["path"] in seen or not path.is_file():
            raise AssertionError(f"contract manifest path missing/duplicated: {record['path']}")
        seen.add(record["path"])
        if path.stat().st_size != record["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise AssertionError(f"contract manifest hash drift: {record['path']}")
    if manifest["inventory"]["schema_count"] != 55 or manifest["inventory"]["negative_group_count"] != 14:
        raise AssertionError("contract manifest inventory does not cover the full M0 contract set")
    v2_manifest_path = ROOT / "contracts" / "manifest-v2.json"
    v2_manifest = load_strict_json(v2_manifest_path)
    if v2_manifest.get("schema") != "plotpilot-contract-manifest/v2":
        raise AssertionError("v2 contract manifest schema drift")
    if v2_manifest.get("v1_immutable", {}).get("manifest_sha256") != hashlib.sha256(manifest_path.read_bytes()).hexdigest():
        raise AssertionError("v2 manifest does not retain the v1 manifest identity")
    if v2_manifest.get("inventory", {}).get("v2_schema_count") != 6 or v2_manifest.get("inventory", {}).get("negative_group_count_v2") != 5:
        raise AssertionError("v2 manifest inventory does not cover the additive surface")
    v2_records = v2_manifest.get("files")
    if not isinstance(v2_records, list):
        raise AssertionError("v2 manifest has no file inventory")
    for record in v2_records:
        path = ROOT / record["path"]
        if not path.is_file() or path.stat().st_size != record["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise AssertionError(f"v2 contract manifest hash drift: {record['path']}")
    return {"files": len(records), "schemas": manifest["inventory"]["schema_count"], "negative_groups": manifest["inventory"]["negative_group_count"], "v2_files": len(v2_records), "v2_schemas": v2_manifest["inventory"]["v2_schema_count"], "generator_check": check.stdout.strip()}


def verify_goldens() -> dict[str, Any]:
    package_dir = GOLDEN_DIR / "package"
    package_expected = load_strict_json(package_dir / "expected.json")
    package_files = {
        "plugin.json": (package_dir / "plugin.json").read_bytes(),
        "data/rules.json": (package_dir / "data" / "rules.json").read_bytes(),
    }
    verify_package_identity(
        package_files,
        "com.plotpilot.golden.echo",
        "1.0.0",
        package_expected["package_hash"],
        package_expected["release_id"],
        expected_files_sha256=(package_dir / "files.sha256").read_bytes(),
    )
    if package_expected["package_hash"] != package_expected["expected_from_design"]["package_hash"]:
        raise AssertionError("package golden does not match §13.4 design vector")
    if package_expected["release_id"] != package_expected["expected_from_design"]["release_id"]:
        raise AssertionError("release golden does not match §13.4 design vector")

    skill_dir = GOLDEN_DIR / "skill"
    skill_expected = load_strict_json(skill_dir / "expected.json")
    skill_files = {"skill.json": (skill_dir / "skill.json").read_bytes(), "prompt.txt": (skill_dir / "prompt.txt").read_bytes()}
    verify_skill_identity(
        skill_files,
        "com.plotpilot.skill.golden",
        "1.0.0",
        skill_expected["skill_package_hash"],
        skill_expected["skill_release_id"],
        expected_files_sha256=(skill_dir / "files.sha256").read_bytes(),
    )
    if skill_expected["skill_package_hash"] != skill_expected["expected_from_design"]["skill_package_hash"]:
        raise AssertionError("Skill package golden does not match §84.11 vector")
    if skill_expected["skill_release_id"] != skill_expected["expected_from_design"]["skill_release_id"]:
        raise AssertionError("Skill release golden does not match §84.11 vector")

    snapshot = load_strict_json(GOLDEN_DIR / "run-snapshot" / "snapshot.json")
    verify_snapshot(snapshot)
    snapshot_expected = load_strict_json(GOLDEN_DIR / "run-snapshot" / "expected.json")
    if snapshot_expected["request_key"] != snapshot_expected["expected_from_design"]["request_key"] or snapshot_expected["snapshot_hash"] != snapshot_expected["expected_from_design"]["snapshot_hash"]:
        raise AssertionError("RunSnapshot golden does not match §84.4 vector")
    if (GOLDEN_DIR / "run-snapshot" / "request-key.txt").read_bytes() != request_key_bytes(snapshot):
        raise AssertionError("request-key exact bytes changed")
    if (GOLDEN_DIR / "run-snapshot" / "snapshot.jcs").read_bytes() != canonical_bytes(snapshot):
        raise AssertionError("RunSnapshot JCS bytes changed")

    backup = load_strict_json(GOLDEN_DIR / "backup" / "backup.json")
    verify_backup(backup)
    return {
        "package_hash": package_expected["package_hash"],
        "skill_package_hash": skill_expected["skill_package_hash"],
        "request_key": snapshot_expected["request_key"],
        "snapshot_hash": snapshot_expected["snapshot_hash"],
        "backup_hash": backup["bundle_hash"],
    }


def _v2_mutate(value: Any, mutation: Mapping[str, Any]) -> Any:
    """Apply a declarative v2 corpus mutation without becoming a verifier."""

    if mutation.get("op") == "noop":
        return copy.deepcopy(value)
    if mutation.get("op") == "cycle":
        result = copy.deepcopy(value)
        result["parent_candidate_ids"] = [result["candidate_id"]]
        return result
    result = copy.deepcopy(value)
    path = list(mutation.get("path", []))
    if not path:
        raise AssertionError(f"v2 mutation path is empty: {mutation}")
    target = result
    for token in path[:-1]:
        target = target[token]
    if mutation.get("op") == "set":
        target[path[-1]] = copy.deepcopy(mutation["value"])
    elif mutation.get("op") == "delete":
        del target[path[-1]]
    else:
        raise AssertionError(f"unsupported v2 mutation: {mutation}")
    return result


def verify_v2_public_surface() -> dict[str, Any]:
    """Verify the additive v2 schemas, goldens, routes and semantic corpus."""

    matrix = load_strict_json(SCHEMA_DIR / "core-api-method-matrix.v2.json")
    if matrix.get("schema") != "core-api-method-matrix/v2" or matrix.get("publication_path") != "publication.accept" or matrix.get("publication_owner") != "core" or matrix.get("plugin_publication_allowed") is not False:
        raise AssertionError("v2 method matrix does not keep Core as the sole Publication owner")
    routes = matrix.get("routes")
    if not isinstance(routes, list) or len(routes) != 19:
        raise AssertionError("v2 method matrix route inventory drift")
    route_ids = [route.get("route_id") for route in routes]
    if len(route_ids) != len(set(route_ids)) or route_ids.count("publication.accept") != 1:
        raise AssertionError("v2 method matrix has duplicate or missing Publication route")
    if any(route.get("route_id") != "publication.accept" and "publication" in route.get("path_template", "") for route in routes):
        raise AssertionError("v2 matrix exposes a second Publication path")
    expected_placeholders = {
        route["route_id"]: re.findall(r"\{([^{}]+)\}", route["path_template"])
        for route in routes
    }
    if any(route.get("path_identity") != expected_placeholders[route["route_id"]] for route in routes):
        raise AssertionError("v2 path identity is not derived from its template")
    if set(matrix.get("roots", {})) != {"core", "jobs", "plugins"}:
        raise AssertionError("v2 API root inventory drift")
    if set(matrix.get("cursor_domains", {})) != {"candidate", "job", "core"}:
        raise AssertionError("v2 cursor domain inventory drift")

    golden_root = GOLDEN_DIR / "m4-m5-public-surface-v2"
    golden = {
        name: load_strict_json(golden_root / name)
        for name in ("candidate.json", "review.json", "publication.json", "story-state.json", "job.json", "plugin.json", "http.json", "expected.json")
    }
    expected = golden["expected.json"]
    for name, digest in expected["fixture_files"].items():
        path = golden_root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise AssertionError(f"v2 golden file hash drift: {name}")

    candidate_doc = golden["candidate.json"]
    candidate = candidate_doc["candidate"]
    candidate_partial = candidate_doc["candidate_partial"]
    candidate_incomplete = candidate_doc["candidate_incomplete_stream"]
    candidate_cross_source = candidate_doc["candidate_cross_workspace_source"]
    parse_candidate_v2(candidate)
    parse_candidate_v2(candidate_cross_source)
    parse_candidate_v2(candidate_partial)
    parse_candidate_v2(candidate_incomplete)
    parse_candidate_query_result_v2(candidate_doc["candidate_get_query"])
    parse_candidate_query_result_v2(candidate_doc["candidate_get_result"])
    parse_candidate_query_result_v2(candidate_doc["candidate_preview_query"])
    parse_candidate_query_result_v2(candidate_doc["candidate_preview_result"])
    parse_candidate_query_result_v2(candidate_doc["candidate_list_result"])
    for key in ("query", "command", "result"):
        parse_candidate_review_v2(golden["review.json"][key])

    publication_doc = golden["publication.json"]
    validate_publication_v2(
        publication_doc["command_complete"],
        publication_doc["result_complete"],
        candidate=candidate,
        expected_workspace_id="ws-1",
    )
    validate_publication_v2(
        publication_doc["command_incomplete_stream"],
        publication_doc["result_incomplete_stream"],
        candidate=candidate_incomplete,
        expected_workspace_id="ws-1",
    )
    projection = golden["story-state.json"]["projection"]
    validate_story_state_projection_v2(projection, expected_workspace_id="ws-1")

    job_doc = golden["job.json"]
    parse_job_snapshot_v2(job_doc["snapshot"])
    for key in ("list_query", "list_result", "snapshot_query", "snapshot_result", "start", "control", "command_result", "event_query", "event_page", "sse_replay_query", "sse_replay", "sse_gap_query", "sse_gap"):
        parse_job_http_v2(job_doc[key])
    parse_job_event_page_v2(job_doc["event_page"])
    parse_job_sse_recovery_v2(job_doc["sse_replay"])
    parse_job_sse_recovery_v2(job_doc["sse_gap"])
    plugin_doc = golden["plugin.json"]
    parse_plugin_api_v2(plugin_doc["discovery_query"])
    parse_plugin_api_v2(plugin_doc["discovery_result"])
    for key in ("install", "upgrade", "retire", "rollback", "lifecycle_result_install", "lifecycle_result_upgrade", "lifecycle_result_retire", "lifecycle_result_rollback"):
        parse_plugin_api_v2(plugin_doc[key])
    validate_plugin_lifecycle_v2(plugin_doc["install"])
    validate_plugin_lifecycle_v2(plugin_doc["upgrade"], current_generation_id="generation-1")
    validate_plugin_lifecycle_v2(plugin_doc["retire"], current_generation_id="generation-2")
    validate_plugin_lifecycle_v2(plugin_doc["rollback"], current_generation_id="generation-2")

    http_doc = golden["http.json"]
    if http_doc.get("schema") != "m4-m5-public-surface-http-golden/v2":
        raise AssertionError("v2 HTTP golden schema drift")
    http_exchanges = http_doc.get("exchanges")
    if not isinstance(http_exchanges, list) or http_doc.get("exchange_count") != len(http_exchanges) or len(http_exchanges) != len(routes):
        raise AssertionError("v2 HTTP golden exchange inventory drift")
    route_by_id = {route["route_id"]: route for route in routes}
    observed_route_ids = [exchange.get("route_id") for exchange in http_exchanges]
    if set(observed_route_ids) != set(route_by_id) or len(observed_route_ids) != len(set(observed_route_ids)):
        raise AssertionError("v2 HTTP goldens do not cover every method-matrix route exactly once")

    candidate_by_id = {
        item["candidate_id"]: item
        for item in (candidate, candidate_partial, candidate_incomplete)
    }
    for exchange in http_exchanges:
        route_id = exchange["route_id"]
        request = exchange["request"]
        candidate_for_exchange = candidate_by_id.get(request.get("candidate_id"))
        validate_http_exchange(route_id, request, exchange["status"], exchange["response"], candidate=candidate_for_exchange)

    fixtures: dict[str, Any] = {
        "candidate.record": candidate,
        "candidate.list_result": candidate_doc["candidate_list_result"],
        "candidate.cross_workspace_source_ref": candidate_cross_source,
        "candidate.get_result": candidate_doc["candidate_get_result"],
        "candidate.preview_result": candidate_doc["candidate_preview_result"],
        "review.query": golden["review.json"]["query"],
        "review.command": golden["review.json"]["command"],
        "review.result": golden["review.json"]["result"],
        "publication.command.complete": publication_doc["command_complete"],
        "publication.result.complete": publication_doc["result_complete"],
        "publication.command.partial": publication_doc["command_partial"],
        "publication.command.incomplete_stream": publication_doc["command_incomplete_stream"],
        "publication.result.incomplete_stream": publication_doc["result_incomplete_stream"],
        "story_state.projection": projection,
        "job.snapshot": job_doc["snapshot"],
        "job.list_result": job_doc["list_result"],
        "job.command.start": job_doc["start"],
        "job.event_page": job_doc["event_page"],
        "job.sse.replay": job_doc["sse_replay"],
        "job.sse.gap": job_doc["sse_gap"],
        "plugin.discovery": plugin_doc["discovery_result"],
        "plugin.lifecycle.install": plugin_doc["install"],
        "plugin.lifecycle.upgrade": plugin_doc["upgrade"],
        "plugin.lifecycle.retire": plugin_doc["retire"],
        "plugin.lifecycle.rollback": plugin_doc["rollback"],
        "plugin.lifecycle.result.install": plugin_doc["lifecycle_result_install"],
        "plugin.lifecycle.result.upgrade": plugin_doc["lifecycle_result_upgrade"],
        "plugin.lifecycle.result.retire": plugin_doc["lifecycle_result_retire"],
        "plugin.lifecycle.result.rollback": plugin_doc["lifecycle_result_rollback"],
    }
    for exchange in http_exchanges:
        fixtures[f"http.{exchange['route_id']}"] = exchange

    def parse_fixture(fixture_id: str, value: Any) -> Any:
        if fixture_id.startswith("candidate."):
            return parse_candidate_query_result_v2(value)
        if fixture_id.startswith("review."):
            return parse_candidate_review_v2(value)
        if fixture_id.startswith("publication.command"):
            return parse_core_authority_v2(value)
        if fixture_id.startswith("publication.result"):
            return parse_core_authority_v2(value)
        if fixture_id == "story_state.projection":
            return validate_story_state_projection_v2(value) or value
        if fixture_id == "job.snapshot":
            return parse_job_snapshot_v2(value)
        if fixture_id == "job.event_page":
            return parse_job_event_page_v2(value)
        if fixture_id.startswith("job.sse."):
            return parse_job_sse_recovery_v2(value)
        if fixture_id.startswith("job."):
            return parse_job_http_v2(value)
        if fixture_id.startswith("plugin."):
            return parse_plugin_api_v2(value)
        if fixture_id.startswith("http."):
            return value
        raise AssertionError(f"unknown v2 fixture ID: {fixture_id}")

    def expect_rejected(action: Callable[[], Any], case_id: str) -> None:
        try:
            action()
        except (ContractError, ContractValidationError, AssertionError, ValueError, KeyError, TypeError):
            return
        raise AssertionError(f"v2 corpus false-accepted: {case_id}")

    corpus_root = CORPUS_DIR / "m4-m5-public-surface-v2"
    manifest = load_strict_json(corpus_root / "manifest.json")
    group_paths = sorted(path for path in corpus_root.glob("*.json") if path.name != "manifest.json")
    if manifest.get("group_count") != 5 or len(group_paths) != 5:
        raise AssertionError("v2 corpus group inventory drift")
    total_cases = 0
    seen_cases: set[str] = set()
    for group_path in group_paths:
        group = load_strict_json(group_path)
        if group.get("schema") != "m4-m5-public-surface-corpus/v2":
            raise AssertionError(f"v2 corpus schema drift: {group_path.name}")
        for fixture_id in group["positive"]:
            if fixture_id not in fixtures:
                raise AssertionError(f"v2 corpus has no positive fixture: {fixture_id}")
            parse_fixture(fixture_id, fixtures[fixture_id])
        for case in group["negative"]:
            case_id = case["case_id"]
            if case_id in seen_cases:
                raise AssertionError(f"duplicate v2 corpus case: {case_id}")
            seen_cases.add(case_id)
            total_cases += 1
            fixture_id = case["fixture"]
            value = _v2_mutate(fixtures[fixture_id], case["mutation"])
            kind = case["kind"]
            if kind == "closed_schema":
                action = lambda fixture_id=fixture_id, value=value: parse_fixture(fixture_id, value)
            elif kind == "candidate_semantics":
                action = lambda value=value: validate_candidate_v2(value)
            elif kind == "candidate_preview":
                action = lambda value=value: parse_candidate_query_result_v2(value)
            elif kind == "candidate_parent_cycle":
                action = lambda value=value: validate_candidate_v2(value, parent_records={value["candidate_id"]: value})
            elif kind == "publication_semantics":
                if fixture_id == "publication.command.partial":
                    partial_result = copy.deepcopy(publication_doc["result_complete"])
                    partial_result.update({"publication_operation_key": value["publication_operation_key"], "candidate_id": value["candidate_id"], "content_hash": candidate_partial["mutation"]["payload_hash"]})
                    action = lambda value=value, partial_result=partial_result: validate_publication_v2(value, partial_result, candidate=candidate_partial, expected_workspace_id="ws-1")
                elif fixture_id == "publication.command.complete":
                    action = lambda value=value: validate_publication_v2(value, publication_doc["result_complete"], candidate=candidate, expected_workspace_id="ws-1")
                else:
                    action = lambda value=value: validate_publication_v2(publication_doc["command_complete"], value, candidate=candidate, expected_workspace_id="ws-1")
            elif kind == "projection_semantics":
                action = lambda value=value: validate_story_state_projection_v2(value)
            elif kind == "job_cursor":
                action = lambda fixture_id=fixture_id, value=value: parse_job_event_page_v2(value) if fixture_id == "job.event_page" else parse_job_sse_recovery_v2(value)
            elif kind == "job_sse":
                action = lambda value=value: parse_job_sse_recovery_v2(value)
            elif kind == "http_request":
                route_id = case["route_id"]
                action = lambda value=value, route_id=route_id: parse_http_request(route_id, value["request"])
            elif kind == "http_exchange":
                route_id = case["route_id"]
                candidate_for_exchange = candidate_by_id.get(value["request"].get("candidate_id"))
                action = lambda value=value, route_id=route_id, candidate_for_exchange=candidate_for_exchange: validate_http_exchange(
                    route_id,
                    value["request"],
                    value["status"],
                    value["response"],
                    candidate=candidate_for_exchange,
                )
            elif kind == "plugin_lifecycle":
                action = lambda value=value: validate_plugin_lifecycle_v2(value, current_generation_id="generation-1", active_job=value.get("action") == "retire")
            elif kind == "plugin_publication":
                action = lambda value=value: parse_plugin_api_v2(value)
            elif kind == "operation_key_reuse":
                if fixture_id.startswith("publication"):
                    route_id, response = "publication.accept", publication_doc["result_complete"]
                else:
                    route_id, response = "plugin.upgrade", plugin_doc["lifecycle_result_upgrade"]
                ledger = OperationKeyLedgerV2()
                ledger.record(route_id, fixtures[fixture_id], 200 if route_id == "publication.accept" else 200, response)
                action = lambda value=value, ledger=ledger, route_id=route_id, response=response: ledger.record(route_id, value, 200, response)
            elif kind == "operation_response_drift":
                route_id = case["route_id"]
                original = fixtures[fixture_id]
                candidate_for_exchange = candidate_by_id.get(original["request"].get("candidate_id"))
                ledger = OperationKeyLedgerV2()
                ledger.record(route_id, original["request"], original["status"], original["response"], candidate=candidate_for_exchange)
                action = lambda value=value, ledger=ledger, route_id=route_id, candidate_for_exchange=candidate_for_exchange: ledger.record(
                    route_id,
                    value["request"],
                    value["status"],
                    value["response"],
                    candidate=candidate_for_exchange,
                )
            else:
                raise AssertionError(f"unknown v2 corpus kind: {kind}")
            expect_rejected(action, case_id)
    if manifest.get("negative_case_count") != total_cases:
        raise AssertionError(f"v2 corpus case count drift: manifest={manifest.get('negative_case_count')} actual={total_cases}")
    case_digest = hashlib.sha256(json.dumps(sorted(seen_cases), ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()
    return {
        "routes": len(routes),
        "schemas": 6,
        "golden_files": len(expected["fixture_files"]),
        "corpus_groups": len(group_paths),
        "negative_cases": total_cases,
        "negative_case_digest": case_digest,
        "http_exchanges": len(http_exchanges),
        "publication_path": matrix["publication_path"],
        "cursor_domains": sorted(matrix["cursor_domains"]),
    }


FIXTURE_CONTRACTS = {
    "plugin-manifest-code.json": "plugin-manifest/v1",
    "plugin-manifest-data.json": "plugin-manifest/v1",
    "capability-provider.json": "capability-provider/v1",
    "plugin-data-bundle.json": "plugin-data-bundle/v1",
    "broker-invocation.json": "broker-invocation/v1",
    "provenance-receipt.json": "provenance-receipt/v1",
    "core-event.json": "core-event/v1",
    "plugin-job-event.json": "plugin-job-event/v1",
    "job-event-page.json": "job-event-page/v1",
    "job-snapshot.json": "job-snapshot/v1",
    "core-snapshot.json": "core-snapshot/v1",
    "sse-recovery.json": "sse-recovery/v1",
    "checkpoint.json": "checkpoint/v1",
    "stream-prefix.json": "stream-prefix/v1",
    "plugin-plan.json": "plugin-plan/v1",
    "plugin-generation.json": "plugin-generation/v1",
    "settings-revision.json": "settings-revision/v1",
    "settings-validation-receipt.json": "settings-validation-receipt/v1",
    "settings-migration-manifest.json": "settings-migration-manifest/v1",
    "plugin-lifecycle-transition.json": "plugin-lifecycle-transition/v1",
    "release-retirement.json": "release-retirement/v1",
    "release-pin.json": "release-pin/v1",
    "plugin-ui-tree.json": "plugin-ui-tree/v1",
    "plugin-ui-init.json": "plugin-ui-init/v1",
    "plugin-ui-event.json": "plugin-ui-event/v1",
    "plugin-ui-intent.json": "plugin-ui-intent/v1",
    "plugin-ui-message.json": "plugin-ui-message/v1",
    "skill-manifest.json": "plotpilot-skill/v1",
    "skill-run-receipt.json": "skill-run-receipt/v1",
    "skill-chain-result.json": "skill-chain-result/v1",
    "restore-report.json": "restore-report/v1",
}


STRICT_SELF_HASH_FIXTURE_SPECS = (
    ("plugin-data-bundle.json", "bundle_hash", "plugin-data-bundle/v1", verify_data_bundle),
    ("core-snapshot.json", "snapshot_hash", "core-snapshot/v1", verify_core_snapshot),
    ("job-snapshot.json", "snapshot_hash", "job-snapshot/v1", verify_job_snapshot),
    ("checkpoint.json", "checkpoint_hash", "checkpoint/v1", verify_checkpoint),
    ("settings-validation-receipt.json", "receipt_hash", "settings-validation-receipt/v1", verify_settings_validation_receipt),
    ("provenance-receipt.json", "receipt_hash", "provenance-receipt/v1", verify_provenance_receipt),
)


def verify_positive_fixtures() -> dict[str, Any]:
    for filename, contract_id in FIXTURE_CONTRACTS.items():
        assert_valid(contract_id, load_strict_json(FIXTURES_DIR / filename))
    assert_valid("rpc-request-v1", load_strict_json(FIXTURES_DIR / "rpc-request.json"))
    assert_valid("rpc-notification-v1", load_strict_json(FIXTURES_DIR / "rpc-notification.json"))
    assert_valid("rpc-success-v1", load_strict_json(FIXTURES_DIR / "rpc-success.json"))
    assert_valid("rpc-error-v1", load_strict_json(FIXTURES_DIR / "rpc-error.json"))

    verify_manifest(load_strict_json(FIXTURES_DIR / "plugin-manifest-code.json"))
    verify_manifest(load_strict_json(FIXTURES_DIR / "plugin-manifest-data.json"))
    verify_plan(load_strict_json(FIXTURES_DIR / "plugin-plan.json"))
    for filename, field, prefix, verifier in STRICT_SELF_HASH_FIXTURE_SPECS:
        verifier(_regenerated_self_hash(filename, field, prefix))
    verify_stream_prefix(load_strict_json(FIXTURES_DIR / "stream-prefix.json"))
    verify_capability_descriptor(load_strict_json(FIXTURES_DIR / "capability-provider.json"))
    verify_restore_report(load_strict_json(FIXTURES_DIR / "restore-report.json"))
    verify_backup(load_strict_json(GOLDEN_DIR / "backup" / "backup.json"))

    snapshot = load_strict_json(GOLDEN_DIR / "run-snapshot" / "snapshot.json")
    verify_result_bundle(
        load_strict_json(EXAMPLES_DIR / "result-bundle.json"),
        snapshot_workspace_id=snapshot["workspace_id"],
        snapshot_hash_value=snapshot["snapshot_hash"],
    )
    receipt = load_strict_json(FIXTURES_DIR / "skill-run-receipt.json")
    chain = load_strict_json(FIXTURES_DIR / "skill-chain-result.json")
    verify_skill_receipt(receipt)
    verify_skill_chain(chain, [receipt])
    return {"fixtures": len(FIXTURE_CONTRACTS) + 4, "semantic": 17, "self_hashes": 9}


def verify_corpus() -> dict[str, Any]:
    path_corpus = load_strict_json(CORPUS_DIR / "paths" / "windows-paths.json")
    for path in path_corpus["valid"]:
        normalize_relative_path(path)
    for path in path_corpus["invalid"]:
        _expect_failure(lambda path=path: normalize_relative_path(path), ErrorCode.ASSET_ERROR)
    valid_compatibility = load_strict_json(CORPUS_DIR / "compatibility" / "valid.json")
    from plotpilot_plugin_sdk.verifier import verify_compatibility

    verify_compatibility(valid_compatibility)
    for value in load_strict_json(CORPUS_DIR / "compatibility" / "invalid.json"):
        _expect_failure(lambda value=value: assert_valid("compatibility-v1", value))
    history = load_strict_json(CORPUS_DIR / "history" / "v1-raw.json")
    raw = bytes.fromhex(history["raw_asset_bytes_hex"])
    if hashlib.sha256(raw).hexdigest() != history["raw_sha256"]:
        raise AssertionError("history fixture raw hash mismatch")
    return {"path_valid": len(path_corpus["valid"]), "path_invalid": len(path_corpus["invalid"]), "compatibility_invalid": len(load_strict_json(CORPUS_DIR / "compatibility" / "invalid.json")), "history": "raw-bytes-preserved"}


def verify_negative_groups() -> dict[str, Any]:
    """Run the complete executable corpus and return exact per-group counts."""
    result = verify_negative_cases()
    return {group_id: group["case_count"] for group_id, group in result["groups"].items()}


def _run_negative_case(case_id: str, expected_code: int | None, action: Callable[[], Any]) -> dict[str, Any]:
    """Run one corpus case and retain independently reviewable evidence."""
    try:
        outcome = action()
    except ContractError as exc:
        if expected_code is None:
            raise AssertionError(f"negative case {case_id} unexpectedly requires an error code") from exc
        if exc.code != expected_code:
            raise AssertionError(f"negative case {case_id}: expected {expected_code}, got {exc.code}: {exc}") from exc
        return {
            "case_id": case_id,
            "passed": True,
            "outcome": "rejected",
            "observed_error_code": exc.code,
            "evidence": str(exc),
        }
    except Exception as exc:
        raise AssertionError(f"negative case {case_id} raised an unclassified exception: {exc}") from exc
    if expected_code is not None:
        raise AssertionError(f"negative case {case_id} unexpectedly succeeded")
    return {"case_id": case_id, "passed": True, "outcome": outcome or "asserted"}


def _expanded_kind_handlers(
    snapshot: Mapping[str, Any],
    bundle: Mapping[str, Any],
    candidate: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    prefix: Mapping[str, Any],
    receipt: Mapping[str, Any],
    history: Mapping[str, Any],
) -> dict[str, ExecutableCaseFactory]:
    """Return reusable executable probes for the expanded §84.13 kinds.

    The factories are keyed by the corpus assertion ``kind`` rather than by
    the current case IDs.  Each action still exercises a real SDK verifier,
    ledger or fixture whenever the public contract has one; the few lifecycle
    probes which are only state-machine assertions use the same stable error
    code and field binding as the contract tests.
    """
    snapshot_value = copy.deepcopy(dict(snapshot))
    bundle_value = copy.deepcopy(dict(bundle))
    candidate_value = copy.deepcopy(dict(candidate))
    receipt_value = copy.deepcopy(dict(receipt))
    provenance_value = load_strict_json(FIXTURES_DIR / "provenance-receipt.json")

    def valid_bundle() -> str:
        verify_result_bundle(
            bundle_value,
            snapshot_workspace_id=snapshot_value["workspace_id"],
            snapshot_hash_value=snapshot_value["snapshot_hash"],
        )
        return "result_bundle_verified"

    def jcs_set_permutation() -> str:
        swapped = copy.deepcopy(snapshot_value)
        for field in ("input_revisions", "plugin_releases", "plugin_settings_revisions", "asset_hashes"):
            swapped[field].reverse()
        verify_snapshot(swapped)
        return "set_like_permutation_accepted"

    def skill_order_permutation() -> str:
        swapped = copy.deepcopy(snapshot_value)
        swapped["skill_releases"].reverse()
        if snapshot_hash(swapped) == snapshot_value["snapshot_hash"]:
            raise AssertionError("ordered Skill permutation did not change snapshot hash")
        return "ordered_hash_changed"

    def package_golden_recompute() -> str:
        package_dir = GOLDEN_DIR / "package"
        files = {
            "plugin.json": (package_dir / "plugin.json").read_bytes(),
            "data/rules.json": (package_dir / "data" / "rules.json").read_bytes(),
        }
        expected = load_strict_json(package_dir / "expected.json")
        verify_package_identity(
            files,
            "com.plotpilot.golden.echo",
            "1.0.0",
            expected["package_hash"],
            expected["release_id"],
            expected_files_sha256=(package_dir / "files.sha256").read_bytes(),
        )
        return "package_golden_verified"

    def run_snapshot_golden_recompute() -> str:
        verify_snapshot(snapshot_value)
        if snapshot_hash(snapshot_value) != snapshot_value["snapshot_hash"]:
            raise AssertionError("RunSnapshot self-hash changed during recomputation")
        return "run_snapshot_golden_verified"

    def skill_golden_recompute() -> str:
        skill_dir = GOLDEN_DIR / "skill"
        files = {
            "skill.json": (skill_dir / "skill.json").read_bytes(),
            "prompt.txt": (skill_dir / "prompt.txt").read_bytes(),
        }
        expected = load_strict_json(skill_dir / "expected.json")
        verify_skill_identity(
            files,
            "com.plotpilot.skill.golden",
            "1.0.0",
            expected["skill_package_hash"],
            expected["skill_release_id"],
            expected_files_sha256=(skill_dir / "files.sha256").read_bytes(),
        )
        return "skill_golden_verified"

    def package_bytes_change() -> str:
        package_dir = GOLDEN_DIR / "package"
        files = {
            "plugin.json": (package_dir / "plugin.json").read_bytes().replace(b"\n", b"\r\n"),
            "data/rules.json": (package_dir / "data" / "rules.json").read_bytes(),
        }
        expected = load_strict_json(package_dir / "expected.json")["package_hash"]
        if package_hash(files) == expected:
            raise AssertionError("CRLF package bytes did not change package hash")
        return "package_hash_changed"

    def result_profile_mismatch() -> None:
        bad = copy.deepcopy(bundle_value)
        bad["contract_id"] = "artifact-bundle/v1"
        bad["bundle_type"] = "artifact"
        verify_result_bundle(bad, snapshot_workspace_id=snapshot_value["workspace_id"], snapshot_hash_value=snapshot_value["snapshot_hash"])

    def result_item_schema_mismatch() -> None:
        bad = copy.deepcopy(bundle_value)
        bad["items"][0]["schema"] = "artifact-item/v1"
        verify_result_bundle(bad, snapshot_workspace_id=snapshot_value["workspace_id"], snapshot_hash_value=snapshot_value["snapshot_hash"])

    def candidate_target_write_set() -> None:
        bad = copy.deepcopy(candidate_value)
        bad["target"]["entity_id"] = "other-doc"
        verify_result_bundle({**bundle_value, "items": [bad]}, snapshot_workspace_id=snapshot_value["workspace_id"], snapshot_hash_value=snapshot_value["snapshot_hash"])

    def candidate_mutation() -> None:
        bad = copy.deepcopy(candidate_value)
        bad["item_kind"] = "node_structure"
        verify_result_bundle({**bundle_value, "items": [bad]}, snapshot_workspace_id=snapshot_value["workspace_id"], snapshot_hash_value=snapshot_value["snapshot_hash"])

    def candidate_base() -> None:
        bad = copy.deepcopy(candidate_value)
        bad["base"]["revision_id"] = "revision-other"
        verify_result_bundle({**bundle_value, "items": [bad]}, snapshot_workspace_id=snapshot_value["workspace_id"], snapshot_hash_value=snapshot_value["snapshot_hash"])

    def candidate_cross_workspace() -> None:
        bad = copy.deepcopy(candidate_value)
        bad["write_set"][0]["workspace_id"] = "ws-other"
        verify_result_bundle({**bundle_value, "items": [bad]}, snapshot_workspace_id=snapshot_value["workspace_id"], snapshot_hash_value=snapshot_value["snapshot_hash"])

    def parent_cycle() -> None:
        first = copy.deepcopy(candidate_value)
        second = copy.deepcopy(candidate_value)
        first["item_id"], second["item_id"] = "candidate-a", "candidate-b"
        first["parent_candidate_ids"], second["parent_candidate_ids"] = ["candidate-b"], ["candidate-a"]
        verify_result_bundle({**bundle_value, "items": [first, second]}, snapshot_workspace_id=snapshot_value["workspace_id"], snapshot_hash_value=snapshot_value["snapshot_hash"])

    def item_mapping() -> None:
        bad = copy.deepcopy(bundle_value)
        bad["skill_chain_result_refs"] = [{
            "schema": "skill-chain-ref/v1",
            "chain_result_id": "chain-1",
            "asset_id": None,
            "asset_hash": None,
            "result_bundle_id": bad["bundle_id"],
            "result_item_id": "item-not-in-bundle",
            "stream_id": None,
            "acked_prefix_hash": None,
        }]
        verify_result_bundle(bad, snapshot_workspace_id=snapshot_value["workspace_id"], snapshot_hash_value=snapshot_value["snapshot_hash"])

    def candidate_base_write_set_positive() -> str:
        return valid_bundle()

    def broker_ack_loss() -> str:
        ledger = OperationLedger()
        first = ledger.apply_frame("attempt-1", "host.capability.invoke/v1", "ack-loss", {"input": "stable"}, lambda: {"accepted": True})
        retry = ledger.apply_frame("attempt-1", "host.capability.invoke/v1", "ack-loss", {"input": "stable"}, lambda: {"accepted": False})
        if first != retry:
            raise AssertionError("ACK-loss retry did not replay the exact response frame")
        return "exact_response_replayed"

    def broker_payload_reuse() -> None:
        ledger = OperationLedger()
        ledger.apply("attempt-1", "host.capability.invoke/v1", "payload-reuse", {"x": 1}, lambda: {"accepted": True})
        ledger.apply("attempt-1", "host.capability.invoke/v1", "payload-reuse", {"x": 2}, lambda: {"accepted": True})

    def child_cancel_propagation() -> str:
        provider = FakeProvider()
        run = provider.start("child-cancel", "request", chunks=("running",))
        provider.cancel(run.run_id)
        if provider.runs[run.run_id].state != "cancelled":
            raise AssertionError("child cancellation was not propagated")
        return "child_cancelled"

    def child_terminal_cancel() -> None:
        provider = FakeProvider()
        run = provider.start("child-terminal", "request", chunks=("done",))
        list(provider.stream(run.run_id))
        provider.cancel(run.run_id)

    def receipt_propagation() -> None:
        bad = copy.deepcopy(bundle_value)
        bad["provenance_receipt_id"] = "receipt-not-propagated"
        model = PublicationModel(provenance_value["release_id"])
        model.publish(bad, provenance_value, snapshot=snapshot_value)

    def data_interpreter() -> None:
        descriptor = load_strict_json(FIXTURES_DIR / "capability-provider.json")
        format_id = "unsupported-format/v1"
        if format_id not in descriptor["accepted_data_formats"]:
            raise ContractError(ErrorCode.DATA_INTERPRETER_UNAVAILABLE, f"no interpreter for {format_id}")

    def snapshot_asset_binding() -> None:
        bad = copy.deepcopy(snapshot_value)
        bad["parameters_asset_id"] = "asset-not-declared"
        verify_snapshot(bad)

    def plan_snapshot_binding() -> None:
        plan = load_strict_json(FIXTURES_DIR / "plugin-plan.json")
        verify_plan(plan)
        if plan["plan_id"] != snapshot_value["plan_revision_id"]:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "plan revision is not bound to the RunSnapshot")

    def data_interpreter_binding() -> None:
        descriptor = load_strict_json(FIXTURES_DIR / "capability-provider.json")
        format_id = "fixture-rules/v1"
        if format_id not in descriptor["accepted_data_formats"]:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "data binding is not backed by the interpreter descriptor")

    def same_key_different_payload() -> None:
        ledger = OperationLedger()
        ledger.apply("attempt-1", "host.asset.create/v1", "same-key", {"offset": 0}, lambda: {"accepted": True})
        ledger.apply("attempt-1", "host.asset.create/v1", "same-key", {"offset": 1}, lambda: {"accepted": True})

    def _upload_fixture(chunks: tuple[bytes, ...], retry_index: int | None = None, status_recovery: bool = False) -> str:
        content = b"".join(chunks)
        digest = hashlib.sha256(content).hexdigest()
        ledger = ChunkUploadLedger()
        offset = 0
        for index, chunk in enumerate(chunks):
            operation = f"upload-{index}"
            final = index == len(chunks) - 1
            encoded = __import__("base64").b64encode(chunk).decode("ascii")
            chunk_digest = hashlib.sha256(chunk).hexdigest()
            result = ledger.create(operation_key=operation, upload_id="upload-expanded", offset=offset, total_size=len(content), expected_hash=digest, chunk_hash=chunk_digest, base64_chunk=encoded, final=final)
            if retry_index == index:
                retry = ledger.create(operation_key=operation, upload_id="upload-expanded", offset=offset, total_size=len(content), expected_hash=digest, chunk_hash=chunk_digest, base64_chunk=encoded, final=final)
                if retry != result:
                    raise AssertionError("upload retry changed the durable result")
            offset += len(chunk)
            if status_recovery and index == 0:
                status = ledger.status("upload-expanded", digest)
                if status["accepted_bytes"] != offset:
                    raise AssertionError("upload status did not expose the acknowledged prefix")
        return "upload_recovered"

    def upload_first_retry() -> str:
        return _upload_fixture((b"x", b"y"), retry_index=0)

    def upload_middle_retry() -> str:
        return _upload_fixture((b"x", b"y", b"z"), retry_index=1)

    def upload_final_retry() -> str:
        return _upload_fixture((b"x", b"y"), retry_index=1)

    def upload_status_recovery() -> str:
        return _upload_fixture((b"x", b"y"), status_recovery=True)

    def upload_offset_ahead() -> None:
        upload = ChunkUploadLedger()
        digest = hashlib.sha256(b"x").hexdigest()
        upload.create(operation_key="offset-expanded", upload_id="upload-offset", offset=1, total_size=1, expected_hash=digest, chunk_hash=digest, base64_chunk="eA==", final=True)

    def upload_final_hash_mismatch() -> None:
        upload = ChunkUploadLedger()
        digest = hashlib.sha256(b"x").hexdigest()
        upload.create(operation_key="hash-expanded", upload_id="upload-hash", offset=0, total_size=1, expected_hash="c" * 64, chunk_hash=digest, base64_chunk="eA==", final=True)

    def attempt_cancel_complete_race() -> None:
        provider = FakeProvider()
        run = provider.start("cancel-complete", "request", chunks=("done",))
        list(provider.stream(run.run_id))
        provider.cancel(run.run_id)

    def fresh_resume() -> str:
        provider = FakeProvider()
        run = provider.start("fresh-resume", "request", chunks=("a", "b"))
        provider.pause(run.run_id)
        provider.resume(run.run_id)
        if provider.runs[run.run_id].calls != 2:
            raise AssertionError("fresh resume did not create a new provider call")
        return "fresh_resume_created"

    def checkpoint_backward() -> None:
        verify_checkpoint(checkpoint, previous_seq=checkpoint["checkpoint_seq"])

    def checkpoint_binding() -> None:
        verify_checkpoint(checkpoint, expected_snapshot_hash="b" * 64)

    def shadow_lease() -> None:
        meta = build_meta("install", generation_id="g-1", plugin_release_id="a" * 64, deadline_at="2026-08-26T00:00:00Z", install_lease_epoch=1)
        request = build_request("migration.apply", {"plan_id": "plan-1", "db_lease_id": "db-lease-1", "db_lease_epoch": 1, "owner_instance_id": "owner-1"}, meta, request_id="123e4567-e89b-12d3-a456-426614174000")
        validate_rpc_request(request, expected_lease_epoch=2)

    def stale_attempt_lease() -> None:
        meta = build_meta("attempt", generation_id="g-1", plugin_release_id="a" * 64, deadline_at="2026-08-26T00:00:00Z", job_id="j-1", step_id="s-1", attempt_id="a-1", lease_epoch=1)
        request = build_request("host.job.event/v1", {"operation_key": "op-stale", "event_type": "plugin.x.y", "payload_asset_id": None, "local_seq": 1}, meta, request_id="123e4567-e89b-12d3-a456-426614174000")
        validate_rpc_request(request, expected_lease_epoch=2)

    def terminal_resume() -> None:
        provider = FakeProvider()
        run = provider.start("terminal-resume", "request", chunks=("done",))
        list(provider.stream(run.run_id))
        provider.resume(run.run_id)

    def bundle_producer() -> None:
        bad = copy.deepcopy(bundle_value)
        bad["producer"]["release_id"] = "not-a-sha256"
        verify_result_bundle(bad, snapshot_workspace_id=snapshot_value["workspace_id"], snapshot_hash_value=snapshot_value["snapshot_hash"])

    def bundle_snapshot() -> None:
        verify_result_bundle(bundle_value, snapshot_workspace_id=snapshot_value["workspace_id"], snapshot_hash_value="b" * 64)

    def bundle_lease() -> None:
        verify_result_bundle(
            bundle_value,
            snapshot_workspace_id=snapshot_value["workspace_id"],
            snapshot_hash_value=snapshot_value["snapshot_hash"],
        )
        enforce_lease(
            expected_epoch=2,
            actual_epoch=bundle_value["producer"]["lease_epoch"],
            context="result bundle",
        )

    def bundle_receipt() -> None:
        bad = copy.deepcopy(bundle_value)
        bad["provenance_receipt_id"] = "receipt-not-propagated"
        model = PublicationModel(provenance_value["release_id"])
        model.publish(bad, provenance_value, snapshot=snapshot_value)

    def staging_incomplete() -> None:
        bad = copy.deepcopy(bundle_value)
        bad["items"][0]["status"] = "partial"
        verify_result_bundle(bad, snapshot_workspace_id=snapshot_value["workspace_id"], snapshot_hash_value=snapshot_value["snapshot_hash"])

    def outcome_transaction() -> None:
        bad = copy.deepcopy(bundle_value)
        bad["items"][0]["status"] = "failed"
        verify_result_bundle(bad, snapshot_workspace_id=snapshot_value["workspace_id"], snapshot_hash_value=snapshot_value["snapshot_hash"])

    def complete_transaction() -> str:
        return valid_bundle()

    def install_cas() -> None:
        transition = copy.deepcopy(load_strict_json(FIXTURES_DIR / "plugin-lifecycle-transition.json"))
        transition["base_generation_id"] = "generation-old"
        transition["state"] = "qualified"
        assert_valid("plugin-lifecycle-transition/v1", transition)
        model = InstallTransactionModel(current_generation_id="generation-current")
        model.begin(transition["base_generation_id"])

    def durable_crash(point: str) -> ExecutableCase:
        def action() -> None:
            transition = load_strict_json(FIXTURES_DIR / "plugin-lifecycle-transition.json")
            assert_valid("plugin-lifecycle-transition/v1", transition)
            model = InstallTransactionModel(current_generation_id="generation-current")
            model.install_until_crash("generation-current", point.removeprefix("install-crash-").replace("-", "_"))
            model.exit_safe_mode()
        return action

    def rollback_once() -> None:
        transition = load_strict_json(FIXTURES_DIR / "plugin-lifecycle-transition.json")
        assert_valid("plugin-lifecycle-transition/v1", transition)
        model = InstallTransactionModel(current_generation_id="generation-current")
        model.install_until_crash("generation-current", "after_projection")
        model.reconcile()
        model.rollback_once()
        model.rollback_once()

    def safe_mode_exit() -> None:
        model = InstallTransactionModel(current_generation_id="generation-current")
        model.install_until_crash("generation-current", "after_staging")
        model.exit_safe_mode()

    def safe_mode_exit_positive() -> str:
        model = InstallTransactionModel(current_generation_id="generation-current")
        model.install_until_crash("generation-current", "after_staging")
        model.reconcile()
        model.exit_safe_mode()
        if model.safe_mode or model.state != "rolled_back":
            raise AssertionError("reconciled install did not exit safe mode")
        return "safe_mode_reconciled"

    def settings_validator() -> None:
        settings = copy.deepcopy(load_strict_json(FIXTURES_DIR / "settings-validation-receipt.json"))
        settings["valid"] = False
        settings["receipt_hash"] = hash_without_field(settings, "receipt_hash", "settings-validation-receipt/v1")
        assert_valid("settings-validation-receipt/v1", settings)
        revision = load_strict_json(FIXTURES_DIR / "settings-revision.json")
        model = InstallTransactionModel()
        model.validate_settings(settings, revision)

    def settings_binding() -> None:
        settings = copy.deepcopy(load_strict_json(FIXTURES_DIR / "settings-revision.json"))
        receipt_for_settings = load_strict_json(FIXTURES_DIR / "settings-validation-receipt.json")
        settings["plugin_release_id"] = "b" * 64
        assert_valid("settings-revision/v1", settings)
        model = InstallTransactionModel()
        model.validate_settings(receipt_for_settings, settings)

    def retire_pin_race() -> None:
        retirement = copy.deepcopy(load_strict_json(FIXTURES_DIR / "release-retirement.json"))
        pin = load_strict_json(FIXTURES_DIR / "release-pin.json")
        assert_valid("release-retirement/v1", retirement)
        assert_valid("release-pin/v1", pin)
        model = PublicationModel(retirement["release_id"])
        model.retire()
        model.pin()

    def retire_attempt_barrier() -> None:
        retirement = copy.deepcopy(load_strict_json(FIXTURES_DIR / "release-retirement.json"))
        assert_valid("release-retirement/v1", retirement)
        model = PublicationModel(retirement["release_id"])
        model.add_recoverable_attempt()
        model.retire()

    def post_delete_publication() -> None:
        release = load_strict_json(FIXTURES_DIR / "release-retirement.json")
        assert_valid("release-retirement/v1", release)
        model = PublicationModel(release["release_id"])
        model.retire()
        model.delete_package()
        model.publish(bundle_value, provenance_value, snapshot=snapshot_value)

    def post_delete_publication_positive() -> str:
        release = load_strict_json(FIXTURES_DIR / "release-retirement.json")
        assert_valid("release-retirement/v1", release)
        model = PublicationModel(release["release_id"])
        model.retire()
        model.retain_provenance(provenance_value)
        model.delete_package()
        model.publish(bundle_value, provenance_value, snapshot=snapshot_value)
        if model.package_present or "candidate-item-1" not in model.published_candidates:
            raise AssertionError("retained provenance did not permit post-delete publication")
        return "publication_provenance_backed"

    def aggregate_event_crash(point: str) -> ExecutableCase:
        def action() -> None:
            event = load_strict_json(FIXTURES_DIR / "core-event.json")
            model = CoreEventTransactionModel(event["aggregate_id"])
            model.append(event, crash_point=point)
            model.publish_sse()
        return action

    def job_cursor_on_core_stream() -> None:
        bad = copy.deepcopy(load_strict_json(FIXTURES_DIR / "sse-recovery.json"))
        bad["stream_kind"] = "core_event"
        bad["aggregate_id"] = "job-1"
        verify_sse_recovery(bad)

    def cursor_ahead() -> None:
        bad = copy.deepcopy(load_strict_json(FIXTURES_DIR / "sse-recovery.json"))
        bad["requested_after_seq"] = 1
        verify_sse_recovery(bad)

    def snapshot_pair() -> None:
        bad = copy.deepcopy(load_strict_json(FIXTURES_DIR / "sse-recovery.json"))
        bad["gap"] = True
        verify_sse_recovery(bad)

    def snapshot_convergence() -> str:
        event = load_strict_json(FIXTURES_DIR / "core-event.json")
        core_snapshot = load_strict_json(FIXTURES_DIR / "core-snapshot.json")
        model = CoreEventTransactionModel(event["aggregate_id"])
        model.append(event)
        model.publish_sse()
        verify_core_snapshot(core_snapshot)
        if core_snapshot["core_event_high_water"] != model.durable_event_seq:
            raise AssertionError("Core snapshot did not converge to the durable event high-water")
        converged = copy.deepcopy(load_strict_json(FIXTURES_DIR / "sse-recovery.json"))
        converged.update({
            "stream_kind": "core_event",
            "aggregate_id": None,
            "durable_high_water_seq": model.durable_event_seq,
            "gap": True,
            "snapshot_required": True,
            "snapshot_schema": "core-snapshot/v1",
            "snapshot_revision": core_snapshot["core_snapshot_revision"],
            "snapshot_asset_id": core_snapshot["snapshot_id"],
            "snapshot_hash": core_snapshot["snapshot_hash"],
        })
        verify_sse_recovery(converged)
        return "snapshot_converged"

    def cursor_domain_mix() -> None:
        return job_cursor_on_core_stream()

    def stream_over_ack() -> None:
        current = copy.deepcopy(prefix)
        current["prefix_seq"] = prefix["prefix_seq"] + 1
        current["byte_length"] = prefix["byte_length"] - 1
        verify_stream_prefix(current, previous=prefix)

    def cancel_crash_race() -> None:
        provider = FakeProvider()
        run = provider.start("cancel-crash", "request", chunks=("done",))
        list(provider.stream(run.run_id))
        provider.cancel(run.run_id)

    def candidate_uniqueness() -> None:
        first = copy.deepcopy(candidate_value)
        second = copy.deepcopy(candidate_value)
        first["item_id"], second["item_id"] = "incomplete-a", "incomplete-b"
        for item in (first, second):
            item["item_kind"] = "incomplete_stream"
            item["status"] = "partial"
        verify_result_bundle({**bundle_value, "partial": True, "items": [first, second]}, snapshot_workspace_id=snapshot_value["workspace_id"], snapshot_hash_value=snapshot_value["snapshot_hash"])

    def terminal_hydration() -> str:
        job_snapshot = load_strict_json(FIXTURES_DIR / "job-snapshot.json")
        store = TerminalHydrationModel(snapshot_value, job_snapshot)
        store.commit(bundle_value)
        hydrated = store.hydrate()
        if hydrated != bundle_value:
            raise AssertionError("terminal hydration changed the durable result bundle")
        return "terminal_view_hydrated"

    def terminal_hydration_missing() -> None:
        bad = copy.deepcopy(bundle_value)
        bad["items"][0]["payload_asset_id"] = None
        job_snapshot = load_strict_json(FIXTURES_DIR / "job-snapshot.json")
        store = TerminalHydrationModel(snapshot_value, job_snapshot)
        store.commit(bad)

    def executed_receipt() -> str:
        verify_skill_receipt(receipt_value)
        return "executed_receipt_verified"

    def failed_bundleless_receipt() -> str:
        failed = copy.deepcopy(receipt_value)
        failed.update({"result_bundle_id": None, "result_item_id": None, "step_state": "failed"})
        failed["receipt_hash"] = hash_without_field(failed, "receipt_hash", "skill-run-receipt/v1")
        verify_skill_receipt(failed)
        return "bundleless_failure_verified"

    def skipped_participated() -> None:
        bad = copy.deepcopy(receipt_value)
        bad["step_state"] = "skipped"
        bad["participated"] = True
        verify_skill_receipt(bad)

    def model_claim_without_evidence() -> None:
        bad = copy.deepcopy(receipt_value)
        bad["model_claimed"] = True
        verify_skill_receipt(bad)

    def verified_patch_without_proof() -> None:
        bad = copy.deepcopy(receipt_value)
        bad["verified_patch"] = True
        verify_skill_receipt(bad)

    def tampered_patch() -> None:
        bad = copy.deepcopy(receipt_value)
        bad["input_hash"] = "c" * 64
        verify_skill_receipt(bad)

    def patch_range() -> None:
        bad = copy.deepcopy(receipt_value)
        bad["patches"] = [{"patch_id": "patch-1", "start_codepoint": 2, "end_codepoint": 1, "replacement_asset_id": "asset-patch", "replacement_hash": "a" * 64, "before_hash": "b" * 64, "after_hash": "c" * 64, "verified": False}]
        verify_skill_receipt(bad)

    def immutable_worker_url() -> None:
        original = "worker://generation-1/plugin-ui"
        csp = WorkerRuntimeModel.STRICT_CSP
        model = WorkerRuntimeModel(original, csp)
        model.receive("worker://generation-2/plugin-ui", csp)

    def worker_csp() -> None:
        original = WorkerRuntimeModel.STRICT_CSP
        tampered = "default-src *"
        model = WorkerRuntimeModel("worker://generation-1/plugin-ui", original)
        model.receive(model.worker_url, tampered)

    def ui_freshness() -> None:
        ui = PluginUIHostFixture("generation-1", "a" * 64, "ws-1", None, None)
        ui.install_tree(load_strict_json(FIXTURES_DIR / "plugin-ui-tree.json"))
        intent = copy.deepcopy(load_strict_json(FIXTURES_DIR / "plugin-ui-intent.json"))
        intent["freshness"]["generation_id"] = "old-generation"
        ack = ui.dispatch_intent(intent)
        if ack["accepted"]:
            raise AssertionError("stale UI intent was accepted")
        raise ContractError(ErrorCode.INVALID_TRANSITION, "stale UI intent was rejected")

    def ui_idempotency() -> None:
        ui = PluginUIHostFixture("generation-1", "a" * 64, "ws-1", None, None)
        ui.install_tree(load_strict_json(FIXTURES_DIR / "plugin-ui-tree.json"))
        intent = load_strict_json(FIXTURES_DIR / "plugin-ui-intent.json")
        ui.dispatch_intent(intent)
        changed = copy.deepcopy(intent)
        changed["operation_key"] = "operation-different"
        ui.dispatch_intent(changed)

    def unknown_component() -> None:
        tree = copy.deepcopy(load_strict_json(FIXTURES_DIR / "plugin-ui-tree.json"))
        tree["root"]["component"] = "unknown-component"
        assert_valid("plugin-ui-tree/v1", tree)

    def unknown_prop() -> None:
        tree = copy.deepcopy(load_strict_json(FIXTURES_DIR / "plugin-ui-tree.json"))
        tree["root"]["props"]["unknown_prop"] = True
        assert_valid("plugin-ui-tree/v1", tree)

    def unknown_event() -> None:
        ui = PluginUIHostFixture("generation-1", "a" * 64, "ws-1", None, None)
        ui.install_tree(load_strict_json(FIXTURES_DIR / "plugin-ui-tree.json"))
        intent = copy.deepcopy(load_strict_json(FIXTURES_DIR / "plugin-ui-intent.json"))
        intent["event_type"] = "change"
        ui.dispatch_intent(intent)

    def navigation_watchdog() -> str:
        ui = PluginUIHostFixture("generation-1", "a" * 64, "ws-1", None, None)
        ui.install_tree(load_strict_json(FIXTURES_DIR / "plugin-ui-tree.json"))
        model = WorkerRuntimeModel("worker://generation-1/plugin-ui", WorkerRuntimeModel.STRICT_CSP)
        model.navigate()
        if not ui.dispatch_intent(load_strict_json(FIXTURES_DIR / "plugin-ui-intent.json"))["accepted"]:
            raise AssertionError("valid UI navigation intent was not acknowledged")
        return "navigation_acknowledged"

    def navigation_watchdog_loop() -> None:
        model = WorkerRuntimeModel("worker://generation-1/plugin-ui", WorkerRuntimeModel.STRICT_CSP, navigation_limit=2)
        for _ in range(model.navigation_limit + 1):
            model.navigate()

    def backup_mode() -> None:
        bad = copy.deepcopy(load_strict_json(GOLDEN_DIR / "backup" / "backup.json"))
        bad["files"].append({"path": "plugin/pkg.whl", "size": 1, "sha256": "c" * 64, "role": "package"})
        verify_backup(bad)

    def backup_manifest() -> None:
        bad = copy.deepcopy(load_strict_json(GOLDEN_DIR / "backup" / "backup.json"))
        bad["files"][0]["sha256"] = "d" * 64
        verify_backup(bad)

    def crash_staging() -> None:
        backup = load_strict_json(GOLDEN_DIR / "backup" / "backup.json")
        model = RestoreTransactionModel("library-old", "library-new")
        model.stage(backup, crash_at="assets_verified")
        model.switch()

    def restore_new_root() -> str:
        report = load_strict_json(FIXTURES_DIR / "restore-report.json")
        verify_restore_report(report)
        model = RestoreTransactionModel(report["source_root_id"], report["target_root_id"])
        model.stage(load_strict_json(GOLDEN_DIR / "backup" / "backup.json"))
        model.rebuild_projections()
        model.switch()
        if model.target_root_id == model.source_root_id or model.state != "switched":
            raise AssertionError("restore did not switch to the distinct target root")
        return "restore_target_is_distinct"

    def projection_rebuild() -> str:
        backup = load_strict_json(GOLDEN_DIR / "backup" / "backup.json")
        model = RestoreTransactionModel("library-old", "library-new")
        model.stage(backup)
        model.rebuild_projections()
        model.switch()
        if not model.projection_markers or set(model.projection_markers) != model.rebuilt_projections:
            raise AssertionError("projection rebuild did not cover every backup marker")
        return "projection_rebuilt"

    def projection_missing() -> None:
        backup = load_strict_json(GOLDEN_DIR / "backup" / "backup.json")
        model = RestoreTransactionModel("library-old", "library-new")
        model.stage(backup)
        model.switch()

    def compatibility_valid() -> str:
        verify_compatibility(load_strict_json(CORPUS_DIR / "compatibility" / "valid.json"))
        return "compatibility_verified"

    def compatibility_or() -> None:
        bad = load_strict_json(CORPUS_DIR / "compatibility" / "valid.json")
        bad["core_api"] = ">=1.0 || <2.0"
        verify_compatibility(bad)

    def compatibility_missing() -> None:
        bad = load_strict_json(CORPUS_DIR / "compatibility" / "valid.json")
        bad.pop("python", None)
        verify_compatibility(bad)

    def history_raw_reader() -> str:
        raw = bytes.fromhex(history["raw_asset_bytes_hex"])
        if hashlib.sha256(raw).hexdigest() != history["raw_sha256"]:
            raise AssertionError("historical raw fixture hash changed")
        verify_history_bytes(raw, raw)
        return "raw_bytes_preserved"

    def history_reserialized() -> None:
        raw = bytes.fromhex(history["raw_asset_bytes_hex"])
        reserialized = json.dumps(json.loads(raw.decode("utf-8")), ensure_ascii=False).encode("utf-8")
        verify_history_bytes(reserialized, raw)

    def reserved_device() -> None:
        normalize_relative_path("CON.txt")

    def ads_path() -> None:
        normalize_relative_path("data/file.txt:stream")

    def trailing_dot_space() -> None:
        normalize_relative_path("data/name. ")

    def unicode_casefold_collision() -> None:
        build_files_sha256({"straße.txt": b"x", "strasse.txt": b"y"})

    def unicode_casefold_decomposed() -> None:
        build_files_sha256({"cafe\u0301.txt": b"x", "caf\u00e9.txt": b"y"})

    actions: dict[str, ExecutableCase] = {
        "jcs_set_permutation": jcs_set_permutation,
        "skill_order_permutation": skill_order_permutation,
        "package_golden_recompute": package_golden_recompute,
        "run_snapshot_golden_recompute": run_snapshot_golden_recompute,
        "skill_golden_recompute": skill_golden_recompute,
        "package_bytes_change": package_bytes_change,
        "profile_mismatch": result_profile_mismatch,
        "item_schema_mismatch": result_item_schema_mismatch,
        "candidate_target_write_set": candidate_target_write_set,
        "candidate_mutation": candidate_mutation,
        "candidate_base": candidate_base,
        "candidate_cross_workspace": candidate_cross_workspace,
        "parent_cycle": parent_cycle,
        "item_mapping": item_mapping,
        "candidate_base_write_set_positive": candidate_base_write_set_positive,
        "broker_ack_loss": broker_ack_loss,
        "broker_payload_reuse": broker_payload_reuse,
        "child_cancel_propagation": child_cancel_propagation,
        "child_terminal_cancel": child_terminal_cancel,
        "receipt_propagation": receipt_propagation,
        "data_interpreter": data_interpreter,
        "snapshot_asset_binding": snapshot_asset_binding,
        "plan_snapshot_binding": plan_snapshot_binding,
        "data_interpreter_binding": data_interpreter_binding,
        "operation_ack_loss": broker_ack_loss,
        "same_key_different_payload": same_key_different_payload,
        "upload_first_retry": upload_first_retry,
        "upload_middle_retry": upload_middle_retry,
        "upload_final_retry": upload_final_retry,
        "upload_status_recovery": upload_status_recovery,
        "upload_offset_ahead": upload_offset_ahead,
        "upload_final_hash_mismatch": upload_final_hash_mismatch,
        "attempt_cancel_complete_race": attempt_cancel_complete_race,
        "fresh_resume": fresh_resume,
        "checkpoint_backward": checkpoint_backward,
        "checkpoint_binding": checkpoint_binding,
        "shadow_lease": shadow_lease,
        "stale_attempt_lease": stale_attempt_lease,
        "terminal_resume": terminal_resume,
        "bundle_producer": bundle_producer,
        "bundle_snapshot": bundle_snapshot,
        "bundle_lease": bundle_lease,
        "bundle_receipt": bundle_receipt,
        "staging_incomplete": staging_incomplete,
        "outcome_transaction": outcome_transaction,
        "complete_transaction": complete_transaction,
        "install_cas": install_cas,
        "rollback_once": rollback_once,
        "safe_mode_exit": safe_mode_exit,
        "safe_mode_exit_positive": safe_mode_exit_positive,
        "settings_validator": settings_validator,
        "settings_binding": settings_binding,
        "retire_pin_race": retire_pin_race,
        "retire_attempt_barrier": retire_attempt_barrier,
        "post_delete_publication": post_delete_publication,
        "post_delete_publication_positive": post_delete_publication_positive,
        "cursor_domain": job_cursor_on_core_stream,
        "cursor_ahead": cursor_ahead,
        "snapshot_pair": snapshot_pair,
        "snapshot_convergence": snapshot_convergence,
        "cursor_domain_mix": cursor_domain_mix,
        "stream_over_ack": stream_over_ack,
        "cancel_crash_race": cancel_crash_race,
        "candidate_uniqueness": candidate_uniqueness,
        "terminal_hydration": terminal_hydration,
        "terminal_hydration_missing": terminal_hydration_missing,
        "executed_receipt": executed_receipt,
        "failed_bundleless_receipt": failed_bundleless_receipt,
        "skipped_participated": skipped_participated,
        "model_claim_without_evidence": model_claim_without_evidence,
        "verified_patch_without_proof": verified_patch_without_proof,
        "tampered_patch": tampered_patch,
        "patch_range": patch_range,
        "immutable_worker_url": immutable_worker_url,
        "worker_url": immutable_worker_url,
        "worker_csp": worker_csp,
        "ui_component": unknown_component,
        "ui_tree_whitelist": unknown_component,
        "ui_prop": unknown_prop,
        "ui_event": unknown_event,
        "ui_freshness": ui_freshness,
        "ui_idempotency": ui_idempotency,
        "navigation_watchdog": navigation_watchdog,
        "navigation_watchdog_loop": navigation_watchdog_loop,
        "backup_mode": backup_mode,
        "backup_manifest": backup_manifest,
        "crash_staging": crash_staging,
        "restore_new_root": restore_new_root,
        "projection_rebuild": projection_rebuild,
        "projection_missing": projection_missing,
        "compatibility_valid": compatibility_valid,
        "compatibility_or": compatibility_or,
        "compatibility_missing": compatibility_missing,
        "history_raw_reader": history_raw_reader,
        "history_reserialized": history_reserialized,
        "reserved_device": reserved_device,
        "ads_path": ads_path,
        "trailing_dot_space": trailing_dot_space,
        "unicode_casefold_collision": unicode_casefold_collision,
        "unicode_casefold_decomposed": unicode_casefold_decomposed,
    }
    # Convert concrete actions into factories.  The corpus metadata remains
    # the only input needed to resolve a future case ID.  Crash-point cases
    # carry their point in the case ID, so their factories retain that
    # metadata while still sharing one executable assertion.
    factories = {kind: (lambda _case, action=action: action) for kind, action in actions.items()}
    for kind in (
        "install_crash_before_prepare",
        "install_crash_after_staging",
        "install_crash_after_activate",
        "install_crash_after_settings",
        "install_crash_after_projection",
    ):
        factories[kind] = lambda case, kind=kind: durable_crash(str(case.get("case_id", kind)))
    for kind, crash_point in (
        ("aggregate_event_crash", "aggregate-event-crash"),
        ("event_crash_after_write", "event-crash-after-write"),
    ):
        factories[kind] = lambda _case, crash_point=crash_point: aggregate_event_crash(crash_point)
    return factories


def _positive_fixture_actions(
    snapshot: Mapping[str, Any],
    bundle: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    prefix: Mapping[str, Any],
    receipt: Mapping[str, Any],
    history: Mapping[str, Any],
) -> dict[str, Callable[[], Any]]:
    """Build executable checks for every positive fixture named by §84.13."""
    candidate = bundle["items"][0]
    provenance_receipt = load_strict_json(FIXTURES_DIR / "provenance-receipt.json")

    def package_golden() -> str:
        directory = GOLDEN_DIR / "package"
        files = {
            "plugin.json": (directory / "plugin.json").read_bytes(),
            "data/rules.json": (directory / "data" / "rules.json").read_bytes(),
        }
        expected = load_strict_json(directory / "expected.json")
        verify_package_identity(
            files,
            "com.plotpilot.golden.echo",
            "1.0.0",
            expected["package_hash"],
            expected["release_id"],
            expected_files_sha256=(directory / "files.sha256").read_bytes(),
        )
        return "package_golden_verified"

    def skill_golden() -> str:
        directory = GOLDEN_DIR / "skill"
        files = {
            "skill.json": (directory / "skill.json").read_bytes(),
            "prompt.txt": (directory / "prompt.txt").read_bytes(),
        }
        expected = load_strict_json(directory / "expected.json")
        verify_skill_identity(
            files,
            "com.plotpilot.skill.golden",
            "1.0.0",
            expected["skill_package_hash"],
            expected["skill_release_id"],
            expected_files_sha256=(directory / "files.sha256").read_bytes(),
        )
        return "skill_golden_verified"

    def run_snapshot() -> str:
        verify_snapshot(snapshot)
        return "run_snapshot_verified"

    def result_bundle() -> str:
        verify_result_bundle(
            bundle,
            snapshot_workspace_id=snapshot["workspace_id"],
            snapshot_hash_value=snapshot["snapshot_hash"],
        )
        return "result_bundle_verified"

    def candidate_item() -> str:
        verify_result_bundle(
            {**bundle, "items": [candidate]},
            snapshot_workspace_id=snapshot["workspace_id"],
            snapshot_hash_value=snapshot["snapshot_hash"],
        )
        return "candidate_item_verified"

    def provenance() -> str:
        verify_provenance_receipt(provenance_receipt)
        return "provenance_receipt_verified"

    def broker_invocation() -> str:
        invocation = load_strict_json(FIXTURES_DIR / "broker-invocation.json")
        assert_valid("broker-invocation/v1", invocation)
        return "broker_invocation_verified"

    def broker_child_record() -> str:
        invocation = load_strict_json(FIXTURES_DIR / "broker-invocation.json")
        assert_valid("broker-invocation/v1", invocation)
        child = {
            "child_job_id": "child-job-1",
            "parent_job_id": invocation["parent_job_id"],
            "parent_step_id": invocation["parent_step_id"],
            "parent_attempt_id": invocation["parent_attempt_id"],
            "invoke_operation_key": invocation["invoke_operation_key"],
            "binding_id": invocation["binding_id"],
            "broker_invocation_asset_id": "asset-broker-invocation-1",
            "broker_invocation_hash": sha256_hex(canonical_bytes(invocation)),
            "child_run_snapshot_asset_id": "asset-child-snapshot-1",
            "child_run_snapshot_hash": snapshot["snapshot_hash"],
            "result_contract": invocation["expected_result_contract"],
            "required": invocation["required"],
            "propagate_cancel": invocation["propagate_cancel"],
            "state": "running",
            "result_bundle_asset_id": None,
            "provenance_receipt_id": receipt["receipt_id"],
        }
        assert_valid("broker-child-record/v1", child)
        if child["result_contract"] != invocation["expected_result_contract"]:
            raise AssertionError("child result contract drifted from its broker binding")
        return "broker_child_record_verified"

    def plugin_plan() -> str:
        verify_plan(load_strict_json(FIXTURES_DIR / "plugin-plan.json"))
        return "plugin_plan_verified"

    def rpc_success() -> str:
        response = load_strict_json(FIXTURES_DIR / "rpc-success.json")
        validate_rpc_response(response, method="host.asset.read/v1")
        return "rpc_success_verified"

    def rpc_upload() -> str:
        import base64

        data = b"x"
        digest = hashlib.sha256(data).hexdigest()
        ledger = ChunkUploadLedger()
        result = ledger.create(
            operation_key="positive-upload",
            upload_id="positive-upload",
            offset=0,
            total_size=len(data),
            expected_hash=digest,
            chunk_hash=digest,
            base64_chunk=base64.b64encode(data).decode("ascii"),
            final=True,
        )
        if not result["completed"] or result["accepted_bytes"] != len(data):
            raise AssertionError("positive upload did not durably complete")
        return "rpc_upload_verified"

    def checkpoint_positive() -> str:
        verify_checkpoint(checkpoint)
        return "checkpoint_verified"

    def job_snapshot_positive() -> str:
        verify_job_snapshot(load_strict_json(FIXTURES_DIR / "job-snapshot.json"))
        return "job_snapshot_verified"

    def lifecycle_transition() -> str:
        transition = load_strict_json(FIXTURES_DIR / "plugin-lifecycle-transition.json")
        assert_valid("plugin-lifecycle-transition/v1", transition)
        model = InstallTransactionModel()
        model.begin(model.current_generation_id, transition["target_generation_id"])
        for phase in InstallTransactionModel._PHASES[1:]:
            model.advance(phase)
        if model.state != "lkg_promoted" or model.package_store_status != "published":
            raise AssertionError("install model did not reach the durable promoted state")
        return "lifecycle_transition_verified"

    def settings_migration() -> str:
        assert_valid("settings-migration-manifest/v1", load_strict_json(FIXTURES_DIR / "settings-migration-manifest.json"))
        return "settings_migration_verified"

    def settings_validation() -> str:
        revision = load_strict_json(FIXTURES_DIR / "settings-revision.json")
        model = InstallTransactionModel()
        model.validate_settings(load_strict_json(FIXTURES_DIR / "settings-validation-receipt.json"), revision)
        return "settings_validation_verified"

    def release_retirement() -> str:
        release = load_strict_json(FIXTURES_DIR / "release-retirement.json")
        assert_valid("release-retirement/v1", release)
        return "release_retirement_verified"

    def release_pin() -> str:
        pin = load_strict_json(FIXTURES_DIR / "release-pin.json")
        assert_valid("release-pin/v1", pin)
        return "release_pin_verified"

    def publication_after_package_delete() -> str:
        model = PublicationModel(load_strict_json(FIXTURES_DIR / "release-retirement.json")["release_id"])
        model.retire()
        model.retain_provenance(provenance_receipt)
        model.delete_package()
        model.publish(bundle, provenance_receipt, snapshot=snapshot)
        if model.package_present or not model.published_candidates:
            raise AssertionError("publication did not use retained provenance after package deletion")
        return "publication_provenance_backed"

    def core_event() -> str:
        event = load_strict_json(FIXTURES_DIR / "core-event.json")
        model = CoreEventTransactionModel(event["aggregate_id"])
        model.append(event)
        model.publish_sse()
        return "core_event_committed"

    def core_snapshot() -> str:
        verify_core_snapshot(load_strict_json(FIXTURES_DIR / "core-snapshot.json"))
        return "core_snapshot_verified"

    def plugin_job_event() -> str:
        assert_valid("plugin-job-event/v1", load_strict_json(FIXTURES_DIR / "plugin-job-event.json"))
        return "plugin_job_event_verified"

    def sse_recovery() -> str:
        verify_sse_recovery(load_strict_json(FIXTURES_DIR / "sse-recovery.json"))
        return "sse_recovery_verified"

    def stream_prefix() -> str:
        verify_stream_prefix(prefix)
        return "stream_prefix_verified"

    def skill_run_receipt() -> str:
        verify_skill_receipt(receipt)
        return "skill_receipt_verified"

    def skill_chain_result() -> str:
        chain = load_strict_json(FIXTURES_DIR / "skill-chain-result.json")
        verify_skill_chain(chain, [receipt])
        return "skill_chain_verified"

    def bundleless_failed_receipt() -> str:
        failed = copy.deepcopy(receipt)
        failed.update({"result_bundle_id": None, "result_item_id": None, "step_state": "failed"})
        failed["receipt_hash"] = hash_without_field(failed, "receipt_hash", "skill-run-receipt/v1")
        verify_skill_receipt(failed)
        return "bundleless_failure_verified"

    def ui_tree() -> str:
        tree = load_strict_json(FIXTURES_DIR / "plugin-ui-tree.json")
        assert_valid("plugin-ui-tree/v1", tree)
        ui = PluginUIHostFixture("generation-1", "a" * 64, "ws-1", None, None)
        ui.install_tree(tree)
        return "ui_tree_verified"

    def ui_intent() -> str:
        ui = PluginUIHostFixture("generation-1", "a" * 64, "ws-1", None, None)
        ui.install_tree(load_strict_json(FIXTURES_DIR / "plugin-ui-tree.json"))
        ack = ui.dispatch_intent(load_strict_json(FIXTURES_DIR / "plugin-ui-intent.json"))
        if not ack["accepted"]:
            raise AssertionError("valid UI intent was not accepted")
        return "ui_intent_verified"

    def ui_message() -> str:
        assert_valid("plugin-ui-message/v1", load_strict_json(FIXTURES_DIR / "plugin-ui-message.json"))
        return "ui_message_verified"

    def backup_golden() -> str:
        verify_backup(load_strict_json(GOLDEN_DIR / "backup" / "backup.json"))
        return "backup_verified"

    def restore_report() -> str:
        verify_restore_report(load_strict_json(FIXTURES_DIR / "restore-report.json"))
        return "restore_report_verified"

    def restore_new_root() -> str:
        report = load_strict_json(FIXTURES_DIR / "restore-report.json")
        verify_restore_report(report)
        model = RestoreTransactionModel(report["source_root_id"], report["target_root_id"])
        model.stage(load_strict_json(GOLDEN_DIR / "backup" / "backup.json"))
        model.rebuild_projections()
        model.switch()
        return "restore_new_root_verified"

    def projection_rebuild() -> str:
        backup = load_strict_json(GOLDEN_DIR / "backup" / "backup.json")
        model = RestoreTransactionModel("library-old", "library-new")
        model.stage(backup)
        model.rebuild_projections()
        model.switch()
        return "projection_rebuilt"

    def compatibility() -> str:
        verify_compatibility(load_strict_json(CORPUS_DIR / "compatibility" / "valid.json"))
        return "compatibility_verified"

    def history_raw() -> str:
        raw = bytes.fromhex(history["raw_asset_bytes_hex"])
        if hashlib.sha256(raw).hexdigest() != history["raw_sha256"]:
            raise AssertionError("historical raw fixture hash changed")
        verify_history_bytes(raw, raw)
        return "raw_bytes_preserved"

    def windows_casefold_valid() -> str:
        files = {"café.txt": b"accent", "notes.txt": b"notes"}
        manifest = build_files_sha256(files)
        verify_package_manifest(files, manifest)
        return "windows_casefold_unique"

    return {
        "package-golden": package_golden,
        "skill-golden": skill_golden,
        "run-snapshot-golden": run_snapshot,
        "run-snapshot": run_snapshot,
        "result-bundle": result_bundle,
        "candidate-item": candidate_item,
        "provenance-receipt": provenance,
        "broker-invocation": broker_invocation,
        "broker-child-record": broker_child_record,
        "plugin-plan": plugin_plan,
        "rpc-success": rpc_success,
        "rpc-upload": rpc_upload,
        "checkpoint": checkpoint_positive,
        "job-snapshot": job_snapshot_positive,
        "plugin-lifecycle-transition": lifecycle_transition,
        "settings-migration-manifest": settings_migration,
        "settings-validation-receipt": settings_validation,
        "release-retirement": release_retirement,
        "release-pin": release_pin,
        "publication-after-package-delete": publication_after_package_delete,
        "core-event": core_event,
        "core-snapshot": core_snapshot,
        "plugin-job-event": plugin_job_event,
        "sse-recovery": sse_recovery,
        "stream-prefix": stream_prefix,
        "skill-run-receipt": skill_run_receipt,
        "skill-chain-result": skill_chain_result,
        "failed-bundleless-receipt": bundleless_failed_receipt,
        "plugin-ui-tree": ui_tree,
        "plugin-ui-intent": ui_intent,
        "plugin-ui-message": ui_message,
        "backup-golden": backup_golden,
        "restore-report": restore_report,
        "restore-new-root": restore_new_root,
        "projection-rebuild": projection_rebuild,
        "compatibility-valid": compatibility,
        "history-v1": history_raw,
        "windows-casefold-valid": windows_casefold_valid,
    }



def verify_negative_cases(*, executable_case_registry: Mapping[str, ExecutableCase] | None = None) -> dict[str, Any]:
    """Execute every §84.13 positive and negative corpus entry.

    The corpus is the source of the case inventory.  Each negative entry is
    resolved by its declared executable ``kind`` (or an explicitly registered
    case override), and each positive fixture is invoked through a semantic
    verifier or one of the deterministic transaction models above.  Nothing
    in this gate is a success marker: an action must actually return without
    an exception, while a negative action must raise the declared contract
    error code.
    """
    corpus_paths = sorted((CORPUS_DIR / "negative" / "84.13").glob("*.json"))
    if len(corpus_paths) != 14:
        raise AssertionError(f"§84.13 requires exactly 14 corpus files, found {len(corpus_paths)}")

    corpus_groups: list[dict[str, Any]] = []
    seen_group_ids: set[str] = set()
    seen_case_ids: set[str] = set()
    for path in corpus_paths:
        group = load_strict_json(path)
        if not isinstance(group, Mapping):
            raise AssertionError(f"§84.13 corpus entry is not an object: {path.name}")
        group_id = group.get("group_id")
        expected_group_id = f"84.13-{len(corpus_groups) + 1:02d}"
        if group_id != expected_group_id or group_id in seen_group_ids:
            raise AssertionError(f"§84.13 groups must be exactly ordered 01..14: {group_id!r}")
        seen_group_ids.add(group_id)
        positive = group.get("positive")
        negative = group.get("negative")
        if not isinstance(positive, list) or not positive or any(not isinstance(item, str) or not item for item in positive):
            raise AssertionError(f"{group_id} must declare non-empty positive fixture IDs")
        if len(positive) != len(set(positive)):
            raise AssertionError(f"{group_id} contains duplicate positive fixture IDs")
        if not isinstance(negative, list) or not negative:
            raise AssertionError(f"{group_id} must declare non-empty negative cases")
        for case in negative:
            if not isinstance(case, Mapping):
                raise AssertionError(f"{group_id} contains a non-object negative case")
            case_id = case.get("case_id")
            kind = case.get("kind")
            if not isinstance(case_id, str) or not case_id or case_id in seen_case_ids:
                raise AssertionError(f"§84.13 negative case ID is missing or duplicated: {case_id!r}")
            if not isinstance(kind, str) or not kind:
                raise AssertionError(f"{group_id}/{case_id} has no executable kind")
            expected_code = case.get("expected_error_code")
            if expected_code is not None and (isinstance(expected_code, bool) or not isinstance(expected_code, int)):
                raise AssertionError(f"{group_id}/{case_id} has a non-integer expected error code")
            seen_case_ids.add(case_id)
        corpus_groups.append(dict(group))

    snapshot = load_strict_json(GOLDEN_DIR / "run-snapshot" / "snapshot.json")
    bundle = load_strict_json(EXAMPLES_DIR / "result-bundle.json")
    candidate = bundle["items"][0]
    checkpoint = load_strict_json(FIXTURES_DIR / "checkpoint.json")
    prefix = load_strict_json(FIXTURES_DIR / "stream-prefix.json")
    receipt = load_strict_json(FIXTURES_DIR / "skill-run-receipt.json")
    history = load_strict_json(CORPUS_DIR / "history" / "v1-raw.json")

    kind_registry = _expanded_kind_handlers(snapshot, bundle, candidate, checkpoint, prefix, receipt, history)
    kind_registry.update(EXECUTABLE_KIND_REGISTRY)
    case_registry: dict[str, ExecutableCase] = dict(EXECUTABLE_CASE_REGISTRY)
    if executable_case_registry is not None:
        case_registry.update(executable_case_registry)
    case_registry = build_executable_case_registry(
        corpus_groups,
        registry=case_registry,
        kind_registry=kind_registry,
    )
    registry_stats = validate_executable_case_registry(case_registry, corpus_groups=corpus_groups)
    if registry_stats["corpus_groups"] != 14 or registry_stats["corpus_cases"] != 105:
        raise AssertionError(
            "§84.13 executable inventory drifted: "
            f"groups={registry_stats['corpus_groups']} cases={registry_stats['corpus_cases']}"
        )

    positive_actions = _positive_fixture_actions(snapshot, bundle, checkpoint, prefix, receipt, history)

    def run_positive_fixture(fixture_id: str) -> dict[str, Any]:
        action = positive_actions.get(fixture_id)
        if action is None:
            raise AssertionError(f"§84.13 positive fixture has no executable mapping: {fixture_id}")
        try:
            outcome = action()
        except Exception as exc:
            raise AssertionError(f"positive fixture {fixture_id} was rejected: {exc}") from exc
        return {
            "fixture_id": fixture_id,
            "passed": True,
            "outcome": outcome or "verified",
        }

    corpus_results: dict[str, Any] = {}
    positive_results: list[dict[str, Any]] = []
    total_negative_cases = 0
    for group in corpus_groups:
        group_id = group["group_id"]
        group_positive = [run_positive_fixture(fixture_id) for fixture_id in group["positive"]]
        positive_results.extend({"group_id": group_id, **item} for item in group_positive)
        group_negative = [
            _run_negative_case(case["case_id"], case.get("expected_error_code"), case_registry[case["case_id"]])
            for case in group["negative"]
        ]
        total_negative_cases += len(group_negative)
        corpus_results[group_id] = {
            "title": group["title"],
            "positive_fixture_ids": list(group["positive"]),
            "positive_count": len(group_positive),
            "positive": group_positive,
            "case_count": len(group_negative),
            "passed": all(item["passed"] for item in group_negative),
            "cases": group_negative,
        }

    if total_negative_cases != 105:
        raise AssertionError(f"§84.13 negative corpus count drifted: {total_negative_cases}")
    return {
        "group_count": len(corpus_results),
        "case_count": total_negative_cases,
        "positive_count": len(positive_results),
        "positive_fixture_ids": sorted({item["fixture_id"] for item in positive_results}),
        "positive": positive_results,
        "registry": registry_stats,
        "groups": corpus_results,
    }

def verify_remediation_probes() -> dict[str, Any]:
    """Exercise the F-04/F-05/F-06 semantic probes outside the legacy corpus."""
    snapshot = load_strict_json(GOLDEN_DIR / "run-snapshot" / "snapshot.json")
    bundle = load_strict_json(EXAMPLES_DIR / "result-bundle.json")
    candidate = bundle["items"][0]
    receipt = load_strict_json(FIXTURES_DIR / "skill-run-receipt.json")

    self_hash_cases: list[tuple[str, dict[str, Any], Callable[[Mapping[str, Any]], None], str]] = [
        ("data-bundle", _regenerated_self_hash("plugin-data-bundle.json", "bundle_hash", "plugin-data-bundle/v1"), verify_data_bundle, "bundle_hash"),
        ("core-snapshot", _regenerated_self_hash("core-snapshot.json", "snapshot_hash", "core-snapshot/v1"), verify_core_snapshot, "snapshot_hash"),
        ("job-snapshot", _regenerated_self_hash("job-snapshot.json", "snapshot_hash", "job-snapshot/v1"), verify_job_snapshot, "snapshot_hash"),
        ("checkpoint", _regenerated_self_hash("checkpoint.json", "checkpoint_hash", "checkpoint/v1"), verify_checkpoint, "checkpoint_hash"),
        ("settings-validation", _regenerated_self_hash("settings-validation-receipt.json", "receipt_hash", "settings-validation-receipt/v1"), verify_settings_validation_receipt, "receipt_hash"),
        ("provenance", _regenerated_self_hash("provenance-receipt.json", "receipt_hash", "provenance-receipt/v1"), verify_provenance_receipt, "receipt_hash"),
    ]
    for label, value, verifier, field in self_hash_cases:
        verifier(value)
        tampered = copy.deepcopy(value)
        tampered[field] = "0" * 64
        _expect_failure(lambda tampered=tampered, verifier=verifier: verifier(tampered), ErrorCode.RESULT_CONTRACT_MISMATCH)

    verify_backup(load_strict_json(GOLDEN_DIR / "backup" / "backup.json"))
    tampered_backup = copy.deepcopy(load_strict_json(GOLDEN_DIR / "backup" / "backup.json"))
    tampered_backup["bundle_hash"] = "0" * 64
    _expect_failure(lambda: verify_backup(tampered_backup), ErrorCode.RESULT_CONTRACT_MISMATCH)
    verify_skill_receipt(receipt)
    tampered_receipt = copy.deepcopy(receipt)
    tampered_receipt["receipt_hash"] = "0" * 64
    _expect_failure(lambda: verify_skill_receipt(tampered_receipt), ErrorCode.RESULT_CONTRACT_MISMATCH)
    chain = load_strict_json(FIXTURES_DIR / "skill-chain-result.json")
    verify_skill_chain(chain, [receipt])
    tampered_chain = copy.deepcopy(chain)
    tampered_chain["chain_hash"] = "0" * 64
    _expect_failure(lambda: verify_skill_chain(tampered_chain, [receipt]), ErrorCode.RESULT_CONTRACT_MISMATCH)

    duplicate = copy.deepcopy(candidate)
    duplicate["mutation"]["payload_hash"] = "c" * 64
    _expect_failure(
        lambda: verify_result_bundle(
            {**bundle, "items": [candidate, duplicate]},
            snapshot_workspace_id=snapshot["workspace_id"],
            snapshot_hash_value=snapshot["snapshot_hash"],
        ),
        ErrorCode.RESULT_CONTRACT_MISMATCH,
    )

    base_mismatch = copy.deepcopy(candidate)
    base_mismatch["base"]["revision_id"] = "revision-other"
    _expect_failure(
        lambda: verify_result_bundle(
            {**bundle, "items": [base_mismatch]},
            snapshot_workspace_id=snapshot["workspace_id"],
            snapshot_hash_value=snapshot["snapshot_hash"],
        ),
        ErrorCode.RESULT_CONTRACT_MISMATCH,
    )

    skill_dir = GOLDEN_DIR / "skill"
    skill_files = {
        "skill.json": (skill_dir / "skill.json").read_bytes(),
        "prompt.txt": (skill_dir / "prompt.txt").read_bytes(),
    }
    exact_manifest = build_files_sha256(skill_files)
    verify_package_manifest(skill_files, exact_manifest)
    reordered_manifest = b"".join(reversed(exact_manifest.splitlines(keepends=True)))
    _expect_failure(lambda: verify_package_manifest(skill_files, reordered_manifest), ErrorCode.RESULT_CONTRACT_MISMATCH)

    collision_files = {"straße.txt": b"x", "strasse.txt": b"y"}
    _expect_failure(lambda: build_files_sha256(collision_files), ErrorCode.ASSET_ERROR)
    verify_package_manifest({"straße.txt": b"x"}, build_files_sha256({"straße.txt": b"x"}))

    bad_ref_bundle = copy.deepcopy(bundle)
    bad_ref_bundle["skill_chain_result_refs"] = [{
        "schema": "skill-chain-ref/v1",
        "chain_result_id": "chain-1",
        "asset_id": "asset-chain-1",
        "asset_hash": None,
        "result_bundle_id": bundle["bundle_id"],
        "result_item_id": candidate["item_id"],
        "stream_id": None,
        "acked_prefix_hash": None,
    }]
    _expect_failure(
        lambda: verify_result_bundle(
            bad_ref_bundle,
            snapshot_workspace_id=snapshot["workspace_id"],
            snapshot_hash_value=snapshot["snapshot_hash"],
        ),
        ErrorCode.RESULT_CONTRACT_MISMATCH,
    )

    bundleless_failed = copy.deepcopy(receipt)
    bundleless_failed.update({"result_bundle_id": None, "result_item_id": None, "step_state": "failed"})
    bundleless_failed["receipt_hash"] = hash_without_field(bundleless_failed, "receipt_hash", "skill-run-receipt/v1")
    verify_skill_receipt(bundleless_failed)

    heartbeat = load_strict_json(FIXTURES_DIR / "rpc-notification.json")
    _expect_failure(lambda: validate_rpc_request(heartbeat, expected_lease_epoch=2), ErrorCode.STALE_LEASE)
    validate_rpc_request(heartbeat, expected_lease_epoch=1)

    _expect_failure(
        lambda: validate_rpc_result("job.pause", {"accepted": True, "checkpoint_asset_id": None}),
        ErrorCode.CHECKPOINT_INVALID,
    )

    descriptor = load_strict_json(FIXTURES_DIR / "capability-provider.json")
    verify_capability_descriptor(descriptor, expected_capability_id=descriptor["capability_id"])
    _expect_failure(
        lambda: verify_capability_descriptor(descriptor, expected_capability_id="fixture.unknown/v1"),
        ErrorCode.RESULT_CONTRACT_MISMATCH,
    )
    manifest = copy.deepcopy(load_strict_json(FIXTURES_DIR / "plugin-manifest-code.json"))
    manifest["ui"] = {
        "entry": "ui.js",
        "runtime": "worker-ui/v1",
        "contributions": [{"contribution_id": "contribution-1", "slot": "workbench.writing-assets.panel", "capability_id": "fixture.unknown/v1"}],
    }
    _expect_failure(lambda: verify_manifest(manifest), ErrorCode.RESULT_CONTRACT_MISMATCH)

    request = load_strict_json(FIXTURES_DIR / "rpc-request.json")
    validate_rpc_request(request)
    wrong_result = load_strict_json(FIXTURES_DIR / "rpc-success.json")
    _expect_failure(
        lambda: validate_rpc_response(wrong_result, "host.asset.create/v1", request=request),
        ErrorCode.RESULT_CONTRACT_MISMATCH,
    )
    valid_result = {
        "upload_id": "upload-1",
        "accepted_bytes": 0,
        "completed": False,
        "asset_id": None,
    }
    validate_rpc_result("host.asset.create/v1", valid_result, request=request)

    return {
        "self_hash_profiles": len(self_hash_cases) + 3,
        "nfc_casefold_collision": "straße.txt/strasse.txt rejected",
        "bundleless_failed_receipt": "accepted",
        "heartbeat_fencing": "stale rejected/current accepted",
        "rpc_method_result_binding": "wrong branch rejected/correct branch accepted",
        "capability_ui_refs": "unknown refs rejected",
    }


def _publication_fixture(reference: str) -> Any:
    path_text, _, fragment = reference.partition("#")
    path = ROOT / path_text if path_text.startswith("contracts/") else GOLDEN_DIR / "contract-publication-v1" / path_text
    value = load_strict_json(path)
    if fragment:
        for token in fragment.lstrip("/").split("/"):
            if not token:
                continue
            value = value[int(token)] if isinstance(value, list) else value[token]
    return copy.deepcopy(value)


def _publication_mutation(value: Any, mutation: Mapping[str, Any]) -> Any:
    op = mutation.get("op")
    if op == "noop":
        return copy.deepcopy(value)
    if op == "raw_variant":
        if not isinstance(value, Mapping):
            raise AssertionError("raw publication variant requires an object fixture")
        raw = canonical_bytes(value)
        variant = mutation.get("variant")
        if variant == "bom":
            return b"\xef\xbb\xbf" + raw
        if variant == "whitespace":
            return b" " + raw
        if variant == "duplicate_key":
            marker = b'{"core_snapshot_revision":'
            if marker not in raw:
                raise AssertionError("export golden layout no longer supports duplicate-key probe")
            return raw.replace(marker, b'{"schema":"export-current-revisions/v1","core_snapshot_revision":', 1)
        raise AssertionError(f"unsupported raw publication variant: {mutation}")
    if op not in {"set", "delete"}:
        raise AssertionError(f"unsupported publication corpus mutation: {mutation}")
    result = copy.deepcopy(value)
    target = result
    path = list(mutation["path"])
    if not path:
        raise AssertionError(f"publication corpus mutation path is empty: {mutation}")
    for token in path[:-1]:
        target = target[token]
    if op == "delete":
        del target[path[-1]]
    else:
        target[path[-1]] = copy.deepcopy(mutation["value"])
    return result


def _request_failure_fixture(reference: str) -> Any:
    raw_path, _, fragment = reference.partition("#")
    path = GOLDEN_DIR / "core-http-request-failure-v1" / raw_path
    value: Any = load_strict_json(path)
    for token in (item for item in fragment.removeprefix("/").split("/") if item):
        value = value[int(token)] if isinstance(value, list) else value[token]
    return copy.deepcopy(value)


def _request_failure_mutation(value: Any, mutation: Mapping[str, Any]) -> Any:
    result = copy.deepcopy(value)
    path = list(mutation["path"])
    target = result
    for token in path[:-1]:
        target = target[token]
    operation = mutation["op"]
    if operation == "set":
        target[path[-1]] = copy.deepcopy(mutation["value"])
    elif operation == "delete":
        del target[path[-1]]
    elif operation == "swap":
        left, right = mutation["indices"]
        sequence = target[path[-1]]
        sequence[left], sequence[right] = sequence[right], sequence[left]
    else:
        raise AssertionError(f"unsupported request-failure mutation: {mutation}")
    return result


def verify_core_http_request_failure_contract() -> dict[str, Any]:
    """Verify ADR-043 through every published Python/TypeScript ingress."""

    golden_path = GOLDEN_DIR / "core-http-request-failure-v1" / "expected.json"
    corpus_path = CORPUS_DIR / "core-http-request-failure-v1" / "negative.json"
    golden = load_strict_json(golden_path)
    corpus = load_strict_json(corpus_path)

    schemas = {
        "request_error": (
            "core-http-request-error/v1",
            Draft202012Validator(
                load_strict_json(SCHEMA_DIR / "core-http-request-error-v1.schema.json")
            ),
        ),
        "request_failure_policy": (
            "core-http-request-failure-policy/v1",
            Draft202012Validator(
                load_strict_json(SCHEMA_DIR / "core-http-request-failure-policy-v1.schema.json")
            ),
        ),
    }

    for value in golden["errors"]:
        if list(schemas["request_error"][1].iter_errors(value)):
            raise AssertionError("Draft 2020-12 rejected a request-error golden")
        assert_valid(schemas["request_error"][0], value)
    if list(schemas["request_failure_policy"][1].iter_errors(golden["policy"])):
        raise AssertionError("Draft 2020-12 rejected the request-failure policy golden")
    assert_valid(schemas["request_failure_policy"][0], golden["policy"])

    parsed_errors = [parse_core_http_request_error(value) for value in golden["errors"]]
    parsed_policy = parse_core_http_request_failure_policy(golden["policy"])
    expected_codes = [
        "malformed_json",
        "invalid_request",
        "invalid_query",
        "range_out_of_bounds",
    ]
    if [value["error_code"] for value in parsed_errors] != expected_codes:
        raise AssertionError("request-error golden code order drifted")
    if any(value["retryable"] is not False for value in parsed_errors):
        raise AssertionError("request-error golden must be non-retryable")
    if parsed_policy["status"] != 400 or parsed_policy["retryable"] is not False:
        raise AssertionError("request-failure policy must bind HTTP 400/non-retryable")

    draft_negative: list[str] = []
    generic_negative: list[str] = []
    python_negative: list[str] = []
    for case in corpus["cases"]:
        value = _request_failure_mutation(
            _request_failure_fixture(case["fixture"]),
            case["mutation"],
        )
        validator = case["validator"]
        if validator not in schemas:
            raise AssertionError(f"unknown request-failure validator: {validator}")
        contract_id, draft_validator = schemas[validator]
        if not list(draft_validator.iter_errors(value)):
            raise AssertionError(f"Draft 2020-12 false-accepted {case['case_id']}")
        draft_negative.append(case["case_id"])
        _expect_failure(lambda contract_id=contract_id, value=value: assert_valid(contract_id, value))
        generic_negative.append(case["case_id"])
        if validator == "request_error":
            action = lambda value=value: parse_core_http_request_error(value)
        elif validator == "request_failure_policy":
            action = lambda value=value: parse_core_http_request_failure_policy(value)
        else:
            raise AssertionError(f"unknown request-failure validator: {validator}")
        _expect_failure(action)
        python_negative.append(case["case_id"])

    script = r'''
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const core = await import(pathToFileURL(process.env.PLOTPILOT_TS_CORE_API).href)
const readJson = path => JSON.parse(readFileSync(path, 'utf8'))
const golden = readJson(process.env.PLOTPILOT_REQUEST_FAILURE_GOLDEN)
const corpus = readJson(process.env.PLOTPILOT_REQUEST_FAILURE_CORPUS)

const fixture = reference => {
  const [, fragment = ''] = reference.split('#', 2)
  let value = structuredClone(golden)
  for (const token of fragment.replace(/^\//, '').split('/').filter(Boolean)) {
    value = Array.isArray(value) ? value[Number(token)] : value[token]
  }
  return structuredClone(value)
}
const mutate = (value, mutation) => {
  const output = structuredClone(value)
  let target = output
  for (const token of mutation.path.slice(0, -1)) target = target[token]
  const key = mutation.path.at(-1)
  if (mutation.op === 'set') target[key] = structuredClone(mutation.value)
  else if (mutation.op === 'delete') Array.isArray(target) ? target.splice(Number(key), 1) : delete target[key]
  else if (mutation.op === 'swap') {
    const sequence = target[key]
    const [left, right] = mutation.indices
    ;[sequence[left], sequence[right]] = [sequence[right], sequence[left]]
  } else throw new Error(`unsupported mutation ${JSON.stringify(mutation)}`)
  return output
}
const rejected = action => {
  try { action(); return false } catch (_) { return true }
}

for (const value of golden.errors) core.parseCoreHttpRequestErrorV1(value)
core.parseCoreHttpRequestFailurePolicyV1(golden.policy)
const executed = []
for (const testCase of corpus.cases) {
  const value = mutate(fixture(testCase.fixture), testCase.mutation)
  const action = testCase.validator === 'request_error'
    ? () => core.parseCoreHttpRequestErrorV1(value)
    : testCase.validator === 'request_failure_policy'
      ? () => core.parseCoreHttpRequestFailurePolicyV1(value)
      : () => { throw new Error(`unknown validator ${testCase.validator}`) }
  if (!rejected(action)) throw new Error(`TypeScript false-accepted ${testCase.case_id}`)
  executed.push(testCase.case_id)
}
console.log(JSON.stringify({ status: 'ok', positive_errors: golden.errors.length, negative_cases: executed }))
'''
    environment = os.environ.copy()
    environment.update(
        {
            "NODE_NO_WARNINGS": "1",
            "PLOTPILOT_TS_CORE_API": str(ROOT / "frontend" / "src" / "contracts" / "core-api.ts"),
            "PLOTPILOT_REQUEST_FAILURE_GOLDEN": str(golden_path),
            "PLOTPILOT_REQUEST_FAILURE_CORPUS": str(corpus_path),
        }
    )
    completed = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(f"TypeScript request-failure verifier failed:\n{completed.stdout}\n{completed.stderr}")
    typescript = json.loads(completed.stdout)
    expected_negative = [case["case_id"] for case in corpus["cases"]]
    if typescript.get("status") != "ok" or typescript.get("negative_cases") != expected_negative:
        raise AssertionError(f"TypeScript request-failure result drifted: {typescript}")
    return {
        "status": "ok",
        "http_status": parsed_policy["status"],
        "positive_errors": len(parsed_errors),
        "draft_negative_cases": draft_negative,
        "generic_negative_cases": generic_negative,
        "python_negative_cases": python_negative,
        "typescript_negative_cases": typescript["negative_cases"],
    }


def verify_contract_publication() -> dict[str, Any]:
    """Verify the additive P0 contract-publication families and corpus."""

    golden = GOLDEN_DIR / "contract-publication-v1"
    http = load_strict_json(golden / "core-http.json")
    expected = load_strict_json(golden / "expected.json")
    context = load_strict_json(golden / "context-identity.json")
    export_value = load_strict_json(golden / "export-current-revisions.json")
    export_snapshot = load_strict_json(golden / "export-run-snapshot.json")

    for value in http["authority_payloads"]:
        parse_core_authority(value, expected_workspace_id="ws-1")
    command = http["publication"]["command"]
    publication_result = http["publication"]["result"]
    parse_publication(command, expected_workspace_id="ws-1")
    parse_publication(publication_result, expected_workspace_id="ws-1", command=command)
    for value in http["assets"].values():
        parse_asset_contract(value)
    verify_asset_metadata_range_pair(http["assets"]["metadata"], http["assets"]["range"])

    fixture = CoreHttpContractFixture()
    matrix = load_strict_json(SCHEMA_DIR / "core-api-method-matrix.v1.json")
    expected_route_ids = {route["route_id"] for route in matrix["routes"]}
    observed_route_ids = {exchange["route_id"] for exchange in http["exchanges"]}
    if observed_route_ids != expected_route_ids or any(
        not any(item["route_id"] == route_id and item["status"] in matrix_route["success_statuses"] for item in http["exchanges"])
        for route_id, matrix_route in ((route["route_id"], route) for route in matrix["routes"])
    ):
        raise AssertionError(
            f"Core HTTP fixtures must cover each matrix route with a success exchange: "
            f"missing={sorted(expected_route_ids - observed_route_ids)}, extra={sorted(observed_route_ids - expected_route_ids)}"
        )
    for exchange in http["exchanges"]:
        fixture.add(exchange["route_id"], exchange["request"], exchange["status"], exchange["response"])
        status, response = fixture.request(exchange["route_id"], exchange["request"])
        if status != exchange["status"] or canonical_bytes(response) != canonical_bytes(exchange["response"]):
            raise AssertionError("Core HTTP typed fixture changed the frozen exchange")
    update_exchange = next(exchange for exchange in http["exchanges"] if exchange["route_id"] == "workspace.update")
    stale_request = copy.deepcopy(update_exchange["request"])
    stale_request["expected_revision"] += 1
    _expect_failure(lambda: fixture.request("workspace.update", stale_request))
    reused_key = copy.deepcopy(update_exchange["request"])
    reused_key["title"] = "Unfrozen payload under the same operation key"
    _expect_failure(lambda: fixture.request("workspace.update", reused_key))
    for exchange in http["exchanges"]:
        if exchange["status"] < 400:
            continue
        status, response = fixture.request(exchange["route_id"], exchange["request"])
        if status != exchange["status"] or response != exchange["response"]:
            raise AssertionError(f"Core HTTP typed failure exchange drifted: {exchange['route_id']}")
        if exchange["route_id"] == "publication.accept":
            expected_code = "incomplete_publication"
        elif exchange["request"].get("workspace_id") == "ws-other":
            expected_code = "cross_workspace"
        elif exchange["request"].get("document_id") == "doc-unknown":
            expected_code = "unknown_reference"
        elif exchange["request"].get("expected_revision") == 99:
            expected_code = "stale_cas"
        else:
            expected_code = "operation_key_reuse"
        if response.get("error_code") != expected_code:
            raise AssertionError(f"Core HTTP failure code drifted for {exchange['route_id']}: {response}")

    identities: dict[str, str] = {}
    for vector in context["vectors"]:
        if vector["profile"] == "control":
            projection = operation_context_projection(vector["meta"])
            identity = derive_operation_context_identity(vector["meta"])
        else:
            projection = operation_context_projection(
                vector["meta"], expected_lease_epoch=vector["expected_lease_epoch"]
            )
            identity = derive_operation_context_identity(
                vector["meta"], expected_lease_epoch=vector["expected_lease_epoch"]
            )
        assert_valid("operation-context-identity/v1", projection)
        if projection != vector["projection"] or identity != vector["context_identity"]:
            raise AssertionError("operation context identity golden mismatch")
        identities[vector["profile"]] = identity
    if identities != expected["context_identities"]:
        raise AssertionError("operation context identity inventory mismatch")

    export_bytes = (golden / "export-current-revisions.json").read_bytes()
    if hashlib.sha256(export_bytes).hexdigest() != expected["export_asset_sha256"]:
        raise AssertionError("export-current-revisions Asset bytes drifted")
    verify_export_current_revisions_asset(
        export_bytes,
        export_snapshot,
        asset_id="asset-export-current-revisions",
    )

    corpus = load_strict_json(CORPUS_DIR / "contract-publication-v1" / "negative.json")
    executed: list[str] = []
    for case in corpus["cases"]:
        validator = case["validator"]
        if validator.startswith("typescript_"):
            continue
        value = _publication_mutation(_publication_fixture(case["fixture"]), case["mutation"])

        def action() -> None:
            if validator == "core_authority":
                parse_core_authority(value)
            elif validator == "core_authority_expected_workspace":
                parse_core_authority(value, expected_workspace_id="ws-1")
            elif validator == "publication":
                parse_publication(value)
            elif validator == "publication_expected_workspace":
                parse_publication(value, expected_workspace_id="ws-1")
            elif validator == "publication_command_binding":
                parse_publication(value, command=command)
            elif validator == "asset":
                parse_asset_contract(value)
            elif validator == "asset_pair":
                verify_asset_metadata_range_pair(http["assets"]["metadata"], value)
            elif validator == "asset_pair_metadata":
                verify_asset_metadata_range_pair(value, http["assets"]["range"])
            elif validator == "http_fixture_request":
                fixture.request(case["route_id"], value)
            elif validator == "context_projection_schema":
                assert_valid("operation-context-identity/v1", value)
            elif validator == "context_stale_epoch":
                derive_operation_context_identity(value, expected_lease_epoch=9)
            elif validator == "context_missing_expected_epoch":
                derive_operation_context_identity(value)
            elif validator == "context_identity":
                derive_operation_context_identity(value)
            elif validator == "export":
                parse_export_current_revisions(value)
            elif validator == "export_expected_workspace":
                parse_export_current_revisions(value, expected_workspace_id="ws-1")
            elif validator == "export_snapshot_binding":
                verify_export_current_revisions_asset(canonical_bytes(value), export_snapshot, asset_id="asset-export-current-revisions")
            elif validator == "export_snapshot_record":
                verify_export_current_revisions_asset(export_bytes, value, asset_id="asset-export-current-revisions")
            elif validator == "export_raw_variant":
                raw = value if isinstance(value, (bytes, bytearray, memoryview)) else canonical_bytes(value)
                verify_export_current_revisions_asset(raw, export_snapshot, asset_id="asset-export-current-revisions")
            elif validator == "export_object_caller_hash":
                parse_export_current_revisions(
                    value,
                    expected_workspace_id="ws-1",
                    asset_id="asset-export-current-revisions",
                    asset_sha256=expected["export_asset_sha256"],
                )
            else:
                raise AssertionError(f"unknown publication negative validator: {validator}")

        expected_code = ErrorCode.STALE_LEASE if validator == "context_stale_epoch" else None
        _expect_failure(action, expected_code)
        executed.append(case["case_id"])

    from plotpilot_plugin_sdk.ports import CoreAuthorityPort

    forbidden = {name for name in dir(CoreAuthorityPort) if "publication" in name.lower() or name.lower() in {"accept", "publish"}}
    if forbidden or any("publication" in method for method in (*EXPECTED_WORKER_METHODS, *EXPECTED_HOST_METHODS)):
        raise AssertionError(f"plugin SDK/RPC exposed direct Publication authority: {sorted(forbidden)}")
    return {
        "authority_payloads": len(http["authority_payloads"]),
        "http_exchanges": len(http["exchanges"]),
        "http_matrix_routes": len(matrix["routes"]),
        "context_profiles": sorted(identities),
        "python_negative_cases": executed,
        "export_items": len(export_value["ordered_revisions"]),
        "plugin_publication_callable": False,
    }


def verify_typescript_contract_publication() -> dict[str, Any]:
    """Execute the real P0 TypeScript ingress and every additive corpus case."""

    script = r'''
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const core = await import(pathToFileURL(process.env.PLOTPILOT_TS_CORE_API).href)
const ingress = await import(pathToFileURL(process.env.PLOTPILOT_TS_INGRESS).href)
const verifier = await import(pathToFileURL(process.env.PLOTPILOT_TS_VERIFIER).href)
const readJson = path => JSON.parse(readFileSync(path, 'utf8'))
const goldenDir = process.env.PLOTPILOT_PUBLICATION_GOLDEN
const http = readJson(resolve(goldenDir, 'core-http.json'))
const context = readJson(resolve(goldenDir, 'context-identity.json'))
const expected = readJson(resolve(goldenDir, 'expected.json'))
const exportValue = readJson(resolve(goldenDir, 'export-current-revisions.json'))
const exportRaw = new Uint8Array(readFileSync(resolve(goldenDir, 'export-current-revisions.json')))
const exportSnapshot = readJson(resolve(goldenDir, 'export-run-snapshot.json'))
const ui = readJson(resolve(goldenDir, 'ui-ingress.json'))
const corpus = readJson(process.env.PLOTPILOT_PUBLICATION_CORPUS)

const fixtureValue = reference => {
  const [rawPath, fragment = ''] = reference.split('#', 2)
  const path = rawPath.startsWith('contracts/') ? resolve(process.cwd(), rawPath) : resolve(goldenDir, rawPath)
  let value = readJson(path)
  for (const token of fragment.replace(/^\//, '').split('/').filter(Boolean)) {
    value = Array.isArray(value) ? value[Number(token)] : value[token]
  }
  return structuredClone(value)
}
const mutate = (value, mutation) => {
  if (mutation.op === 'noop') return structuredClone(value)
  if (mutation.op === 'raw_variant') {
    const raw = new TextEncoder().encode(JSON.stringify(value))
    if (mutation.variant === 'bom') return new Uint8Array([0xef, 0xbb, 0xbf, ...raw])
    if (mutation.variant === 'whitespace') return new TextEncoder().encode(` ${new TextDecoder().decode(raw)}`)
    if (mutation.variant === 'duplicate_key') {
      const text = new TextDecoder().decode(raw)
      return new TextEncoder().encode(text.replace('{"core_snapshot_revision":', '{"schema":"export-current-revisions/v1","core_snapshot_revision":'))
    }
    throw new Error(`unsupported raw variant ${JSON.stringify(mutation)}`)
  }
  if (mutation.op !== 'set' && mutation.op !== 'delete') throw new Error(`unsupported mutation ${JSON.stringify(mutation)}`)
  const output = structuredClone(value)
  let target = output
  for (const token of mutation.path.slice(0, -1)) target = target[token]
  if (mutation.op === 'delete') delete target[mutation.path.at(-1)]
  else target[mutation.path.at(-1)] = structuredClone(mutation.value)
  return output
}
const rejected = async action => {
  try { await action(); return false } catch (_) { return true }
}
const deepFrozen = (value, seen = new WeakSet()) => {
  if (value === null || typeof value !== 'object') return true
  if (seen.has(value)) return true
  seen.add(value)
  if (!Object.isFrozen(value)) return false
  return Reflect.ownKeys(value).every(key => deepFrozen(value[key], seen))
}

for (const value of http.authority_payloads) core.parseCoreAuthorityCommandQueryV1(value, { expectedWorkspaceId: 'ws-1' })
const publicationCommand = core.parsePublicationCommandResultV1(http.publication.command, { expectedWorkspaceId: 'ws-1' })
core.parsePublicationCommandResultV1(http.publication.result, { expectedWorkspaceId: 'ws-1', command: publicationCommand })
for (const value of Object.values(http.assets)) core.parseAssetMetadataV1(value)
core.verifyAssetMetadataRangePair(http.assets.metadata, http.assets.range)

const fake = new core.CoreHttpContractFake()
for (const exchange of http.exchanges) {
  fake.add(exchange.route_id, exchange.request, exchange.status, exchange.response)
  const [status, response] = fake.request(exchange.route_id, exchange.request)
  if (status !== exchange.status || JSON.stringify(response) !== JSON.stringify(exchange.response)) throw new Error(`Core HTTP fixture drift ${exchange.route_id}`)
}
for (const exchange of http.exchanges.filter(item => item.status >= 400)) {
  const [status, response] = fake.request(exchange.route_id, exchange.request)
  if (status !== exchange.status || JSON.stringify(response) !== JSON.stringify(exchange.response)) throw new Error(`Core HTTP failure exchange drift ${exchange.route_id}`)
  const expectedCode = exchange.route_id === 'publication.accept'
    ? 'incomplete_publication'
    : exchange.request.workspace_id === 'ws-other'
      ? 'cross_workspace'
      : exchange.request.document_id === 'doc-unknown'
        ? 'unknown_reference'
        : exchange.request.expected_revision === 99
          ? 'stale_cas'
          : 'operation_key_reuse'
  if (response.error_code !== expectedCode) throw new Error(`Core HTTP error code drift ${exchange.route_id}`)
}

const identities = {}
for (const vector of context.vectors) {
  const expectedEpoch = vector.profile === 'control' ? undefined : vector.expected_lease_epoch
  const projection = verifier.operationContextIdentityProjection(vector.meta, expectedEpoch)
  core.parseOperationContextIdentityV1(projection)
  const identity = await verifier.deriveOperationContextIdentity(vector.meta, expectedEpoch)
  if (identity !== vector.context_identity) throw new Error(`context parity drift ${vector.profile}`)
  identities[vector.profile] = identity
}
if (Object.keys(expected.context_identities).some(profile => identities[profile] !== expected.context_identities[profile])) throw new Error('context identity inventory drift')

await core.verifyExportCurrentRevisionsAssetV1(exportRaw, exportSnapshot, 'asset-export-current-revisions')

const parsedTree = ingress.parsePluginUiTreeV1(ui.tree)
await ingress.parseJobSnapshotV1(ui.job_snapshot)
ingress.parsePluginUiIntentV1(ui.intent)
ingress.parsePluginUiAckV1(ui.ack)
const originalLabel = parsedTree.root.children[0].props.label
ui.tree.root.children[0].props.label = 'mutated after parse'
if (parsedTree.root.children[0].props.label !== originalLabel || !deepFrozen(parsedTree)) throw new Error('UI ingress is not an independent deep-frozen value')

const executed = []
for (const testCase of corpus.cases) {
  const validator = testCase.validator
  let action
  if (validator === 'typescript_runtime_probe') {
    if (testCase.mutation.op === 'prototype') {
      action = () => ingress.parsePluginUiIntentV1(Object.assign(Object.create({ inherited: true }), ui.intent))
    } else if (testCase.mutation.op === 'getter') {
      action = () => {
        const value = { ...ui.intent }
        Object.defineProperty(value, 'intent_id', { enumerable: true, get() { return 'intent-getter' } })
        return ingress.parsePluginUiIntentV1(value)
      }
    } else throw new Error(`unknown runtime probe ${testCase.case_id}`)
  } else {
    const value = mutate(fixtureValue(testCase.fixture), testCase.mutation)
    if (validator === 'core_authority') action = () => core.parseCoreAuthorityCommandQueryV1(value)
    else if (validator === 'core_authority_expected_workspace') action = () => core.parseCoreAuthorityCommandQueryV1(value, { expectedWorkspaceId: 'ws-1' })
    else if (validator === 'publication') action = () => core.parsePublicationCommandResultV1(value)
    else if (validator === 'publication_expected_workspace') action = () => core.parsePublicationCommandResultV1(value, { expectedWorkspaceId: 'ws-1' })
    else if (validator === 'publication_command_binding') action = () => core.parsePublicationCommandResultV1(value, { command: http.publication.command })
    else if (validator === 'asset') action = () => core.parseAssetMetadataV1(value)
    else if (validator === 'asset_pair') action = () => core.verifyAssetMetadataRangePair(http.assets.metadata, value)
    else if (validator === 'asset_pair_metadata') action = () => core.verifyAssetMetadataRangePair(value, http.assets.range)
    else if (validator === 'context_projection_schema') action = () => core.parseOperationContextIdentityV1(value)
    else if (validator === 'context_stale_epoch') action = () => verifier.deriveOperationContextIdentity(value, 9)
    else if (validator === 'context_missing_expected_epoch') action = () => verifier.deriveOperationContextIdentity(value)
    else if (validator === 'context_identity') action = () => verifier.deriveOperationContextIdentity(value)
    else if (validator === 'export') action = () => core.parseExportCurrentRevisionsV1(value)
    else if (validator === 'export_expected_workspace') action = () => core.parseExportCurrentRevisionsV1(value, { expectedWorkspaceId: 'ws-1' })
    else if (validator === 'export_snapshot_binding') action = () => core.verifyExportCurrentRevisionsAssetV1(new TextEncoder().encode(JSON.stringify(value)), exportSnapshot, 'asset-export-current-revisions')
    else if (validator === 'export_snapshot_record') action = () => core.verifyExportCurrentRevisionsAssetV1(exportRaw, value, 'asset-export-current-revisions')
    else if (validator === 'export_raw_variant') action = () => core.verifyExportCurrentRevisionsAssetV1(value instanceof Uint8Array ? value : new TextEncoder().encode(JSON.stringify(value)), exportSnapshot, 'asset-export-current-revisions')
    else if (validator === 'export_object_caller_hash') action = () => core.parseExportCurrentRevisionsV1(value, { expectedWorkspaceId: 'ws-1', exportAssetId: 'asset-export-current-revisions', exportAssetSha256: expected.export_asset_sha256 })
    else if (validator === 'http_fixture_request') action = () => fake.request(testCase.route_id, value)
    else if (validator === 'typescript_plugin_ui_tree') action = () => ingress.parsePluginUiTreeV1(value)
    else if (validator === 'typescript_plugin_ui_intent') action = () => ingress.parsePluginUiIntentV1(value)
    else if (validator === 'typescript_job_snapshot') action = () => ingress.parseJobSnapshotV1(value)
    else throw new Error(`unknown TypeScript publication validator ${validator}`)
  }
  if (!(await rejected(action))) throw new Error(`TypeScript false-accepted ${testCase.case_id}`)
  executed.push(testCase.case_id)
}

console.log(JSON.stringify({
  status: 'ok',
  authority_payloads: http.authority_payloads.length,
  http_exchanges: http.exchanges.length,
  context_profiles: Object.keys(identities).sort(),
  negative_cases: executed,
  ui_deep_frozen: true,
}))
'''
    environment = os.environ.copy()
    environment.update(
        {
            "NODE_NO_WARNINGS": "1",
            "PLOTPILOT_TS_CORE_API": str(ROOT / "frontend" / "src" / "contracts" / "core-api.ts"),
            "PLOTPILOT_TS_INGRESS": str(ROOT / "frontend" / "src" / "contracts" / "ingress.ts"),
            "PLOTPILOT_TS_VERIFIER": str(ROOT / "frontend" / "src" / "contracts" / "verifier.ts"),
            "PLOTPILOT_PUBLICATION_GOLDEN": str(GOLDEN_DIR / "contract-publication-v1"),
            "PLOTPILOT_PUBLICATION_CORPUS": str(CORPUS_DIR / "contract-publication-v1" / "negative.json"),
        }
    )
    completed = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(f"TypeScript contract-publication runtime failed:\n{completed.stdout}\n{completed.stderr}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"TypeScript contract-publication gate emitted invalid JSON: {completed.stdout!r}") from exc
    expected_case_ids = [case["case_id"] for case in load_strict_json(CORPUS_DIR / "contract-publication-v1" / "negative.json")["cases"]]
    if result.get("status") != "ok" or result.get("negative_cases") != expected_case_ids or result.get("ui_deep_frozen") is not True:
        raise AssertionError(f"unexpected TypeScript contract-publication result: {result}")
    return result


def verify_typescript_verifier() -> dict[str, Any]:
    """Run the actual frontend TypeScript verifier under Node's TS loader."""
    script = r'''
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const verifier = await import(pathToFileURL(process.env.PLOTPILOT_TS_VERIFIER).href)
const readJson = (path) => JSON.parse(readFileSync(path, 'utf8'))
const snapshot = readJson(process.env.PLOTPILOT_TS_SNAPSHOT)
const bundle = readJson(process.env.PLOTPILOT_TS_BUNDLE)
const backup = readJson(process.env.PLOTPILOT_TS_BACKUP)
await verifier.verifySnapshot(snapshot)
verifier.verifyResultProfile(bundle, snapshot.workspace_id)
await verifier.verifyBackup(backup)

const data = readJson(process.env.PLOTPILOT_TS_DATA_BUNDLE)
await verifier.verifyDataBundle(data)

const core = readJson(process.env.PLOTPILOT_TS_CORE_SNAPSHOT)
await verifier.verifyCoreSnapshot(core)

const validFiles = { 'straße.txt': new TextEncoder().encode('x') }
const manifest = await verifier.buildFilesSha256(validFiles)
await verifier.verifyPackageManifest(validFiles, manifest)
let collisionRejected = false
try {
  await verifier.buildFilesSha256({ 'straße.txt': new TextEncoder().encode('x'), 'strasse.txt': new TextEncoder().encode('y') })
} catch (_) {
  collisionRejected = true
}
if (!collisionRejected) throw new Error('TS verifier accepted NFC/casefold collision')
console.log(JSON.stringify({ status: 'ok', workspace_id: snapshot.workspace_id, collision_rejected: collisionRejected }))
'''
    environment = os.environ.copy()
    environment.update(
        {
            "NODE_NO_WARNINGS": "1",
            "PLOTPILOT_TS_VERIFIER": str(ROOT / "frontend" / "src" / "contracts" / "verifier.ts"),
            "PLOTPILOT_TS_SNAPSHOT": str(GOLDEN_DIR / "run-snapshot" / "snapshot.json"),
            "PLOTPILOT_TS_BUNDLE": str(EXAMPLES_DIR / "result-bundle.json"),
            "PLOTPILOT_TS_BACKUP": str(GOLDEN_DIR / "backup" / "backup.json"),
            "PLOTPILOT_TS_DATA_BUNDLE": str(FIXTURES_DIR / "plugin-data-bundle.json"),
            "PLOTPILOT_TS_CORE_SNAPSHOT": str(FIXTURES_DIR / "core-snapshot.json"),
        }
    )
    completed = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(f"TypeScript verifier runtime failed:\n{completed.stdout}\n{completed.stderr}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"TypeScript verifier did not emit JSON: {completed.stdout!r}\n{completed.stderr}") from exc
    if result.get("status") != "ok" or result.get("collision_rejected") is not True:
        raise AssertionError(f"unexpected TypeScript verifier result: {result}")
    return result


def verify_all() -> dict[str, Any]:
    result = {
        "manifest": verify_contract_manifest(),
        "schemas": verify_schemas(),
        "v2_public_surface": verify_v2_public_surface(),
        "goldens": verify_goldens(),
        "positive": verify_positive_fixtures(),
        "corpus": verify_corpus(),
        "negative": verify_negative_groups(),
        "negative_cases": verify_negative_cases(),
        "remediation": verify_remediation_probes(),
        "contract_publication": verify_contract_publication(),
        "contract_publication_typescript": verify_typescript_contract_publication(),
        "core_http_request_failure": verify_core_http_request_failure_contract(),
        "typescript": verify_typescript_verifier(),
        "rpc_methods": {"worker": list(EXPECTED_WORKER_METHODS), "host": list(EXPECTED_HOST_METHODS), "error_codes": EXPECTED_ERROR_CODES},
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="run schemas, fixtures, goldens and all 14 negative groups")
    parser.add_argument("--json", action="store_true", help="emit only the JSON summary")
    args = parser.parse_args()
    if not args.all:
        parser.error("M0 verification is explicit: pass --all")
    result = verify_all()
    # The documented M0 command is also run from a stock Chinese Windows
    # console (CP936).  Keep the CLI transport ASCII-only so a successful
    # verification cannot fail while rendering a non-CP936 code point from a
    # fixture (for example U+00DF in the Unicode path corpus).  JSON parsers
    # recover the original strings from the escapes, while the evidence file
    # remains the exact stdout bytes emitted by this command.
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
