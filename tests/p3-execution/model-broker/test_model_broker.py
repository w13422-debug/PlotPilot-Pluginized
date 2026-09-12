from __future__ import annotations

import copy
import hashlib
import sqlite3
import sys
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
PROMPT_SKILL_SRC = ROOT / "first-party-plugins" / "prompt-skill-runtime" / "src"
if str(PROMPT_SKILL_SRC) not in sys.path:
    sys.path.insert(0, str(PROMPT_SKILL_SRC))

from backend import plotpilot_plugin_sdk as _sdk_package

sys.modules.setdefault("plotpilot_plugin_sdk", _sdk_package)

from plotpilot_prompt_skill_runtime import decode_model_receipt

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.bootstrap.host_provider_adapter import (
    HostProviderAdapter,
    ProviderFactoryBinding,
    ProviderFactoryRequest,
)
from backend.plotpilot_core.configuration import (
    ModelConfigurationAuthority,
    WorkspacePlanAuthority,
)
from backend.plotpilot_core.domain import Workspace
from backend.plotpilot_core.model import (
    ModelBroker,
    ProviderInvocationCommand,
    ProviderInvocationExchange,
    ProviderInvocationPreparation,
)
from backend.plotpilot_core.plugins.generation import validate_generation
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk import (
    ContractError,
    ErrorCode,
    canonical_bytes,
    hash_jcs,
    parse_json_bytes,
)
from backend.plotpilot_plugin_sdk.model_provider_rpc_v2 import (
    build_model_provider_invoke_request_v2,
    build_model_provider_rpc_error_v2,
)
from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash

NOW = "2030-01-02T03:04:05Z"
WORKSPACE_ID = "workspace-1"
GENERATION_ID = "generation-planning-1"
CALLER_PLUGIN_ID = "com.plotpilot.prompt-skill-runtime"
CALLER_RELEASE_ID = "c" * 64
CALLER_PACKAGE_HASH = "d" * 64
PROVIDER_PLUGIN_ID = "com.plotpilot.provider.local"
PROVIDER_RELEASE_ID = "a" * 64
PROVIDER_PACKAGE_HASH = "e" * 64
PLAN_ID = "plan-revision-1"
PLAN_HASH = "b" * 64
RAW_VALUE = "RAW_MODEL_VALUE_8e73c9f1"


def _member(plugin_id: str, release_id: str, package_hash: str) -> dict[str, Any]:
    return {
        "plugin_id": plugin_id,
        "release_id": release_id,
        "package_hash": package_hash,
        "data_generation_id": None,
        "ui_bundle_hash": None,
        "global_settings_revision_id": None,
        "settings_schema_hash": None,
        "data_bundle_asset_id": None,
    }


def _generation(generation_id: str = GENERATION_ID) -> dict[str, Any]:
    return validate_generation(
        {
            "schema": "plugin-generation/v1",
            "generation_id": generation_id,
            "core_api_version": "1.2.0",
            "members": sorted(
                (
                    _member(CALLER_PLUGIN_ID, CALLER_RELEASE_ID, CALLER_PACKAGE_HASH),
                    _member(PROVIDER_PLUGIN_ID, PROVIDER_RELEASE_ID, PROVIDER_PACKAGE_HASH),
                ),
                key=lambda item: item["plugin_id"].encode(),
            ),
            "created_reason": "P2A deterministic fake",
            "created_at": NOW,
            "health_result_asset_id": f"health-{generation_id}",
            "parent_generation_id": None,
            "base_generation_id": None,
        }
    )


def _install_generation(
    repository: CoreAuthorityRepository, generation: Mapping[str, Any]
) -> None:
    payload = canonical_bytes(generation)
    with repository.transaction() as connection:
        connection.execute(
            "INSERT INTO p2_plugin_generation(generation_id,payload_json,payload_hash) "
            "VALUES(?,?,?)",
            (
                generation["generation_id"],
                payload.decode(),
                hashlib.sha256(payload).hexdigest(),
            ),
        )
        connection.execute(
            "UPDATE p2_plugin_generation_pointer SET current_generation_id=?,"
            "safe_mode=0,safe_mode_reason=NULL,revision=revision+1 WHERE singleton=1",
            (generation["generation_id"],),
        )


def _configure(
    repository: CoreAuthorityRepository,
    *,
    raw_value: str = RAW_VALUE,
) -> tuple[ModelConfigurationAuthority, WorkspacePlanAuthority, dict[str, Any]]:
    configuration = ModelConfigurationAuthority(
        repository,
        clock=lambda: NOW,
        revision_id_factory=lambda: "model-profile-revision-1",
    )
    selection_ids = iter(f"selection-{index}" for index in range(1, 100))
    planning = WorkspacePlanAuthority(
        repository,
        clock=lambda: NOW,
        selection_id_factory=lambda: next(selection_ids),
    )
    configuration.put_secret(
        {
            "schema": "model-secret-put-command/v2",
            "operation_key": "secret-operation-1",
            "secret_id": "provider-main",
            "value": raw_value,
        }
    )
    profile = configuration.revise_profile(
        {
            "schema": "model-profile-revise-command/v2",
            "operation_key": "profile-operation-1",
            "profile_id": "model-profile-1",
            "expected_parent_revision_id": None,
            "provider": {
                "plugin_id": PROVIDER_PLUGIN_ID,
                "release_id": PROVIDER_RELEASE_ID,
                "endpoint": "https://models.example.test/v1",
                "model_name": "planner-model-1",
                "options": {
                    "temperature": 0.4,
                    "top_p": 0.9,
                    "max_output_tokens": 4096,
                    "timeout_seconds": 120,
                    "max_retries": 0,
                },
                "api_key_ref": "secret://provider-main",
            },
        }
    )["revision"]
    planning.register_verified_plan_reference(
        generation_id=GENERATION_ID,
        plan_revision_id=PLAN_ID,
        plan_revision_hash=PLAN_HASH,
    )
    planning.select_workspace_plan(
        {
            "schema": "workspace-plan-selection-command/v2",
            "operation_key": "plan-operation-1",
            "workspace_id": WORKSPACE_ID,
            "expected_workspace_revision": 0,
            "expected_current_plan_revision_id": None,
            "expected_current_plan_revision_hash": None,
            "selection_mode": "explicit",
            "plan_revision_id": PLAN_ID,
            "plan_revision_hash": PLAN_HASH,
            "model_profile_revision_id": profile["revision_id"],
            "model_profile_revision_hash": profile["revision_hash"],
            "expected_active_generation_id": GENERATION_ID,
        }
    )
    return configuration, planning, profile


