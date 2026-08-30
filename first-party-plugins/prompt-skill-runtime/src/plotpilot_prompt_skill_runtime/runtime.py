"""Opaque preparation and Broker-only execution for Prompt/Skill chains."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Self
from weakref import WeakKeyDictionary

from plotpilot_plugin_sdk import ContractError, assert_valid, hash_jcs, verify_snapshot

from .attribution import (
    AttributionProof,
    FrozenModelInvocation,
    verify_model_receipt_asset,
)
from .chain import (
    AssetRef,
    ChainAnchor,
    ChainExecution,
    PatchEvidence,
    SkillExecution,
    SkillStep,
    _materialize_skill_chain,
)
from .immutability import freeze_json, thaw_json
from .package import SkillPackage
from .persistence import SQLiteSkillRepository
from .ports import GenerationPort, SkillBrokerPort

_RUNTIME_PLUGIN_ID = "com.plotpilot.prompt-skill-runtime"


def _invalid(message: str, *, path: str | None = None) -> ContractError:
    return ContractError(1011, message, path=path)


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{field} must be a non-empty string", path=field)
    return value


@dataclass(frozen=True, slots=True)
class BrokerSkillResult:
    """Closed result from the composed durable Broker adapter."""

    output: AssetRef | None
    model_receipt_asset: AssetRef | None
    invocation: FrozenModelInvocation | None
    model_claimed: bool = False
    step_state: str = "executed"
    patches: tuple[PatchEvidence | Mapping[str, Any], ...] = ()
    warnings: tuple[Mapping[str, Any], ...] = ()
    replacement_assets: Mapping[str, bytes | str] | None = None

    def __post_init__(self) -> None:
        if self.step_state not in {"executed", "failed", "cancelled", "uncertain"}:
            raise _invalid("Broker Skill result state is invalid")
        if type(self.model_claimed) is not bool:
            raise _invalid("model_claimed must be boolean")
        if (self.model_receipt_asset is None) != (self.invocation is None):
            raise _invalid("ModelReceipt Asset and frozen invocation must be all-null or all-present")
        if self.model_claimed and self.model_receipt_asset is None:
            raise _invalid("model_claimed requires authoritative ModelReceipt evidence")


class PreparedSkillRun:
    """Zero-state capability issued and consumed by one Runtime instance."""

    __slots__ = ("__weakref__",)

    def __new__(cls, *_args: Any, **_kwargs: Any) -> Self:
        raise TypeError("PreparedSkillRun can only be created by PromptSkillRuntime.prepare")

    def __copy__(self) -> PreparedSkillRun:
        raise TypeError("PreparedSkillRun cannot be copied")

    def __deepcopy__(self, _memo: dict[int, Any]) -> PreparedSkillRun:
        raise TypeError("PreparedSkillRun cannot be copied")

    def __reduce__(self) -> Any:
        raise TypeError("PreparedSkillRun cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise TypeError("PreparedSkillRun cannot be serialized")


@dataclass(frozen=True, slots=True)
class _PreparedRunRecord:
    job: Mapping[str, Any]
    snapshot: Mapping[str, Any]
    generation: Mapping[str, Any]
    generation_id: str
    chain_id: str
    anchor: ChainAnchor
    input_asset: AssetRef
    steps: tuple[SkillStep, ...]
    package_identities: tuple[tuple[str, str, str], ...]
    fingerprint: str


class PromptSkillRuntime:
    """Validate everything first, then materialize and durably record a chain."""

    def __init__(
        self,
        *,
        repository: SQLiteSkillRepository,
        generation_port: GenerationPort,
        broker: SkillBrokerPort,
        asset_reader: Callable[[str], Any],
        package_reader: Callable[[str, str], SkillPackage],
    ) -> None:
        if not all(callable(value) for value in (asset_reader, package_reader)):
            raise TypeError("Runtime readers must be callable authoritative adapters")
        self._repository = repository
        self._generation_port = generation_port
        self._broker = broker
        self._asset_reader = asset_reader
        self._package_reader = package_reader
        self._prepared_lock = threading.RLock()
        self._prepared: WeakKeyDictionary[PreparedSkillRun, _PreparedRunRecord] = WeakKeyDictionary()

    def _asset(self, asset_id: str, expected_hash: str) -> AssetRef:
        value = self._asset_reader(asset_id)
        if isinstance(value, AssetRef):
            asset = value
        elif isinstance(value, (bytes, bytearray, memoryview, str)):
            asset = AssetRef.from_value(value, asset_id=asset_id)
        else:
            asset = AssetRef(asset_id, bytes(value.content), getattr(value, "sha256", ""))
        if asset.asset_id != asset_id or asset.sha256 != expected_hash:
            raise _invalid("Core Asset authority returned identity/hash drift", path="asset_id")
        return asset

    def _generation_state(self) -> tuple[str, bool]:
        state = self._generation_port.generation_state()
        if not isinstance(state, Mapping) or set(state) != {"current_generation_id", "safe_mode"}:
            raise _invalid("Generation authority state must be a closed current/safe-mode mapping")
        generation_id = _require_text(state["current_generation_id"], "current_generation_id")
        if type(state["safe_mode"]) is not bool:
            raise _invalid("safe_mode must be boolean")
        return generation_id, state["safe_mode"]

    def _validate_generation(
        self,
        generation_id: str,
        snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        generation = dict(self._generation_port.get_generation(generation_id))
        assert_valid("plugin-generation/v1", generation)
        current, safe_mode = self._generation_state()
        if safe_mode or current != generation_id or generation["generation_id"] != generation_id:
            raise _invalid("Prompt Skill execution requires the exact current non-safe-mode Generation")
        members = generation["members"]
        member_ids = [member["plugin_id"] for member in members]
        if len(member_ids) != len(set(member_ids)):
            raise _invalid("Generation members must be unique")
        runtime_members = [member for member in members if member["plugin_id"] == _RUNTIME_PLUGIN_ID]
        if len(runtime_members) != 1:
            raise _invalid("Generation must contain the Prompt Skill Runtime exactly once")
        snapshot_plugins = {
            (item["plugin_id"], item["release_id"], item["package_hash"])
            for item in snapshot["plugin_releases"]
        }
        generation_plugins = {
            (item["plugin_id"], item["release_id"], item["package_hash"])
            for item in members
        }
        if not snapshot_plugins.issubset(generation_plugins):
            raise _invalid("RunSnapshot plugin members do not belong to the frozen Generation")
        return generation

    def _derive_steps(
        self,
        snapshot: Mapping[str, Any],
    ) -> tuple[tuple[SkillStep, ...], tuple[tuple[str, str, str], ...]]:
        """Rederive every Skill binding from the authoritative frozen Snapshot."""

        asset_hashes = {item["asset_id"]: item["sha256"] for item in snapshot["asset_hashes"]}
        steps: list[SkillStep] = []
        package_identities: list[tuple[str, str, str]] = []
        for binding in snapshot["skill_releases"]:
            parameters_id = binding["parameters_asset_id"]
            parameters_hash = None if parameters_id is None else asset_hashes.get(parameters_id)
            if parameters_id is not None and parameters_hash is None:
                raise _invalid("Skill parameters Asset is absent from RunSnapshot asset_hashes")
            if parameters_id is not None:
                self._asset(parameters_id, parameters_hash)
            step = SkillStep(
                binding["skill_id"],
                binding["release_id"],
                binding["package_hash"],
                binding["order"],
                parameters_id,
                parameters_hash,
            )
            package = self._package_reader(step.skill_id, step.release_id)
            package.verify_authoritative()
            identity = (package.skill_id, package.release_id, package.package_hash)
            if identity != (step.skill_id, step.release_id, step.package_hash):
                raise _invalid("frozen Skill binding differs from authoritative package identity")
            steps.append(step)
            package_identities.append(identity)
        if not steps:
            raise _invalid("RunSnapshot must freeze at least one Skill")
        return tuple(steps), tuple(package_identities)

    def prepare(
        self,
        *,
        job: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        generation_id: str,
        chain_id: str,
        input_asset_id: str,
        anchor: ChainAnchor,
    ) -> PreparedSkillRun:
        job_value = dict(job)
        _require_text(job_value.get("job_id"), "job_id")
        _require_text(job_value.get("step_id"), "step_id")
        snapshot_value = dict(snapshot)
        verify_snapshot(snapshot_value)
        generation = self._validate_generation(generation_id, snapshot_value)
        _require_text(chain_id, "chain_id")
        asset_hashes = {item["asset_id"]: item["sha256"] for item in snapshot_value["asset_hashes"]}
        if input_asset_id not in asset_hashes:
            raise _invalid("initial input Asset is absent from RunSnapshot")
        input_asset = self._asset(input_asset_id, asset_hashes[input_asset_id])
        steps, package_identities = self._derive_steps(snapshot_value)
        frozen_job = freeze_json(job_value, path="job")
        frozen_snapshot = freeze_json(snapshot_value, path="snapshot")
        frozen_generation = freeze_json(generation, path="generation")
        fingerprint = hash_jcs(
            "prompt-skill-prepared/v1",
            {
                "job": thaw_json(frozen_job),
                "snapshot": thaw_json(frozen_snapshot),
                "generation": thaw_json(frozen_generation),
                "generation_id": generation_id,
                "chain_id": chain_id,
                "anchor": anchor.as_dict(),
                "input_asset_id": input_asset.asset_id,
                "input_hash": input_asset.sha256,
                "steps": [
                    {**step.as_snapshot_binding(), "parameters_hash": step.parameters_hash}
                    for step in steps
                ],
                "packages": [list(item) for item in package_identities],
            },
        )
        record = _PreparedRunRecord(
            frozen_job,
            frozen_snapshot,
            frozen_generation,
            generation_id,
            chain_id,
            anchor,
            input_asset,
            steps,
            package_identities,
            fingerprint,
        )
        handle = object.__new__(PreparedSkillRun)
        with self._prepared_lock:
            self._prepared[handle] = record
        return handle

    def _consume_prepared(self, prepared: PreparedSkillRun) -> _PreparedRunRecord:
        if not isinstance(prepared, PreparedSkillRun):
            raise TypeError("execute requires a Runtime-owned PreparedSkillRun")
        with self._prepared_lock:
            record = self._prepared.pop(prepared, None)
        if record is None:
            raise TypeError("PreparedSkillRun is forged, cross-Runtime, copied, or already consumed")
        return record

    def _revalidate(self, record: _PreparedRunRecord) -> tuple[dict[str, Any], dict[str, Any]]:
        job = thaw_json(record.job)
        snapshot = thaw_json(record.snapshot)
        verify_snapshot(snapshot)
        generation = self._validate_generation(record.generation_id, snapshot)
        if generation != thaw_json(record.generation):
            raise _invalid("Generation content changed after preparation")
        steps, package_identities = self._derive_steps(snapshot)
        if steps != record.steps or package_identities != record.package_identities:
            raise _invalid("Skill step chain changed after preparation")
        self._asset(record.input_asset.asset_id, record.input_asset.sha256)
        fingerprint = hash_jcs(
            "prompt-skill-prepared/v1",
            {
                "job": job,
                "snapshot": snapshot,
                "generation": generation,
                "generation_id": record.generation_id,
                "chain_id": record.chain_id,
                "anchor": record.anchor.as_dict(),
                "input_asset_id": record.input_asset.asset_id,
                "input_hash": record.input_asset.sha256,
                "steps": [
                    {**step.as_snapshot_binding(), "parameters_hash": step.parameters_hash}
                    for step in steps
                ],
                "packages": [list(item) for item in package_identities],
            },
        )
        if fingerprint != record.fingerprint:
            raise _invalid("Prepared Skill run seal is invalid")
        return job, generation

    def execute(self, prepared: PreparedSkillRun) -> ChainExecution:
        record = self._consume_prepared(prepared)
        job, generation = self._revalidate(record)
        try:
            return self._repository.load_chain(record.chain_id)
        except KeyError:
            pass
        cached: dict[int, SkillExecution] = {}
        proofs: dict[str, AttributionProof] = {}
        replay_context: dict[str, tuple[bytes, Mapping[str, bytes | str]]] = {}
        failure: BaseException | None = None

        def invoke(step: SkillStep, input_asset: AssetRef) -> SkillExecution:
            nonlocal failure
            index = record.steps.index(step)
            if index in cached:
                return cached[index]
            context = {
                "job_id": job["job_id"],
                "step_id": job["step_id"],
                "run_snapshot_hash": thaw_json(record.snapshot)["snapshot_hash"],
                "generation_id": record.generation_id,
                "chain_id": record.chain_id,
                "chain_index": index,
                "skill_id": step.skill_id,
                "release_id": step.release_id,
                "package_hash": step.package_hash,
                "parameters_asset_id": step.parameters_asset_id,
                "parameters_hash": step.parameters_hash,
                "input_asset_id": input_asset.asset_id,
                "input_hash": input_asset.sha256,
            }
            invocation_key = hash_jcs("prompt-skill-model-invocation/v1", context)
            parameters_asset = None
            if step.parameters_asset_id is not None:
                parameters_asset = self._asset(step.parameters_asset_id, step.parameters_hash)
            reconcile_kwargs = {
                "invocation_key": invocation_key,
                "invocation_context": freeze_json(context),
                "generation": freeze_json(generation),
                "job": freeze_json(job),
                "step": step,
            }

            def uncertain_execution(cause: BaseException) -> SkillExecution:
                nonlocal failure
                failure = cause
                return SkillExecution(
                    step_state="failed",
                    warnings=(
                        {
                            "code": "uncertain_external_effect",
                            "message": "Broker reconciliation could not prove a terminal invocation state",
                            "details_asset_id": None,
                        },
                    ),
                    terminal_chain_status="failed",
                )

            try:
                reconciled = self._broker.reconcile_skill(**reconcile_kwargs)
            except BaseException as reconcile_error:  # noqa: BLE001 - uncertainty must stop dispatch
                cached[index] = uncertain_execution(reconcile_error)
                return cached[index]
            if reconciled is not None and not isinstance(reconciled, BrokerSkillResult):
                cached[index] = uncertain_execution(
                    _invalid("Broker reconciliation must return BrokerSkillResult or null")
                )
                return cached[index]
            raw = reconciled
            if raw is None:
                try:
                    raw = self._broker.execute_skill(
                        invocation_key=invocation_key,
                        invocation_context=freeze_json(context),
                        generation=freeze_json(generation),
                        job=freeze_json(job),
                        step=step,
                        input_asset=input_asset,
                        parameters_asset=parameters_asset,
                    )
                    if not isinstance(raw, BrokerSkillResult):
                        raise _invalid("Broker adapter must return BrokerSkillResult")
                except BaseException as execute_error:  # noqa: BLE001 - reconcile the crash window
                    try:
                        recovered = self._broker.reconcile_skill(**reconcile_kwargs)
                    except BaseException as reconcile_error:  # noqa: BLE001 - preserve original failure
                        execute_error.add_note(f"reconciliation unavailable: {reconcile_error!r}")
                        cached[index] = uncertain_execution(execute_error)
                        return cached[index]
                    if recovered is None:
                        cached[index] = uncertain_execution(execute_error)
                        return cached[index]
                    if not isinstance(recovered, BrokerSkillResult):
                        execute_error.add_note("reconciliation returned an invalid result")
                        cached[index] = uncertain_execution(execute_error)
                        return cached[index]
                    raw = recovered
            output = raw.output
            if raw.step_state == "executed" and output is None:
                raise _invalid("executed Broker Skill result requires an authoritative output Asset")
            if raw.step_state != "executed" and output is not None:
                raise _invalid("non-success Broker Skill result cannot carry an output Asset")
            if output is not None:
                output = self._asset(output.asset_id, output.sha256)
            proof: AttributionProof | None = None
            if raw.model_receipt_asset is not None:
                receipt_asset = self._asset(raw.model_receipt_asset.asset_id, raw.model_receipt_asset.sha256)
                invocation = raw.invocation
                if thaw_json(invocation.input_context) != context:
                    raise _invalid("Broker invocation context does not match the frozen Skill invocation")
                if invocation.invocation_key != invocation_key:
                    raise _invalid("Broker invocation key does not match the deterministic Skill invocation")
                if invocation.profile_revision_id != thaw_json(record.snapshot)["model_profile_revision_id"]:
                    raise _invalid("Broker Model Profile does not match RunSnapshot")
                provider_members = [
                    member
                    for member in generation["members"]
                    if member["plugin_id"] == invocation.provider_plugin_id
                    and member["release_id"] == invocation.provider_release_id
                ]
                if len(provider_members) != 1:
                    raise _invalid("Broker Provider/Release does not belong to the frozen Generation")
                proof = verify_model_receipt_asset(
                    receipt_asset,
                    invocation=invocation,
                    expected_context=context,
                    require_receipted=False,
                )
                expected_receipt_state = {
                    "executed": "receipted",
                    "failed": "failed",
                    "cancelled": "cancelled",
                    "uncertain": "uncertain",
                }[raw.step_state]
                if proof.receipt["state"] != expected_receipt_state:
                    raise _invalid("Broker result state does not match its terminal ModelReceipt")
                proofs[f"{record.chain_id}:receipt:{index}"] = proof
            if raw.model_claimed and raw.step_state != "executed":
                raise _invalid("only a successful receipted invocation may claim model attribution")
            attributed = proof is not None and raw.step_state == "executed"
            warnings = list(raw.warnings)
            public_step_state = raw.step_state
            terminal_chain_status = None
            if raw.step_state == "cancelled":
                public_step_state = "skipped"
                terminal_chain_status = "cancelled"
                warnings.append(
                    {
                        "code": "broker_cancelled",
                        "message": "Broker invocation was cancelled",
                        "details_asset_id": None,
                    }
                )
            elif raw.step_state == "uncertain":
                public_step_state = "failed"
                terminal_chain_status = "failed"
                warnings.append(
                    {
                        "code": "uncertain_external_effect",
                        "message": "Broker invocation remains uncertain after reconciliation",
                        "details_asset_id": None,
                    }
                )
                if failure is None:
                    failure = ContractError(1009, "Broker invocation remains uncertain after reconciliation")
            execution = SkillExecution(
                output=output,
                step_state=public_step_state,
                participated=attributed,
                model_claimed=raw.model_claimed if attributed else False,
                verified_patch=any(
                    (patch.verified if isinstance(patch, PatchEvidence) else patch.get("verified") is True)
                    for patch in raw.patches
                ),
                claim_evidence_asset_id=proof.asset_id if attributed else None,
                patches=raw.patches,
                warnings=tuple(warnings),
                replacement_assets=raw.replacement_assets,
                attribution_proof=proof if attributed else None,
                terminal_chain_status=terminal_chain_status,
            )
            cached[index] = execution
            return execution

        execution = _materialize_skill_chain(
            chain_id=record.chain_id,
            run_snapshot_hash=thaw_json(record.snapshot)["snapshot_hash"],
            initial_input=record.input_asset,
            steps=record.steps,
            execute=invoke,
            anchor=record.anchor,
        )
        for receipt in execution.receipts:
            item = cached[receipt["chain_index"]]
            if item.verified_patch:
                input_asset = self._asset(receipt["input_asset_id"], receipt["input_hash"])
                replay_context[receipt["receipt_id"]] = (input_asset.content, item.replacement_assets or {})
        self._repository.record_chain(
            execution,
            expected_steps=record.steps,
            replay_context=replay_context,
            attribution_proofs=proofs,
        )
        if failure is not None:
            raise failure
        return execution
