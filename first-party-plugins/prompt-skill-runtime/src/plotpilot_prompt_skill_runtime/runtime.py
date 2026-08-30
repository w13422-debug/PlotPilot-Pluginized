"""Opaque preparation and Broker-only execution for Prompt/Skill chains."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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
_SEAL = object()


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
        if self.step_state not in {"executed", "failed", "cancelled"}:
            raise _invalid("Broker Skill result state is invalid")
        if type(self.model_claimed) is not bool:
            raise _invalid("model_claimed must be boolean")
        if (self.model_receipt_asset is None) != (self.invocation is None):
            raise _invalid("ModelReceipt Asset and frozen invocation must be all-null or all-present")
        if self.model_claimed and self.model_receipt_asset is None:
            raise _invalid("model_claimed requires authoritative ModelReceipt evidence")


class PreparedSkillRun:
    """Runtime-owned, recursively sealed preparation handle."""

    __slots__ = (
        "_anchor",
        "_chain_id",
        "_fingerprint",
        "_generation",
        "_generation_id",
        "_input_asset",
        "_job",
        "_package_identities",
        "_snapshot",
        "_steps",
    )

    def __init__(
        self,
        *,
        job: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        generation: Mapping[str, Any],
        generation_id: str,
        chain_id: str,
        anchor: ChainAnchor,
        input_asset: AssetRef,
        steps: Sequence[SkillStep],
        packages: Sequence[SkillPackage],
        _seal: object,
    ) -> None:
        if _seal is not _SEAL:
            raise TypeError("PreparedSkillRun can only be created by PromptSkillRuntime.prepare")
        self._job = freeze_json(job, path="job")
        self._snapshot = freeze_json(snapshot, path="snapshot")
        self._generation = freeze_json(generation, path="generation")
        self._generation_id = generation_id
        self._chain_id = chain_id
        self._anchor = anchor
        self._input_asset = input_asset
        self._steps = tuple(steps)
        self._package_identities = tuple(
            (package.skill_id, package.release_id, package.package_hash) for package in packages
        )
        self._fingerprint = hash_jcs(
            "prompt-skill-prepared/v1",
            {
                "job": thaw_json(self._job),
                "snapshot": thaw_json(self._snapshot),
                "generation": thaw_json(self._generation),
                "generation_id": generation_id,
                "chain_id": chain_id,
                "anchor": anchor.as_dict(),
                "input_asset_id": input_asset.asset_id,
                "input_hash": input_asset.sha256,
                "steps": [
                    {
                        **step.as_snapshot_binding(),
                        "parameters_hash": step.parameters_hash,
                    }
                    for step in self._steps
                ],
                "packages": [list(item) for item in self._package_identities],
            },
        )

    @property
    def fingerprint(self) -> str:
        return self._fingerprint


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
        steps: list[SkillStep] = []
        packages: list[SkillPackage] = []
        for binding in snapshot_value["skill_releases"]:
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
            if (package.skill_id, package.release_id, package.package_hash) != (
                step.skill_id,
                step.release_id,
                step.package_hash,
            ):
                raise _invalid("frozen Skill binding differs from authoritative package identity")
            steps.append(step)
            packages.append(package)
        if not steps:
            raise _invalid("RunSnapshot must freeze at least one Skill")
        return PreparedSkillRun(
            job=job_value,
            snapshot=snapshot_value,
            generation=generation,
            generation_id=generation_id,
            chain_id=chain_id,
            anchor=anchor,
            input_asset=input_asset,
            steps=steps,
            packages=packages,
            _seal=_SEAL,
        )

    def _revalidate(self, prepared: PreparedSkillRun) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(prepared, PreparedSkillRun):
            raise TypeError("execute requires a Runtime-owned PreparedSkillRun")
        job = thaw_json(prepared._job)
        snapshot = thaw_json(prepared._snapshot)
        verify_snapshot(snapshot)
        generation = self._validate_generation(prepared._generation_id, snapshot)
        if generation != thaw_json(prepared._generation):
            raise _invalid("Generation content changed after preparation")
        expected = []
        for step, identity in zip(prepared._steps, prepared._package_identities, strict=True):
            package = self._package_reader(step.skill_id, step.release_id)
            package.verify_authoritative()
            actual = (package.skill_id, package.release_id, package.package_hash)
            if actual != identity or actual != (step.skill_id, step.release_id, step.package_hash):
                raise _invalid("Skill package changed after preparation")
            if step.parameters_asset_id is not None:
                self._asset(step.parameters_asset_id, step.parameters_hash)
            expected.append({**step.as_snapshot_binding(), "parameters_hash": step.parameters_hash})
        self._asset(prepared._input_asset.asset_id, prepared._input_asset.sha256)
        fingerprint = hash_jcs(
            "prompt-skill-prepared/v1",
            {
                "job": job,
                "snapshot": snapshot,
                "generation": generation,
                "generation_id": prepared._generation_id,
                "chain_id": prepared._chain_id,
                "anchor": prepared._anchor.as_dict(),
                "input_asset_id": prepared._input_asset.asset_id,
                "input_hash": prepared._input_asset.sha256,
                "steps": expected,
                "packages": [list(item) for item in prepared._package_identities],
            },
        )
        if fingerprint != prepared.fingerprint:
            raise _invalid("Prepared Skill run seal is invalid")
        return job, generation

    def execute(self, prepared: PreparedSkillRun) -> ChainExecution:
        job, generation = self._revalidate(prepared)
        try:
            return self._repository.load_chain(prepared._chain_id)
        except KeyError:
            pass
        cached: dict[int, SkillExecution] = {}
        proofs: dict[str, AttributionProof] = {}
        replay_context: dict[str, tuple[bytes, Mapping[str, bytes | str]]] = {}
        failed_index: int | None = None
        failure: BaseException | None = None
        failure_state = "failed"

        def invoke(step: SkillStep, input_asset: AssetRef) -> SkillExecution:
            nonlocal failed_index, failure, failure_state
            index = prepared._steps.index(step)
            if index in cached:
                return cached[index]
            context = {
                "job_id": job["job_id"],
                "step_id": job["step_id"],
                "run_snapshot_hash": thaw_json(prepared._snapshot)["snapshot_hash"],
                "generation_id": prepared._generation_id,
                "chain_id": prepared._chain_id,
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
            except BaseException as exc:  # noqa: BLE001 - cancellation/interrupts require durable closure
                failure = exc
                failed_index = index
                # Reconciliation is best-effort evidence collection.  A
                # broker outage here must not prevent the failed/skipped
                # closure from being durably recorded, nor replace the
                # original execution exception that the caller needs.
                try:
                    reconciled = self._broker.reconcile_skill(
                        invocation_key=invocation_key,
                        invocation_context=freeze_json(context),
                        generation=freeze_json(generation),
                        job=freeze_json(job),
                        step=step,
                    )
                    if isinstance(reconciled, BrokerSkillResult) and reconciled.step_state == "cancelled":
                        failure_state = "failed"
                except BaseException as reconcile_error:  # noqa: BLE001 - preserve original broker failure
                    # Keep reconciliation failure observable without changing
                    # the exception raised for the original execution call.
                    exc.add_note(f"reconciliation unavailable: {reconcile_error!r}")
                cached[index] = SkillExecution(step_state=failure_state)
                return cached[index]
            output = raw.output
            if raw.step_state == "executed" and output is None:
                raise _invalid("executed Broker Skill result requires an authoritative output Asset")
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
                if invocation.profile_revision_id != thaw_json(prepared._snapshot)["model_profile_revision_id"]:
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
                )
                proofs[f"{prepared._chain_id}:receipt:{index}"] = proof
            execution = SkillExecution(
                output=output,
                step_state="failed" if raw.step_state == "cancelled" else raw.step_state,
                participated=proof is not None,
                model_claimed=raw.model_claimed,
                verified_patch=any(
                    (patch.verified if isinstance(patch, PatchEvidence) else patch.get("verified") is True)
                    for patch in raw.patches
                ),
                claim_evidence_asset_id=None if proof is None else proof.asset_id,
                patches=raw.patches,
                warnings=raw.warnings,
                replacement_assets=raw.replacement_assets,
                attribution_proof=proof,
            )
            cached[index] = execution
            return execution

        execution = _materialize_skill_chain(
            chain_id=prepared._chain_id,
            run_snapshot_hash=thaw_json(prepared._snapshot)["snapshot_hash"],
            initial_input=prepared._input_asset,
            steps=prepared._steps,
            execute=invoke,
            anchor=prepared._anchor,
        )
        for receipt in execution.receipts:
            item = cached[receipt["chain_index"]]
            if item.verified_patch:
                input_asset = self._asset(receipt["input_asset_id"], receipt["input_hash"])
                replay_context[receipt["receipt_id"]] = (input_asset.content, item.replacement_assets or {})
        self._repository.record_chain(
            execution,
            expected_steps=prepared._steps,
            replay_context=replay_context,
            attribution_proofs=proofs,
        )
        if failure is not None:
            raise failure
        return execution