@dataclass
class BrokerStack:
    root: Path
    repository: CoreAuthorityRepository
    assets: AssetStore
    execution: ExecutionAuthority
    configuration: ModelConfigurationAuthority
    planning: WorkspacePlanAuthority
    profile: dict[str, Any]
    snapshot: dict[str, Any]
    snapshot_asset_id: str
    chain_asset_id: str
    input_asset_id: str
    request_asset_id: str

    def close(self) -> None:
        self.repository.close()


def _build_authority(
    tmp_path: Path,
    suffix: str = "base",
    *,
    raw_value: str = RAW_VALUE,
) -> BrokerStack:
    root = tmp_path / suffix
    root.mkdir(parents=True, exist_ok=True)
    repository = CoreAuthorityRepository(root / "core.db")
    assets = AssetStore(root / "assets")
    repository.create_workspace(Workspace(WORKSPACE_ID, "Novel"))
    generation = _generation()
    _install_generation(repository, generation)
    configuration, planning, profile = _configure(
        repository, raw_value=raw_value
    )

    chain = assets.put(
        canonical_bytes({"schema": "test-chain/v1", "chain_id": "chain-1"}),
        mime="application/json",
        logical_role="skill_chain",
        provenance="test:p2a",
    )
    input_asset = assets.put(
        canonical_bytes({"schema": "test-input/v1", "text": "Plan the novel."}),
        mime="application/json",
        logical_role="prompt_input",
        provenance="test:p2a",
    )
    model_request = assets.put(
        canonical_bytes(
            {
                "schema": "model-provider-request-asset/v1",
                "profile_revision_id": profile["revision_id"],
                "messages": [{"role": "user", "content": "Create the macro plan."}],
                "options": {"stream": False, "temperature": 0.0},
            }
        ),
        mime="application/json",
        logical_role="model_request",
        provenance="test:p2a",
    )
    snapshot: dict[str, Any] = {
        "schema": "run-snapshot/v1",
        "snapshot_id": "run-snapshot-1",
        "core_contract_version": "1.2.0",
        "workspace_id": WORKSPACE_ID,
        "scope": {"document_id": None, "node_id": None, "operation": "prompt.skill.execute/v2"},
        "input_revisions": [],
        "plan_revision_id": PLAN_ID,
        "plugin_releases": [
            {
                "plugin_id": CALLER_PLUGIN_ID,
                "release_id": CALLER_RELEASE_ID,
                "package_hash": CALLER_PACKAGE_HASH,
                "data_generation_id": None,
            }
        ],
        "plugin_settings_revisions": [],
        "data_bindings": [],
        "skill_releases": [],
        "model_profile_revision_id": profile["revision_id"],
        "parameters_asset_id": None,
        "asset_hashes": sorted(
            (
                {"asset_id": chain.asset_id, "sha256": chain.sha256},
                {"asset_id": input_asset.asset_id, "sha256": input_asset.sha256},
                {"asset_id": model_request.asset_id, "sha256": model_request.sha256},
            ),
            key=lambda item: item["asset_id"].encode(),
        ),
        "request_key": "0" * 64,
        "run_intent_id": "run-intent-1",
        "created_at": NOW,
        "snapshot_hash": "0" * 64,
    }
    snapshot["request_key"] = request_key(snapshot)
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)
    snapshot_asset = assets.put(
        canonical_bytes(snapshot),
        mime="application/json",
        logical_role="run_snapshot",
        provenance="test:p2a",
    )
    execution = ExecutionAuthority(repository, assets)
    execution.create_from_verified_snapshot("job-1", snapshot)
    with repository.transaction() as connection:
        connection.execute(
            "UPDATE execution_job SET run_snapshot_asset_id=? WHERE job_id='job-1'",
            (snapshot_asset.asset_id,),
        )
    execution.start_attempt(
        job_id="job-1", step_id="step-1", attempt_id="attempt-1",
        worker_run_id="worker-1", plugin_id=CALLER_PLUGIN_ID,
        release_id=CALLER_RELEASE_ID, package_hash=CALLER_PACKAGE_HASH,
        capability_id="prompt.skill.execute/v2", generation_id=GENERATION_ID,
        lease_epoch=1, expected_result_contract="artifact-bundle/v1",
    )
    return BrokerStack(
        root, repository, assets, execution, configuration, planning, profile,
        snapshot, snapshot_asset.asset_id, chain.asset_id, input_asset.asset_id,
        model_request.asset_id,
    )


