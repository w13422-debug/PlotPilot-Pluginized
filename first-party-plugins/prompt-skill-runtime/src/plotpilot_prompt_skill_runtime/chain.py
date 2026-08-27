"""Pure deterministic Skill-chain materialization.

The runtime produces only contract-shaped dictionaries.  Asset creation,
bundle staging, stream ACKs and Job aggregation remain Core/P1/P3 concerns;
callers pass their already frozen IDs/hashes through the small value objects
below.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from plotpilot_plugin_sdk import (
    ContractError,
    ContractValidationError,
    hash_jcs,
    sha256_hex,
    verify_skill_chain as _sdk_verify_skill_chain,
    verify_skill_receipt as _sdk_verify_skill_receipt,
)


_HASH = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def _invalid(message: str, *, path: str | None = None) -> ContractError:
    return ContractError(1011, message, path=path)


def _as_bytes(value: bytes | bytearray | memoryview | str, *, field: str) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    raise _invalid(f"{field} must be UTF-8 text or bytes", path=field)


def _check_hash(value: str, field: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise _invalid(f"{field} must be lowercase SHA-256", path=field)


def _check_id(value: str | None, field: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise _invalid(f"{field} is not a v1 ID", path=field)


@dataclass(frozen=True, slots=True)
class AssetRef:
    asset_id: str
    content: bytes
    sha256: str = ""

    def __post_init__(self) -> None:
        _check_id(self.asset_id, "asset_id")
        content = bytes(self.content)
        object.__setattr__(self, "content", content)
        digest = sha256_hex(content)
        if self.sha256 and self.sha256 != digest:
            raise _invalid("AssetRef content/hash mismatch", path="sha256")
        object.__setattr__(self, "sha256", digest)

    @classmethod
    def from_value(cls, value: "AssetRef | bytes | bytearray | memoryview | str", *, asset_id: str | None = None) -> "AssetRef":
        if isinstance(value, cls):
            return value
        content = _as_bytes(value, field="asset")
        digest = sha256_hex(content)
        return cls(asset_id or f"asset-{digest}", content, digest)


@dataclass(frozen=True, slots=True)
class ChainAnchor:
    """One of the three mutually exclusive Skill evidence profiles."""

    result_bundle_id: str | None = None
    result_item_id: str | None = None
    stream_id: str | None = None
    acked_prefix_hash: str | None = None

    def __post_init__(self) -> None:
        _check_id(self.result_bundle_id, "result_bundle_id", nullable=True)
        _check_id(self.result_item_id, "result_item_id", nullable=True)
        _check_id(self.stream_id, "stream_id", nullable=True)
        if self.acked_prefix_hash is not None:
            _check_hash(self.acked_prefix_hash, "acked_prefix_hash")
        bundle = self.result_bundle_id is not None
        if bundle != (self.result_item_id is not None):
            raise _invalid("bundle anchor must be all-null or all-present")
        stream = self.stream_id is not None
        if stream != (self.acked_prefix_hash is not None):
            raise _invalid("stream anchor must be all-null or all-present")
        if bundle and stream:
            raise _invalid("Skill chain anchor has more than one profile")

    @classmethod
    def bundle(cls, bundle_id: str, item_id: str) -> "ChainAnchor":
        return cls(result_bundle_id=bundle_id, result_item_id=item_id)

    @classmethod
    def stream(cls, stream_id: str, acked_prefix_hash: str) -> "ChainAnchor":
        return cls(stream_id=stream_id, acked_prefix_hash=acked_prefix_hash)

    @classmethod
    def bundleless(cls) -> "ChainAnchor":
        return cls()

    @property
    def profile(self) -> str:
        if self.result_bundle_id is not None:
            return "bundle-backed"
        if self.stream_id is not None:
            return "stream-backed"
        return "bundleless-terminal"

    def as_dict(self) -> dict[str, str | None]:
        return {
            "result_bundle_id": self.result_bundle_id,
            "result_item_id": self.result_item_id,
            "stream_id": self.stream_id,
            "acked_prefix_hash": self.acked_prefix_hash,
        }


def _anchor_from(value: Mapping[str, Any]) -> ChainAnchor:
    return ChainAnchor(
        result_bundle_id=value.get("result_bundle_id"),
        result_item_id=value.get("result_item_id"),
        stream_id=value.get("stream_id"),
        acked_prefix_hash=value.get("acked_prefix_hash"),
    )


@dataclass(frozen=True, slots=True)
class SkillStep:
    skill_id: str
    release_id: str
    package_hash: str
    order: int
    parameters_asset_id: str | None = None

    def __post_init__(self) -> None:
        _check_id(self.skill_id, "skill_id")
        _check_hash(self.release_id, "release_id")
        _check_hash(self.package_hash, "package_hash")
        if not isinstance(self.order, int) or isinstance(self.order, bool) or self.order < 1:
            raise _invalid("Skill order must be a positive integer", path="order")
        _check_id(self.parameters_asset_id, "parameters_asset_id", nullable=True)

    def as_snapshot_binding(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "release_id": self.release_id,
            "package_hash": self.package_hash,
            "parameters_asset_id": self.parameters_asset_id,
            "order": self.order,
        }


@dataclass(frozen=True, slots=True)
class PatchEvidence:
    patch_id: str
    start_codepoint: int
    end_codepoint: int
    replacement_asset_id: str
    replacement_hash: str
    before_hash: str
    after_hash: str
    verified: bool = False

    def __post_init__(self) -> None:
        _check_id(self.patch_id, "patch_id")
        _check_id(self.replacement_asset_id, "replacement_asset_id")
        for name, value in (
            ("replacement_hash", self.replacement_hash),
            ("before_hash", self.before_hash),
            ("after_hash", self.after_hash),
        ):
            _check_hash(value, name)
        if not isinstance(self.start_codepoint, int) or isinstance(self.start_codepoint, bool) or self.start_codepoint < 0:
            raise _invalid("patch start_codepoint must be non-negative", path="start_codepoint")
        if not isinstance(self.end_codepoint, int) or isinstance(self.end_codepoint, bool) or self.end_codepoint < self.start_codepoint:
            raise _invalid("patch range is inverted", path="end_codepoint")
        if not isinstance(self.verified, bool):
            raise _invalid("patch verified must be boolean", path="verified")

    def as_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "start_codepoint": self.start_codepoint,
            "end_codepoint": self.end_codepoint,
            "replacement_asset_id": self.replacement_asset_id,
            "replacement_hash": self.replacement_hash,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "verified": self.verified,
        }


def replay_patches(
    input_content: bytes | str,
    patches: Sequence[Mapping[str, Any] | PatchEvidence],
    replacement_assets: Mapping[str, bytes | str],
) -> bytes:
    """Replay spans against exact input and fail closed on any hash drift.

    Codepoint ranges use Python/Unicode scalar indexes and are half-open.  A
    patch's ``before_hash`` covers the source span, ``replacement_hash`` the
    replacement bytes, and ``after_hash`` the complete replayed output.  The
    last rule gives every verified patch a deterministic whole-output proof and
    prevents a patch from being altered while its receipt hash is recomputed.
    """

    source = _as_bytes(input_content, field="input_content")
    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _invalid("patch input must be UTF-8") from exc
    normalized: list[dict[str, Any]] = []
    for raw in patches:
        patch = raw.as_dict() if isinstance(raw, PatchEvidence) else dict(raw)
        start = patch.get("start_codepoint")
        end = patch.get("end_codepoint")
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start or end > len(text):
            raise _invalid("patch range is outside input", path="patches")
        replacement_id = patch.get("replacement_asset_id")
        if replacement_id not in replacement_assets:
            raise _invalid("patch replacement Asset is unavailable", path="replacement_asset_id")
        replacement = _as_bytes(replacement_assets[replacement_id], field="replacement_asset")
        before_hash = sha256_hex(text[start:end].encode("utf-8"))
        replacement_hash = sha256_hex(replacement)
        if patch.get("before_hash") != before_hash:
            raise _invalid("patch before_hash does not match exact input", path="before_hash")
        if patch.get("replacement_hash") != replacement_hash:
            raise _invalid("patch replacement_hash does not match Asset", path="replacement_hash")
        try:
            replacement_text = replacement.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise _invalid("patch replacement must be UTF-8") from exc
        normalized.append({**patch, "replacement_text": replacement_text})
    normalized.sort(key=lambda item: (item["start_codepoint"], item["end_codepoint"], item.get("patch_id", "")))
    previous_end = 0
    for patch in normalized:
        if patch["start_codepoint"] < previous_end:
            raise _invalid("overlapping Skill patches are ambiguous", path="patches")
        previous_end = patch["end_codepoint"]
    for patch in reversed(normalized):
        start, end = patch["start_codepoint"], patch["end_codepoint"]
        text = text[:start] + patch["replacement_text"] + text[end:]
    output = text.encode("utf-8")
    output_hash = sha256_hex(output)
    for patch in normalized:
        if patch.get("verified") and patch.get("after_hash") != output_hash:
            raise _invalid("verified patch after_hash does not match replay output", path="after_hash")
    return output


def make_patch_evidence(
    *,
    patch_id: str,
    input_content: bytes | str,
    start_codepoint: int,
    end_codepoint: int,
    replacement_asset_id: str,
    replacement: bytes | str,
    verified: bool = True,
    all_patches: Sequence[Mapping[str, Any] | PatchEvidence] = (),
) -> PatchEvidence:
    """Create a patch proof whose hashes are derived from exact bytes."""

    source = _as_bytes(input_content, field="input_content")
    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _invalid("patch input must be UTF-8") from exc
    if start_codepoint < 0 or end_codepoint < start_codepoint or end_codepoint > len(text):
        raise _invalid("patch range is outside input")
    replacement_bytes = _as_bytes(replacement, field="replacement")
    replacement_text = replacement_bytes.decode("utf-8", errors="strict")
    output_text = text[:start_codepoint] + replacement_text + text[end_codepoint:]
    # For multiple patches, callers can pass the complete set so after_hash is
    # the same complete replay proof that replay_patches verifies.
    if all_patches:
        output = replay_patches(source, all_patches, {replacement_asset_id: replacement_bytes})
        output_hash = sha256_hex(output)
    else:
        output_hash = sha256_hex(output_text.encode("utf-8"))
    return PatchEvidence(
        patch_id,
        start_codepoint,
        end_codepoint,
        replacement_asset_id,
        sha256_hex(replacement_bytes),
        sha256_hex(text[start_codepoint:end_codepoint].encode("utf-8")),
        output_hash,
        verified,
    )


@dataclass(frozen=True, slots=True)
class SkillExecution:
    """Deterministic callback result; no Provider or Core handle is stored."""

    output: AssetRef | bytes | str | None = None
    step_state: str = "executed"
    participated: bool = True
    model_claimed: bool = False
    verified_patch: bool = False
    claim_evidence_asset_id: str | None = None
    patches: tuple[PatchEvidence | Mapping[str, Any], ...] = ()
    warnings: tuple[Mapping[str, Any], ...] = ()
    replacement_assets: Mapping[str, bytes | str] | None = None


def build_receipt(
    *,
    receipt_id: str,
    chain_id: str,
    chain_index: int,
    run_snapshot_hash: str,
    skill_id: str,
    release_id: str,
    package_hash: str,
    parameters_asset_id: str | None = None,
    input_asset_id: str,
    input_hash: str,
    output_asset_id: str | None = None,
    output_hash: str | None = None,
    step_state: str = "executed",
    frozen: bool = True,
    participated: bool = False,
    model_claimed: bool = False,
    verified_patch: bool = False,
    claim_evidence_asset_id: str | None = None,
    patches: Sequence[PatchEvidence | Mapping[str, Any]] = (),
    warnings: Sequence[Mapping[str, Any]] = (),
    previous_receipt_hash: str | None = None,
    anchor: ChainAnchor | None = None,
    result_bundle_id: str | None = None,
    result_item_id: str | None = None,
    stream_id: str | None = None,
    acked_prefix_hash: str | None = None,
    input_content: bytes | str | None = None,
    replacement_assets: Mapping[str, bytes | str] | None = None,
) -> dict[str, Any]:
    """Materialize one frozen receipt and verify its self-hash."""

    _check_id(receipt_id, "receipt_id")
    _check_id(chain_id, "chain_id")
    _check_hash(run_snapshot_hash, "run_snapshot_hash")
    _check_id(skill_id, "skill_id")
    _check_hash(release_id, "release_id")
    _check_hash(package_hash, "package_hash")
    _check_id(input_asset_id, "input_asset_id")
    _check_hash(input_hash, "input_hash")
    if output_asset_id is not None:
        _check_id(output_asset_id, "output_asset_id")
    if output_hash is not None:
        _check_hash(output_hash, "output_hash")
    if (output_asset_id is None) != (output_hash is None):
        raise _invalid("Skill output Asset ID/hash must be all-null or all-present")
    if not isinstance(frozen, bool) or not frozen:
        raise _invalid("Skill receipt must be frozen before chain aggregation")
    if step_state not in {"executed", "failed", "skipped"}:
        raise _invalid("invalid Skill step_state")
    if not isinstance(participated, bool) or not isinstance(model_claimed, bool) or not isinstance(verified_patch, bool):
        raise _invalid("Skill attribution fields must be boolean")
    if step_state == "skipped" and (participated or verified_patch):
        raise _invalid("skipped Skill step cannot be participated or verified")
    if model_claimed and claim_evidence_asset_id is None:
        raise _invalid("model_claimed requires claim evidence")
    if claim_evidence_asset_id is not None:
        _check_id(claim_evidence_asset_id, "claim_evidence_asset_id")
    if previous_receipt_hash is not None:
        _check_hash(previous_receipt_hash, "previous_receipt_hash")
    selected_anchor = anchor or ChainAnchor(result_bundle_id, result_item_id, stream_id, acked_prefix_hash)
    if anchor is not None and any(x is not None for x in (result_bundle_id, result_item_id, stream_id, acked_prefix_hash)):
        if selected_anchor != ChainAnchor(result_bundle_id, result_item_id, stream_id, acked_prefix_hash):
            raise _invalid("explicit anchor disagrees with anchor fields")
    patch_dicts = [patch.as_dict() if isinstance(patch, PatchEvidence) else dict(patch) for patch in patches]
    for patch in patch_dicts:
        if patch.get("end_codepoint", -1) < patch.get("start_codepoint", 0):
            raise _invalid("Skill patch range is inverted")
    if verified_patch:
        if not any(patch.get("verified") is True for patch in patch_dicts):
            raise _invalid("verified_patch requires a verified patch")
        if input_content is None or replacement_assets is None or output_hash is None:
            raise _invalid("verified_patch requires exact replay evidence")
        replayed = replay_patches(input_content, patch_dicts, replacement_assets)
        if sha256_hex(replayed) != output_hash:
            raise _invalid("verified patch replay does not match output hash")
    value: dict[str, Any] = {
        "schema": "skill-run-receipt/v1",
        "receipt_id": receipt_id,
        "chain_id": chain_id,
        "chain_index": chain_index,
        "run_snapshot_hash": run_snapshot_hash,
        **selected_anchor.as_dict(),
        "skill_id": skill_id,
        "release_id": release_id,
        "package_hash": package_hash,
        "parameters_asset_id": parameters_asset_id,
        "input_asset_id": input_asset_id,
        "input_hash": input_hash,
        "output_asset_id": output_asset_id,
        "output_hash": output_hash,
        "step_state": step_state,
        "frozen": frozen,
        "participated": participated,
        "model_claimed": model_claimed,
        "verified_patch": verified_patch,
        "claim_evidence_asset_id": claim_evidence_asset_id,
        "patches": patch_dicts,
        "warnings": [dict(warning) for warning in warnings],
        "previous_receipt_hash": previous_receipt_hash,
        "receipt_hash": "",
    }
    value["receipt_hash"] = hash_jcs("skill-run-receipt/v1", {k: v for k, v in value.items() if k != "receipt_hash"})
    _sdk_verify_skill_receipt(value)
    return value


def verify_receipt(
    receipt: Mapping[str, Any],
    *,
    input_content: bytes | str | None = None,
    replacement_assets: Mapping[str, bytes | str] | None = None,
) -> None:
    """Verify the public schema plus independent attribution replay proof."""

    value = dict(receipt)
    _sdk_verify_skill_receipt(value)
    _anchor_from(value)
    if value["verified_patch"]:
        if input_content is None or replacement_assets is None or value["output_hash"] is None:
            raise _invalid("verified_patch requires exact replay evidence")
        replayed = replay_patches(input_content, value["patches"], replacement_assets)
        if sha256_hex(replayed) != value["output_hash"]:
            raise _invalid("verified patch replay does not match output hash")


def build_chain_result(
    *,
    chain_id: str,
    run_snapshot_hash: str,
    input_hash: str,
    receipts: Sequence[Mapping[str, Any]],
    chain_status: str | None = None,
    anchor: ChainAnchor | None = None,
    result_bundle_id: str | None = None,
    result_item_id: str | None = None,
    stream_id: str | None = None,
    acked_prefix_hash: str | None = None,
    final_output_asset_id: str | None = None,
    final_output_hash: str | None = None,
    replay_context: Mapping[str, tuple[bytes | str, Mapping[str, bytes | str]]] | None = None,
) -> dict[str, Any]:
    if not receipts:
        raise _invalid("Skill chain must contain at least one frozen receipt")
    selected = anchor or ChainAnchor(result_bundle_id, result_item_id, stream_id, acked_prefix_hash)
    if anchor is not None and any(x is not None for x in (result_bundle_id, result_item_id, stream_id, acked_prefix_hash)):
        if selected != ChainAnchor(result_bundle_id, result_item_id, stream_id, acked_prefix_hash):
            raise _invalid("explicit chain anchor disagrees with anchor fields")
    receipt_list = [dict(receipt) for receipt in receipts]
    if chain_status is None:
        if any(r["step_state"] == "failed" for r in receipt_list):
            chain_status = "failed"
        elif any(r["step_state"] == "skipped" for r in receipt_list):
            chain_status = "partial"
        else:
            chain_status = "succeeded"
    if chain_status not in {"succeeded", "partial", "failed", "cancelled"}:
        raise _invalid("invalid Skill chain_status")
    value: dict[str, Any] = {
        "schema": "skill-chain-result/v1",
        "chain_id": chain_id,
        "run_snapshot_hash": run_snapshot_hash,
        **selected.as_dict(),
        "receipt_ids": [r["receipt_id"] for r in receipt_list],
        "receipt_hashes": [r["receipt_hash"] for r in receipt_list],
        "chain_status": chain_status,
        "input_hash": input_hash,
        "final_output_asset_id": final_output_asset_id,
        "final_output_hash": final_output_hash,
        "chain_hash": "",
    }
    final_hash = final_output_hash or "-"
    value["chain_hash"] = sha256_hex(
        b"skill-chain/v1\n"
        + b"\n".join(r["receipt_hash"].encode("ascii") for r in receipt_list)
        + f"\n{final_hash}\n".encode("ascii")
    )
    verify_chain(value, receipt_list, initial_input_hash=input_hash, replay_context=replay_context)
    return value


def verify_chain(
    chain: Mapping[str, Any],
    receipts: Sequence[Mapping[str, Any]],
    *,
    initial_input_hash: str | None = None,
    expected_steps: Sequence[SkillStep] | None = None,
    replay_context: Mapping[str, tuple[bytes | str, Mapping[str, bytes | str]]] | None = None,
) -> None:
    """Verify chain schema, ordered identity, continuity and patch proofs."""

    value = dict(chain)
    receipt_list = sorted((dict(receipt) for receipt in receipts), key=lambda item: item.get("chain_index", -1))
    _sdk_verify_skill_chain(value, receipt_list)
    if initial_input_hash is not None:
        _check_hash(initial_input_hash, "initial_input_hash")
        if receipt_list[0]["input_hash"] != initial_input_hash or value["input_hash"] != initial_input_hash:
            raise _invalid("Skill chain input hash does not match initial input")
    previous_output: str | None = None
    for index, receipt in enumerate(receipt_list):
        if receipt["chain_index"] != index:
            raise _invalid("Skill chain indexes must be continuous")
        if index == 0:
            if receipt["previous_receipt_hash"] is not None:
                raise _invalid("first Skill receipt cannot have a previous hash")
        elif receipt["previous_receipt_hash"] != receipt_list[index - 1]["receipt_hash"]:
            raise _invalid("Skill receipt previous hash chain is broken")
        if previous_output is not None and receipt["input_hash"] != previous_output:
            raise _invalid("Skill step input does not equal previous output")
        if receipt["step_state"] == "failed" and index + 1 < len(receipt_list) and any(
            later["step_state"] == "executed" for later in receipt_list[index + 1 :]
        ):
            raise _invalid("failed Skill step cannot be followed by executed steps")
        if receipt["output_hash"] is not None:
            previous_output = receipt["output_hash"]
        if expected_steps is not None:
            step = expected_steps[index]
            if (receipt["skill_id"], receipt["release_id"], receipt["package_hash"]) != (
                step.skill_id,
                step.release_id,
                step.package_hash,
            ):
                raise _invalid("Skill receipt release identity does not match frozen step")
        if receipt["verified_patch"]:
            context = (replay_context or {}).get(receipt["receipt_id"])
            if context is None:
                raise _invalid("verified_patch has no replay context")
            verify_receipt(receipt, input_content=context[0], replacement_assets=context[1])
    chain_anchor = _anchor_from(value)
    if chain_anchor.profile == "bundleless-terminal" and value["chain_status"] not in {"failed", "cancelled"}:
        raise _invalid("only failed/cancelled Skill chains may be bundleless")
    for receipt in receipt_list:
        if _anchor_from(receipt) != chain_anchor:
            raise _invalid("Skill receipt anchor does not match chain")
    if value["chain_status"] in {"succeeded", "partial"} and chain_anchor.profile == "bundleless-terminal":
        raise _invalid("successful/partial Skill chains need a bundle or stream anchor")


@dataclass(frozen=True, slots=True)
class ChainExecution(Mapping[str, Any]):
    """Mapping-compatible result exposing both chain and ordered receipts."""

    chain: Mapping[str, Any]
    receipts: tuple[Mapping[str, Any], ...]

    def __getitem__(self, key: str) -> Any:
        return self.chain[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.chain)

    def __len__(self) -> int:
        return len(self.chain)

    @property
    def chain_result(self) -> Mapping[str, Any]:
        return self.chain


def _coerce_execution(value: Any) -> SkillExecution:
    if isinstance(value, SkillExecution):
        return value
    if isinstance(value, Mapping):
        return SkillExecution(
            output=value.get("output", value.get("output_content")),
            step_state=value.get("step_state", "executed"),
            participated=value.get("participated", True),
            model_claimed=value.get("model_claimed", False),
            verified_patch=value.get("verified_patch", False),
            claim_evidence_asset_id=value.get("claim_evidence_asset_id"),
            patches=tuple(value.get("patches", ())),
            warnings=tuple(value.get("warnings", ())),
            replacement_assets=value.get("replacement_assets"),
        )
    return SkillExecution(output=value)


def run_skill_chain(
    *,
    chain_id: str,
    run_snapshot_hash: str,
    initial_input: AssetRef | bytes | str,
    steps: Sequence[SkillStep],
    execute: Callable[[SkillStep, bytes], SkillExecution | Mapping[str, Any] | AssetRef | bytes | str | None] | None = None,
    anchor: ChainAnchor,
    receipt_id_prefix: str | None = None,
    chain_status: str | None = None,
) -> ChainExecution:
    """Execute deterministic local callbacks in the exact supplied order."""

    if not steps:
        raise _invalid("Skill chain requires at least one step")
    if len({step.order for step in steps}) != len(steps):
        raise _invalid("Skill order values must be unique")
    if len({step.skill_id for step in steps}) != len(steps):
        raise _invalid("one Skill may occur only once in a frozen chain")
    for left, right in zip(steps, steps[1:]):
        if left.order >= right.order:
            raise _invalid("Skill steps must preserve the user's order")
    initial = AssetRef.from_value(initial_input)
    current = initial
    receipts: list[dict[str, Any]] = []
    replay_context: dict[str, tuple[bytes | str, Mapping[str, bytes | str]]] = {}
    previous_receipt_hash: str | None = None
    halted: str | None = None
    runner = execute or (lambda _step, content: content)
    prefix = receipt_id_prefix or f"{chain_id}:receipt"
    _check_id(prefix, "receipt_id_prefix")
    for index, step in enumerate(steps):
        receipt_id = f"{prefix}:{index}"
        if halted is not None:
            execution = SkillExecution(output=None, step_state="skipped", participated=False, model_claimed=False, verified_patch=False)
        else:
            execution = _coerce_execution(runner(step, current.content))
            if execution.step_state not in {"executed", "failed", "skipped"}:
                raise _invalid("callback returned an invalid Skill step state")
        output_ref: AssetRef | None = None
        if execution.output is not None and execution.step_state == "executed":
            output_ref = AssetRef.from_value(execution.output)
        patches = execution.patches
        receipt = build_receipt(
            receipt_id=receipt_id,
            chain_id=chain_id,
            chain_index=index,
            run_snapshot_hash=run_snapshot_hash,
            skill_id=step.skill_id,
            release_id=step.release_id,
            package_hash=step.package_hash,
            parameters_asset_id=step.parameters_asset_id,
            input_asset_id=current.asset_id,
            input_hash=current.sha256,
            output_asset_id=output_ref.asset_id if output_ref else None,
            output_hash=output_ref.sha256 if output_ref else None,
            step_state=execution.step_state,
            participated=execution.participated,
            model_claimed=execution.model_claimed,
            verified_patch=execution.verified_patch,
            claim_evidence_asset_id=execution.claim_evidence_asset_id,
            patches=patches,
            warnings=execution.warnings,
            previous_receipt_hash=previous_receipt_hash,
            anchor=anchor,
            input_content=current.content if execution.verified_patch else None,
            replacement_assets=execution.replacement_assets,
        )
        if execution.verified_patch:
            replay_context[receipt_id] = (current.content, execution.replacement_assets or {})
        receipts.append(receipt)
        previous_receipt_hash = receipt["receipt_hash"]
        if execution.step_state == "failed":
            halted = "failed"
        elif execution.step_state == "skipped":
            halted = "skipped"
        elif output_ref is not None:
            current = output_ref
    if chain_status is None:
        chain_status = "failed" if halted == "failed" else "partial" if halted == "skipped" else "succeeded"
    final_ref = current if chain_status in {"succeeded", "partial"} and halted != "failed" else None
    chain = build_chain_result(
        chain_id=chain_id,
        run_snapshot_hash=run_snapshot_hash,
        input_hash=initial.sha256,
        receipts=receipts,
        chain_status=chain_status,
        anchor=anchor,
        final_output_asset_id=final_ref.asset_id if final_ref else None,
        final_output_hash=final_ref.sha256 if final_ref else None,
        replay_context=replay_context,
    )
    return ChainExecution(chain, tuple(receipts))


# Alias used by a few integration adapters and tests.
execute_skill_chain = run_skill_chain