class FakeProviderPort:
    def __init__(
        self,
        stack: BrokerStack,
        *,
        state: str = "receipted",
        mode: str = "success",
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
        response_bytes_override: bytes | None = None,
    ) -> None:
        self.stack = stack
        self.state = state
        self.mode = mode
        self.entered = entered
        self.release = release
        self.response_bytes_override = (
            None if response_bytes_override is None else bytes(response_bytes_override)
        )
        self.prepare_calls = 0
        self.invoke_calls = 0
        self.transaction_states: list[bool] = []
        self.resolved_value_digest: str | None = None

    def prepare(self, command: ProviderInvocationCommand) -> ProviderInvocationPreparation:
        self.prepare_calls += 1
        context = {
            "operation_key": command.operation_key,
            "workspace_id": command.workspace_id,
            "plugin_id": command.caller_plugin_id,
            "plugin_release_id": command.caller_plugin_release_id,
            "plugin_package_hash": command.caller_plugin_package_hash,
            "generation_id": command.generation_id,
            "job_id": command.job_id,
            "step_id": command.step_id,
            "attempt_id": command.attempt_id,
            "lease_epoch": command.lease_epoch,
            "chain_id": "chain-1",
            "chain_asset_id": command.run_snapshot["asset_hashes"][0]["asset_id"],
            "chain_content_hash": command.run_snapshot["asset_hashes"][0]["sha256"],
            "run_snapshot_id": command.run_snapshot_id,
            "run_snapshot_asset_id": command.run_snapshot_asset_id,
            "run_snapshot_hash": command.run_snapshot_hash,
            "model_profile_revision_id": command.model_profile_revision["revision_id"],
            "input_asset_id": command.run_snapshot["asset_hashes"][1]["asset_id"],
            "input_content_hash": command.run_snapshot["asset_hashes"][1]["sha256"],
        }
        # Asset hashes are sorted, so select by the known IDs instead of relying on order.
        hashes = {
            item["asset_id"]: item["sha256"]
            for item in command.run_snapshot["asset_hashes"]
        }
        context.update(
            chain_asset_id=self.stack.chain_asset_id,
            chain_content_hash=hashes[self.stack.chain_asset_id],
            input_asset_id=self.stack.input_asset_id,
            input_content_hash=hashes[self.stack.input_asset_id],
        )
        if self.mode == "prepare_drift":
            context["attempt_id"] = "attempt-other"
        request = build_model_provider_invoke_request_v2(
            context,
            command.model_request_asset,
            request_id="123e4567-e89b-42d3-a456-426614174100",
        )
        return ProviderInvocationPreparation(
            request,
            opaque={
                "invocation_id": command.invocation_id,
                "invocation_key": command.invocation_key,
            },
        )

    def invoke(
        self,
        preparation: ProviderInvocationPreparation,
        *,
        api_key: str,
    ) -> ProviderInvocationExchange:
        self.invoke_calls += 1
        self.transaction_states.append(self.stack.repository._connection.in_transaction)
        self.resolved_value_digest = hashlib.sha256(api_key.encode()).hexdigest()
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(timeout=5)
        if self.mode == "raise":
            raise RuntimeError(f"transport hid {len(api_key)} bytes")
        request = dict(preparation.provider_request)
        if self.mode in {"rpc_error", "rpc_error_reflection"}:
            return ProviderInvocationExchange(
                build_model_provider_rpc_error_v2(
                    request["id"],
                    code=int(ErrorCode.INCOMPATIBLE_GENERATION),
                    message=(
                        f"Provider reflected {api_key}"
                        if self.mode == "rpc_error_reflection"
                        else "Provider adapter unavailable"
                    ),
                    retryable=False,
                ),
                None,
                None,
            )

        context = request["params"]["planner_context"]
        response_bytes = None
        response_asset = None
        response_hash = None
        if self.state == "receipted":
            response_text = (
                f"A reflected {api_key} value."
                if self.mode == "response_reflection"
                else "A macro plan."
            )
            response_value: dict[str, Any] = {
                "schema": "model-provider-response-asset/v1",
                "text": response_text,
            }
            if self.mode == "response_chunk_reflection":
                response_value["chunks"] = [
                    {"seq": 1, "delta": f"reflected:{api_key}"}
                ]
            elif self.mode == "response_nested_key_reflection":
                response_value["metadata"] = {
                    f"provider:{api_key}": "reflected key"
                }
            elif self.mode == "response_nested_value_reflection":
                response_value["metadata"] = {
                    "provider": {"value": f"reflected:{api_key}"}
                }
            elif self.mode == "response_schema_reflection":
                response_value["schema"] = f"reflected:{api_key}"
            response_bytes = (
                self.response_bytes_override
                if self.response_bytes_override is not None
                else canonical_bytes(response_value)
            )
            response_digest = hashlib.sha256(response_bytes).hexdigest()
            response_asset = {
                "asset_id": f"asset-sha256-{response_digest}",
                "content_hash": response_digest,
            }
            response_hash = hashlib.sha256(b"provider-wire-response").hexdigest()
        elif self.state == "failed":
            response_hash = hashlib.sha256(b"provider-wire-error").hexdigest()
        elif self.state == "uncertain":
            response_hash = hashlib.sha256(b"provider-wire-unknown").hexdigest()
        request_hash = hashlib.sha256(b"provider-wire-request").hexdigest()
        receipt_context = {
            **context,
            "input_hash": context["input_content_hash"],
        }
        del receipt_context["input_content_hash"]
        profile_provider = self.stack.profile["provider"]
        outer = preparation.opaque
        receipt_profile: dict[str, Any] = {
            "profile_revision_id": self.stack.profile["revision_id"],
            "provider_plugin_id": PROVIDER_PLUGIN_ID,
            "provider_release_id": PROVIDER_RELEASE_ID,
            "endpoint": profile_provider["endpoint"],
            "model_name": profile_provider["model_name"],
            "api_key_ref": profile_provider["api_key_ref"],
        }
        if self.mode == "receipt_input_context_reflection":
            receipt_context["provider_note"] = {"value": f"reflected:{api_key}"}
        if self.mode == "receipt_profile_reflection":
            receipt_profile["provider_note"] = {"value": f"reflected:{api_key}"}
        without_hash: dict[str, Any] = {
            "schema": "model-receipt/v1",
            "receipt_id": f"logical-receipt-{self.state}-{outer['invocation_id']}",
            "invocation_id": outer["invocation_id"],
            "invocation_key": outer["invocation_key"],
            "state": self.state,
            "request_hash": request_hash,
            "response_hash": response_hash,
            "profile_revision_id": self.stack.profile["revision_id"],
            "provider_plugin_id": PROVIDER_PLUGIN_ID,
            "provider_release_id": PROVIDER_RELEASE_ID,
            "endpoint": profile_provider["endpoint"],
            "model": profile_provider["model_name"],
            "lifecycle": ["prepared", "sent", self.state],
            "prompt_tokens": 11 if self.state == "receipted" else None,
            "completion_tokens": 7 if self.state == "receipted" else None,
            "total_tokens": 18 if self.state == "receipted" else None,
            "cost": (
                f"reflected:{api_key}"
                if self.mode == "receipt_cost_reflection"
                else "0.0025" if self.state == "receipted" else None
            ),
            "retry_count": 0,
            "stream_termination": {
                "receipted": "response",
                "failed": "http_error",
                "cancelled": "cancelled",
                "uncertain": "transport_unknown",
            }[self.state],
            "error": None if self.state == "receipted" else f"provider {self.state}",
            "input_context": receipt_context,
            "profile_revision": receipt_profile,
            "metadata": {"fixture": "p2a", "terminal_state": self.state},
            "response_asset_id": None if response_asset is None else response_asset["asset_id"],
            "recovered": False,
            "uncertain": self.state == "uncertain",
        }
        if self.mode == "receipt_metadata_reflection":
            without_hash["metadata"] = {
                "fixture": "p2a",
                "nested": {"note": f"reflected:{api_key}:value"},
            }
        elif self.mode == "receipt_metadata_key_reflection":
            without_hash["metadata"] = {
                "fixture": "p2a",
                "nested": {f"note:{api_key}": "reflected key"},
            }
        elif self.mode == "receipt_error_reflection":
            without_hash["error"] = f"provider reflected {api_key}"
        elif self.mode == "receipt_response_id_reflection":
            without_hash["metadata"] = {
                "fixture": "p2a",
                "response_id": f"response:{api_key}",
            }
        receipt = {
            **without_hash,
            "receipt_hash": hash_jcs("model-receipt/v1", without_hash),
        }
        if self.mode == "receipt_missing":
            receipt.pop("metadata")
            receipt["receipt_hash"] = hash_jcs(
                "model-receipt/v1",
                {key: value for key, value in receipt.items() if key != "receipt_hash"},
            )
        elif self.mode == "receipt_extra":
            receipt["raw_secret"] = "forbidden"
            receipt["receipt_hash"] = hash_jcs(
                "model-receipt/v1",
                {key: value for key, value in receipt.items() if key != "receipt_hash"},
            )
        elif self.mode == "receipt_type":
            receipt["retry_count"] = "0"
            receipt["receipt_hash"] = hash_jcs(
                "model-receipt/v1",
                {key: value for key, value in receipt.items() if key != "receipt_hash"},
            )
        elif self.mode == "receipt_self_hash":
            receipt["receipt_hash"] = "0" * 64
        receipt_bytes = canonical_bytes(receipt)
        if self.mode == "leak":
            receipt_bytes += api_key.encode()
        receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
        anchor = {
            "receipt_id": receipt["receipt_id"],
            "asset_id": f"asset-sha256-{receipt_digest}",
            "content_hash": receipt_digest,
            "receipt_hash": receipt["receipt_hash"],
            **context,
        }
        if self.mode == "anchor_drift":
            anchor["chain_id"] = "chain-other"
        result = {
            "schema": "model-provider-invoke-result/v2",
            "model_receipt_anchor": anchor,
            "model_request_asset": dict(request["params"]["model_request_asset"]),
            "provider_transport_request_hash": request_hash,
            "response_asset": response_asset,
            "provider_transport_response_hash": response_hash,
            "provider_terminal_state": self.state,
        }
        if self.mode == "hash_substitution":
            result["provider_transport_request_hash"] = response_asset["content_hash"]
        return ProviderInvocationExchange(
            {"jsonrpc": "2.0", "id": request["id"], "result": result},
            receipt_bytes,
            response_bytes,
        )


def _receipt_verifier(stack: BrokerStack):
    def verify(anchor: Mapping[str, Any]) -> Any:
        raw = stack.assets.read(str(anchor["asset_id"]))
        receipt = decode_model_receipt(parse_json_bytes(raw))
        return SimpleNamespace(
            receipt=receipt,
            asset_id=anchor["asset_id"],
            asset_hash=anchor["content_hash"],
        )

    return verify


class FakeFactoryPort:
    def __init__(
        self,
        stack: BrokerStack,
        port: FakeProviderPort,
        *,
        mode: str = "exact",
    ) -> None:
        self.stack = stack
        self.port = port
        self.mode = mode
        self.bind_calls = 0
        self.factory_calls = 0
        self.requests: list[ProviderFactoryRequest] = []
        self.transports: list[Any] = []

    def bind(
        self, request: ProviderFactoryRequest
    ) -> ProviderFactoryBinding | None:
        self.bind_calls += 1
        self.requests.append(request)
        if self.mode == "missing":
            return None

        def factory(transport: Any) -> FakeProviderPort:
            self.factory_calls += 1
            self.transports.append(transport)
            return self.port

        values = {
            name: getattr(request, name)
            for name in (
                "generation_id",
                "provider_plugin_id",
                "provider_release_id",
                "provider_package_hash",
                "verifier_plugin_id",
                "verifier_release_id",
                "verifier_package_hash",
            )
        }
        if self.mode == "provider_package_drift":
            values["provider_package_hash"] = "0" * 64
        verifier = None if self.mode == "missing_verifier" else _receipt_verifier(self.stack)
        return ProviderFactoryBinding(
            **values,
            provider_factory=factory,
            receipt_verifier=verifier,  # type: ignore[arg-type]
        )


def _adapter(
    stack: BrokerStack,
    port: FakeProviderPort,
    *,
    factory: FakeFactoryPort | None = None,
    transport_factory=lambda _timeout: object(),
) -> HostProviderAdapter:
    return HostProviderAdapter(
        stack.configuration,
        factory or FakeFactoryPort(stack, port),
        transport_factory=transport_factory,
    )


def _host_request(stack: BrokerStack, **changes: Any) -> dict[str, Any]:
    request = {
        "jsonrpc": "2.0",
        "id": "123e4567-e89b-42d3-a456-426614174200",
        "method": "host.model.invoke/v1",
        "meta": {
            "protocol_version": "1",
            "generation_id": GENERATION_ID,
            "plugin_release_id": CALLER_RELEASE_ID,
            "deadline_at": "2030-01-02T03:09:05Z",
            "context": "attempt",
            "operation_id": "model-operation-1",
            "job_id": "job-1",
            "step_id": "step-1",
            "attempt_id": "attempt-1",
            "lease_epoch": 1,
        },
        "params": {
            "operation_key": "model-operation-1",
            "invocation_id": "provider-invocation-1",
            "invocation_key": "provider-invocation-key-1",
            "model_profile_revision_id": stack.profile["revision_id"],
            "request_asset_id": stack.request_asset_id,
            "replay_policy": "never_replay",
        },
    }
    for key, value in changes.items():
        if key.startswith("meta__"):
            request["meta"][key.removeprefix("meta__")] = value
        elif key.startswith("params__"):
            request["params"][key.removeprefix("params__")] = value
        else:
            request[key] = value
    return request


def _broker(
    stack: BrokerStack,
    port: FakeProviderPort,
    *,
    phase_hook=None,
) -> ModelBroker:
    return ModelBroker(
        stack.execution,
        _adapter(stack, port),
        phase_hook=phase_hook,
    )


def _row(stack: BrokerStack) -> sqlite3.Row:
    with stack.repository.read_connection() as connection:
        row = connection.execute("SELECT * FROM model_invocation").fetchone()
    assert row is not None
    return row


def _assert_local_value_absent_from_persistence(
    stack: BrokerStack, local_value: str
) -> None:
    marker = local_value.encode()
    with stack.repository.read_connection() as connection:
        for table_row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ):
            table = str(table_row[0])
            if table == "p1_local_secret_value":
                continue
            for value_row in connection.execute(f'SELECT * FROM "{table}"'):
                for value in value_row:
                    if isinstance(value, str):
                        assert marker not in value.encode()
                    elif isinstance(value, bytes):
                        assert marker not in value
    assert all(
        marker not in path.read_bytes()
        for path in stack.assets.root.rglob("*")
        if path.is_file()
    )


def _second_host_request(stack: BrokerStack) -> dict[str, Any]:
    return _host_request(
        stack,
        id="123e4567-e89b-42d3-a456-426614174201",
        meta__operation_id="model-operation-2",
        params__operation_key="model-operation-2",
        params__invocation_id="provider-invocation-2",
        params__invocation_key="provider-invocation-key-2",
    )


@pytest.mark.parametrize(
    ("provider_state", "host_state"),
    (
        ("receipted", "received"),
        ("failed", "failed"),
        ("cancelled", "failed"),
        ("uncertain", "uncertain"),
    ),
)
def test_receipt_backed_terminal_mapping_t3_and_exact_replay(
    tmp_path: Path, provider_state: str, host_state: str
) -> None:
    stack = _build_authority(tmp_path, provider_state)
    port = FakeProviderPort(stack, state=provider_state)
    broker = _broker(stack, port)
    request = _host_request(stack)
    try:
        prepared = broker.prepare_host_invocation(request)
        assert prepared.replayed is False
        assert prepared.result["state"] == host_state
        assert prepared.result["receipt_id"].startswith("asset-sha256-")
        assert port.invoke_calls == 1
        assert port.transaction_states == [False]
        assert _row(stack)["state"] == "dispatching"

        prepared.commit()
        row = _row(stack)
        assert row["state"] == host_state
        assert row["receipt_asset_id"] == prepared.result["receipt_id"]
        assert row["model_receipt_id"] == (
            f"logical-receipt-{provider_state}-provider-invocation-1"
        )
        assert row["model_receipt_id"] != row["receipt_asset_id"]
        receipt = decode_model_receipt(
            parse_json_bytes(stack.assets.read(row["receipt_asset_id"]))
        )
        assert len(receipt) == 27
        assert receipt["state"] == provider_state
        assert RAW_VALUE.encode() not in stack.assets.read(row["receipt_asset_id"])

        replay = broker.prepare_host_invocation(copy.deepcopy(request))
        assert replay.replayed is True
        assert dict(replay.result) == dict(prepared.result)
        replay.commit()
        assert port.invoke_calls == 1
    finally:
        stack.close()


def test_six_p0b_hash_domains_are_bound_without_substitution(tmp_path: Path) -> None:
    stack = _build_authority(tmp_path, "hash-domains")
    port = FakeProviderPort(stack)
    broker = _broker(stack, port)
    try:
        prepared = broker.prepare_host_invocation(_host_request(stack))
        prepared.commit()
        row = _row(stack)
        domains = (
            row["source_request_asset_hash"],
            row["receipt_asset_hash"],
            row["provider_transport_request_hash"],
            row["provider_transport_response_hash"],
            row["response_asset_hash"],
            row["receipt_hash"],
        )
        assert len(set(domains)) == 6
        with (
            stack.repository.transaction() as connection,
            pytest.raises(sqlite3.IntegrityError),
        ):
            connection.execute(
                "UPDATE model_invocation SET receipt_hash=response_asset_hash"
            )
    finally:
        stack.close()


def test_complete_identity_conflicts_and_current_fence_precedes_replay(
    tmp_path: Path,
) -> None:
    stack = _build_authority(tmp_path, "identity")
    port = FakeProviderPort(stack)
    broker = _broker(stack, port)
    request = _host_request(stack)
    try:
        first = broker.prepare_host_invocation(request)
        first.commit()
        assert port.invoke_calls == 1

        for changed in (
            {"params__invocation_id": "provider-invocation-other"},
            {"params__invocation_key": "provider-invocation-key-other"},
            {"params__replay_policy": "manual_if_unknown"},
        ):
            with pytest.raises(ContractError) as conflict:
                broker.prepare_host_invocation(_host_request(stack, **changed))
            assert conflict.value.code == int(ErrorCode.DUPLICATE_REQUEST)
        alternate_request = stack.assets.put(
            canonical_bytes(
                {
                    "schema": "model-provider-request-asset/v1",
                    "profile_revision_id": stack.profile["revision_id"],
                    "messages": [{"role": "user", "content": "Different request."}],
                    "options": {"stream": False},
                }
            ),
            mime="application/json",
            logical_role="model_request",
            provenance="test:p2a:alternate",
        )
        with pytest.raises(ContractError) as asset_conflict:
            broker.prepare_host_invocation(
                _host_request(
                    stack, params__request_asset_id=alternate_request.asset_id
                )
            )
        assert asset_conflict.value.code == int(ErrorCode.DUPLICATE_REQUEST)
        with pytest.raises(ContractError) as unique_conflict:
            broker.prepare_host_invocation(
                _host_request(
                    stack,
                    meta__operation_id="model-operation-2",
                    params__operation_key="model-operation-2",
                )
            )
        assert unique_conflict.value.code == int(ErrorCode.DUPLICATE_REQUEST)
        assert port.invoke_calls == 1

        with stack.repository.transaction() as connection:
            connection.execute(
                "UPDATE execution_attempt SET state='fenced',revision=revision+1 "
                "WHERE attempt_id='attempt-1'"
            )
        with pytest.raises(ContractError) as stale:
            broker.prepare_host_invocation(request)
        assert stale.value.code == int(ErrorCode.STALE_LEASE)
        assert port.invoke_calls == 1
    finally:
        stack.close()


def test_historical_snapshot_plan_wins_over_switched_workspace_pointer(
    tmp_path: Path,
) -> None:
    stack = _build_authority(tmp_path, "plan-switch")
    port = FakeProviderPort(stack)
    try:
        stack.planning.register_verified_plan_reference(
            generation_id=GENERATION_ID,
            plan_revision_id="plan-revision-2",
            plan_revision_hash="2" * 64,
        )
        stack.planning.select_workspace_plan(
            {
                "schema": "workspace-plan-selection-command/v2",
                "operation_key": "plan-operation-2",
                "workspace_id": WORKSPACE_ID,
                "expected_workspace_revision": 1,
                "expected_current_plan_revision_id": PLAN_ID,
                "expected_current_plan_revision_hash": PLAN_HASH,
                "selection_mode": "explicit",
                "plan_revision_id": "plan-revision-2",
                "plan_revision_hash": "2" * 64,
                "model_profile_revision_id": stack.profile["revision_id"],
                "model_profile_revision_hash": stack.profile["revision_hash"],
                "expected_active_generation_id": GENERATION_ID,
            }
        )
        _install_generation(stack.repository, _generation("generation-planning-2"))
        prepared = _broker(stack, port).prepare_host_invocation(
            _host_request(stack)
        )
        prepared.commit()
        row = _row(stack)
        assert row["plan_revision_id"] == PLAN_ID
        assert row["plan_revision_hash"] == PLAN_HASH
        assert stack.repository.get_workspace(WORKSPACE_ID).current_plan_revision_id == (
            "plan-revision-2"
        )
        assert port.invoke_calls == 1
    finally:
        stack.close()


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize(
    ("phase", "expected_calls"),
    (
        ("after_t2", 0),
        ("after_provider", 1),
        ("after_response_asset", 1),
        ("after_receipt_asset", 1),
        ("before_t3", 1),
    ),
)
def test_post_barrier_crash_recovers_receiptless_uncertain_without_resend(
    tmp_path: Path, phase: str, expected_calls: int
) -> None:
    stack = _build_authority(tmp_path, f"crash-{phase}")
    port = FakeProviderPort(stack)

    def crash(current: str) -> None:
        if current == phase:
            raise SimulatedCrash(phase)

    broker = _broker(stack, port, phase_hook=crash)
    request = _host_request(stack)
    try:
        with pytest.raises(SimulatedCrash):
            broker.prepare_host_invocation(request)
        row = _row(stack)
        assert row["state"] == "dispatching"
        assert row["receipt_asset_id"] is None
        assert row["response_asset_id"] is None
        assert port.invoke_calls == expected_calls

        assert broker.recover_dispatching() == 1
        recovered = _row(stack)
        assert recovered["state"] == "uncertain"
        assert recovered["receipt_asset_id"] is None
        assert recovered["host_result_json"] is None
        assert recovered["rpc_error_json"] is not None
        with pytest.raises(ContractError) as replay:
            _broker(stack, port).prepare_host_invocation(request)
        assert replay.value.code == int(ErrorCode.UNCERTAIN_EXTERNAL_EFFECT)
        portless = ModelBroker(stack.execution, None)
        with pytest.raises(ContractError) as portless_replay:
            portless.prepare_host_invocation(request)
        assert portless_replay.value.code == int(ErrorCode.INCOMPATIBLE_GENERATION)
        assert port.invoke_calls == expected_calls
    finally:
        stack.close()


def test_pre_barrier_reserved_crash_can_dispatch_once(tmp_path: Path) -> None:
    stack = _build_authority(tmp_path, "crash-reserved")
    port = FakeProviderPort(stack)

    def crash(phase: str) -> None:
        if phase == "after_t1":
            raise SimulatedCrash(phase)

    try:
        with pytest.raises(SimulatedCrash):
            _broker(stack, port, phase_hook=crash).prepare_host_invocation(
                _host_request(stack)
            )
        assert _row(stack)["state"] == "reserved"
        assert port.invoke_calls == 0
        assert _broker(stack, port).recover_dispatching() == 0
        prepared = _broker(stack, port).prepare_host_invocation(_host_request(stack))
        prepared.commit()
        assert _row(stack)["state"] == "received"
        assert port.invoke_calls == 1
    finally:
        stack.close()


def test_t3_then_ack_window_replays_without_second_call(tmp_path: Path) -> None:
    stack = _build_authority(tmp_path, "crash-after-t3")
    port = FakeProviderPort(stack)

    def crash(phase: str) -> None:
        if phase == "after_t3":
            raise SimulatedCrash(phase)

    request = _host_request(stack)
    try:
        prepared = _broker(stack, port, phase_hook=crash).prepare_host_invocation(request)
        with pytest.raises(SimulatedCrash):
            prepared.commit()
        assert _row(stack)["state"] == "received"
        replay = _broker(stack, port).prepare_host_invocation(request)
        assert replay.replayed is True
        assert replay.result["state"] == "received"
        assert port.invoke_calls == 1
    finally:
        stack.close()


def test_concurrent_same_operation_has_one_dispatch_owner(tmp_path: Path) -> None:
    stack = _build_authority(tmp_path, "concurrent")
    entered = threading.Event()
    release = threading.Event()
    port = FakeProviderPort(stack, entered=entered, release=release)
    broker = _broker(stack, port)
    request = _host_request(stack)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(broker.prepare_host_invocation, request)
            assert entered.wait(timeout=2)
            second = pool.submit(broker.prepare_host_invocation, copy.deepcopy(request))
            with pytest.raises(ContractError) as duplicate_inflight:
                second.result(timeout=2)
            assert duplicate_inflight.value.code == int(
                ErrorCode.UNCERTAIN_EXTERNAL_EFFECT
            )
            release.set()
            prepared = first.result(timeout=5)
        prepared.commit()
        assert port.invoke_calls == 1
        assert _row(stack)["state"] == "received"
    finally:
        release.set()
        stack.close()


@pytest.mark.parametrize("mode", ("raise", "rpc_error"))
def test_receiptless_provider_outcome_is_uncertain_and_never_replayed(
    tmp_path: Path, mode: str
) -> None:
    stack = _build_authority(tmp_path, f"receiptless-{mode}")
    port = FakeProviderPort(stack, mode=mode)
    broker = _broker(stack, port)
    request = _host_request(stack)
    try:
        with pytest.raises(ContractError) as first:
            broker.prepare_host_invocation(request)
        assert first.value.code == int(ErrorCode.UNCERTAIN_EXTERNAL_EFFECT)
        row = _row(stack)
        assert row["state"] == "uncertain"
        assert row["receipt_asset_id"] is None
        assert row["response_asset_id"] is None
        assert row["host_result_json"] is None
        assert row["rpc_error_json"] is not None
        with pytest.raises(ContractError) as replay:
            broker.prepare_host_invocation(request)
        assert replay.value.code == int(ErrorCode.UNCERTAIN_EXTERNAL_EFFECT)
        assert port.invoke_calls == 1
    finally:
        stack.close()


def test_resolved_local_value_is_only_transient_port_input(tmp_path: Path) -> None:
    stack = _build_authority(tmp_path, "secret-isolation")
    port = FakeProviderPort(stack, mode="leak")
    try:
        with pytest.raises(ContractError) as rejected:
            _broker(stack, port).prepare_host_invocation(_host_request(stack))
        assert rejected.value.code == int(ErrorCode.UNCERTAIN_EXTERNAL_EFFECT)
        assert port.resolved_value_digest == hashlib.sha256(RAW_VALUE.encode()).hexdigest()
        row = _row(stack)
        assert row["state"] == "uncertain"
        assert row["receipt_asset_id"] is None
        assert row["response_asset_id"] is None

        _assert_local_value_absent_from_persistence(stack, RAW_VALUE)
    finally:
        stack.close()


def test_provider_prepare_drift_and_stale_release_make_zero_calls(tmp_path: Path) -> None:
    stack = _build_authority(tmp_path, "zero-call")
    drift = FakeProviderPort(stack, mode="prepare_drift")
    try:
        with pytest.raises(ContractError) as invalid:
            _broker(stack, drift).prepare_host_invocation(_host_request(stack))
        assert invalid.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)
        assert drift.invoke_calls == 0
        with stack.repository.read_connection() as connection:
            assert connection.execute(
                "SELECT count(*) FROM model_invocation"
            ).fetchone()[0] == 0

        clean = FakeProviderPort(stack)
        zero_call_cases = (
            ({"meta__plugin_release_id": "f" * 64}, ErrorCode.INCOMPATIBLE_GENERATION),
            ({"meta__generation_id": "generation-other"}, ErrorCode.INCOMPATIBLE_GENERATION),
            ({"meta__lease_epoch": 2}, ErrorCode.STALE_LEASE),
            ({"params__model_profile_revision_id": "profile-other"}, ErrorCode.INCOMPATIBLE_GENERATION),
            ({"params__request_asset_id": "asset-sha256-" + "0" * 64}, ErrorCode.ASSET_ERROR),
        )
        for changes, expected_code in zero_call_cases:
            with pytest.raises(ContractError) as rejected:
                _broker(stack, clean).prepare_host_invocation(
                    _host_request(stack, **changes)
                )
            assert rejected.value.code == int(expected_code)
        assert clean.prepare_calls == clean.invoke_calls == 0
    finally:
        stack.close()


def test_unavailable_p2b_port_is_fail_closed_without_ledger_effect(tmp_path: Path) -> None:
    stack = _build_authority(tmp_path, "port-unavailable")
    try:
        broker = ModelBroker(stack.execution, None)
        with pytest.raises(ContractError) as unavailable:
            broker.prepare_host_invocation(_host_request(stack))
        assert unavailable.value.code == int(ErrorCode.INCOMPATIBLE_GENERATION)
        with stack.repository.read_connection() as connection:
            assert connection.execute(
                "SELECT count(*) FROM model_invocation"
            ).fetchone()[0] == 0
    finally:
        stack.close()


@pytest.mark.parametrize(
    "coincidental_value",
    (
        "1",
        "model",
        "model-receipt/v1",
        "provider-invocation-1",
        "planner-model-1",
        "secret://provider-main",
        "received",
    ),
)
def test_provenance_scoped_check_allows_fixed_identity_overlap(
    tmp_path: Path, coincidental_value: str
) -> None:
    suffix = hashlib.sha256(coincidental_value.encode()).hexdigest()[:10]
    stack = _build_authority(
        tmp_path, f"fixed-overlap-{suffix}", raw_value=coincidental_value
    )
    port = FakeProviderPort(stack)
    try:
        prepared = _broker(stack, port).prepare_host_invocation(
            _host_request(stack)
        )
        prepared.commit()
        assert prepared.result["state"] == "received"
        assert port.resolved_value_digest == hashlib.sha256(
            coincidental_value.encode()
        ).hexdigest()
        assert _row(stack)["state"] == "received"
    finally:
        stack.close()


@pytest.mark.parametrize(
    "mode",
    (
        "receipt_missing",
        "receipt_extra",
        "receipt_type",
        "receipt_self_hash",
        "anchor_drift",
        "hash_substitution",
    ),
)
def test_invalid_receipt_anchor_and_hash_domains_never_reach_t3(
    tmp_path: Path, mode: str
) -> None:
    stack = _build_authority(tmp_path, f"invalid-{mode}")
    port = FakeProviderPort(stack, mode=mode)
    request = _host_request(stack)
    try:
        with pytest.raises(ContractError) as rejected:
            _broker(stack, port).prepare_host_invocation(request)
        assert rejected.value.code == int(ErrorCode.UNCERTAIN_EXTERNAL_EFFECT)
        row = _row(stack)
        assert row["state"] == "uncertain"
        assert row["provider_success_json"] is None
        assert row["receipt_asset_id"] is None
        assert row["response_asset_id"] is None
        with pytest.raises(ContractError) as replay:
            _broker(stack, port).prepare_host_invocation(request)
        assert replay.value.code == int(ErrorCode.UNCERTAIN_EXTERNAL_EFFECT)
        assert port.invoke_calls == 1
    finally:
        stack.close()


@pytest.mark.parametrize(
    ("mode", "provider_state"),
    (
        ("response_reflection", "receipted"),
        ("response_chunk_reflection", "receipted"),
        ("response_nested_key_reflection", "receipted"),
        ("response_nested_value_reflection", "receipted"),
        ("response_schema_reflection", "receipted"),
        ("receipt_metadata_reflection", "receipted"),
        ("receipt_metadata_key_reflection", "receipted"),
        ("receipt_error_reflection", "failed"),
        ("receipt_input_context_reflection", "receipted"),
        ("receipt_profile_reflection", "receipted"),
        ("receipt_cost_reflection", "receipted"),
        ("receipt_response_id_reflection", "receipted"),
        ("rpc_error_reflection", "receipted"),
    ),
)
def test_dynamic_local_value_reflection_rejects_before_asset_publication(
    tmp_path: Path, mode: str, provider_state: str
) -> None:
    stack = _build_authority(tmp_path, f"dynamic-reflection-{mode}")
    port = FakeProviderPort(stack, mode=mode, state=provider_state)
    before_assets = {
        path.relative_to(stack.assets.root)
        for path in stack.assets.root.rglob("*")
        if path.is_file()
    }
    try:
        with pytest.raises(ContractError) as rejected:
            _broker(stack, port).prepare_host_invocation(_host_request(stack))
        assert rejected.value.code == int(ErrorCode.UNCERTAIN_EXTERNAL_EFFECT)
        assert RAW_VALUE not in str(rejected.value)
        row = _row(stack)
        assert row["state"] == "uncertain"
        assert row["provider_success_json"] is None
        assert row["response_asset_id"] is None
        assert row["receipt_asset_id"] is None
        assert {
            path.relative_to(stack.assets.root)
            for path in stack.assets.root.rglob("*")
            if path.is_file()
        } == before_assets
        _assert_local_value_absent_from_persistence(stack, RAW_VALUE)
        assert port.invoke_calls == 1
    finally:
        stack.close()


def test_distinct_operations_share_identical_response_asset_bytes(
    tmp_path: Path,
) -> None:
    stack = _build_authority(tmp_path, "same-response-content")
    first_port = FakeProviderPort(stack)
    second_port = FakeProviderPort(stack)
    try:
        first = _broker(stack, first_port).prepare_host_invocation(
            _host_request(stack)
        )
        first.commit()
        second = _broker(stack, second_port).prepare_host_invocation(
            _second_host_request(stack)
        )
        second.commit()
        assert first.result["state"] == second.result["state"] == "received"
        assert first.result["response_asset_id"] == second.result["response_asset_id"]
        metadata = stack.assets.describe(str(first.result["response_asset_id"]))
        assert metadata.logical_role == "core_internal"
        assert metadata.provenance == "core:execution"
        with stack.repository.read_connection() as connection:
            assert [
                tuple(row)
                for row in connection.execute(
                    "SELECT operation_key,state,response_asset_id "
                    "FROM model_invocation ORDER BY operation_key"
                )
            ] == [
                ("model-operation-1", "received", first.result["response_asset_id"]),
                ("model-operation-2", "received", first.result["response_asset_id"]),
            ]
        assert first_port.invoke_calls == second_port.invoke_calls == 1
    finally:
        stack.close()


def test_identical_bytes_can_cross_receipt_and_response_roles(
    tmp_path: Path,
) -> None:
    stack = _build_authority(tmp_path, "cross-role-content")
    first_port = FakeProviderPort(stack)
    try:
        first = _broker(stack, first_port).prepare_host_invocation(
            _host_request(stack)
        )
        first.commit()
        first_receipt_id = str(first.result["receipt_id"])
        shared_bytes = stack.assets.read(first_receipt_id)
        second_port = FakeProviderPort(
            stack, response_bytes_override=shared_bytes
        )
        second = _broker(stack, second_port).prepare_host_invocation(
            _second_host_request(stack)
        )
        second.commit()
        assert second.result["state"] == "received"
        assert second.result["response_asset_id"] == first_receipt_id
        metadata = stack.assets.describe(first_receipt_id)
        assert metadata.logical_role == "core_internal"
        assert metadata.provenance == "core:execution"
        assert first_port.invoke_calls == second_port.invoke_calls == 1
    finally:
        stack.close()
