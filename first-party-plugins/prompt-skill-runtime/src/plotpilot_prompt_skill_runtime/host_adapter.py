"""Core Host boundary for the first-party Prompt/Skill Runtime.

The public execute exchange is the frozen P0 ``prompt_skill_rpc_v2`` contract.
This module deliberately does not publish a plugin-local request/result type,
write Core state, or manufacture a ModelReceipt or result Bundle.  The
existing :class:`PromptSkillRuntime` remains the only Skill authority; a
composed P3/Core callback must return the already-authoritative v2 result.
"""
from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol

from plotpilot_plugin_sdk import (
    ContractError,
    assert_valid,
    canonical_bytes,
    parse_json_bytes,
    sha256_hex,
)
from plotpilot_plugin_sdk.prompt_skill_rpc_v2 import (
    METHOD,
    RESULT_CONTRACT,
    PromptSkillExecuteRequest,
    PromptSkillExecuteResult,
    parse_prompt_skill_execute_request_v2,
    validate_prompt_skill_execute_v2,
)
from plotpilot_plugin_sdk.verifier import (
    validate_rpc_result,
    verify_result_bundle,
    verify_skill_chain,
    verify_snapshot,
)

from .chain import AssetRef, ChainAnchor, ChainExecution
from .attribution import FrozenModelInvocation, decode_model_receipt, verify_model_receipt_asset
from .runtime import PreparedSkillRun, PromptSkillRuntime

PLUGIN_ID = "com.plotpilot.prompt-skill-runtime"
CAPABILITY_ID = METHOD
INPUT_SCHEMA = "prompt-skill-execute-request/v2"
OUTPUT_SCHEMA = "prompt-skill-execute-result/v2"

MAX_PAGE_SIZE = 8 * 1024 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_ASSET_PAGES = 256
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class HostPort(Protocol):
    """The only host capability used by this adapter."""

    def call(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]: ...


class ResultComposition(Protocol):
    """P3/Core composition seam; it owns receipts, Bundles and completion."""

    def authoritative_context(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def execute(
        self, request: Mapping[str, Any], prepared: PreparedSkillRun, runtime: PromptSkillRuntime
    ) -> Mapping[str, Any]: ...

    def chain_execution(
        self, request: Mapping[str, Any], result: Mapping[str, Any]
    ) -> ChainExecution: ...


class HostAdapterError(ContractError):
    """A fail-closed error raised at the plugin/Core boundary."""


def _invalid(message: str, *, path: str | None = None, code: int = 1011) -> HostAdapterError:
    return HostAdapterError(code, message, path=path)


def _id(value: object, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise _invalid(f"{field} must be a v2 identity", path=field)
    return value


def _hash(value: object, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise _invalid(f"{field} must be a lowercase SHA-256", path=field)
    return value


def _host_call(host: HostPort, method: str, params: Mapping[str, object]) -> dict[str, object]:
    if host is None or not callable(getattr(host, "call", None)):
        raise _invalid("Prompt Skill Runtime requires a Core HostPort", code=1010)
    try:
        result = host.call(method, dict(params))
    except HostAdapterError:
        raise
    except Exception as exc:  # pragma: no cover - host implementations vary
        raise _invalid(f"{method} failed: {type(exc).__name__}: {exc}", code=1005) from exc
    if not isinstance(result, Mapping):
        raise _invalid(f"{method} returned a non-object", code=1005)
    try:
        validate_rpc_result(method, result)
    except Exception as exc:
        raise _invalid(f"{method} returned an invalid v1 result: {exc}", code=1005) from exc
    return dict(result)


@dataclass(frozen=True, slots=True)
class HostAssetReader:
    """Read and authenticate one immutable Core Asset through Host RPC only."""

    host: HostPort
    page_size: int = 1024 * 1024
    max_bytes: int = MAX_ASSET_BYTES
    max_pages: int = MAX_ASSET_PAGES

    def __post_init__(self) -> None:
        if type(self.page_size) is not int or not 0 < self.page_size <= MAX_PAGE_SIZE:
            raise _invalid("page_size is outside the v1 bound", path="page_size")
        if type(self.max_bytes) is not int or not 0 < self.max_bytes <= MAX_ASSET_BYTES:
            raise _invalid("max_bytes is outside the Host Asset bound", path="max_bytes")
        if type(self.max_pages) is not int or not 0 < self.max_pages <= MAX_ASSET_PAGES:
            raise _invalid("max_pages is outside the Host Asset bound", path="max_pages")

    def __call__(self, asset_id: str) -> AssetRef:
        _id(asset_id, "asset_id")
        offset = 0
        total = 0
        pages = 0
        chunks: list[bytes] = []
        while True:
            pages += 1
            if pages > self.max_pages:
                raise _invalid("Host Asset exceeded the cumulative page limit", code=1005)
            result = _host_call(
                self.host,
                "host.asset.read/v1",
                {"asset_id": asset_id, "offset": offset, "length": self.page_size},
            )
            try:
                chunk = base64.b64decode(result["base64_chunk"], validate=True)
            except Exception as exc:
                raise _invalid("Host Asset page is not valid base64", code=1005) from exc
            if len(chunk) > self.page_size or total + len(chunk) > self.max_bytes:
                raise _invalid("Host Asset exceeded the cumulative byte limit", code=1005)
            if result["content_hash"] != sha256_hex(chunk):
                raise _invalid("Host Asset page hash is invalid", code=1005)
            next_offset = result["next_offset"]
            if next_offset is None:
                chunks.append(chunk)
                total += len(chunk)
                break
            if type(next_offset) is not int or next_offset != offset + len(chunk) or next_offset <= offset:
                raise _invalid("Host Asset pages are not contiguous", code=1005)
            chunks.append(chunk)
            total += len(chunk)
            offset = next_offset
        content = b"".join(chunks)
        if len(content) != total or len(content) > self.max_bytes:
            raise _invalid("Host Asset cumulative allocation is inconsistent", code=1005)
        return AssetRef(asset_id, content, sha256_hex(content))


@dataclass(frozen=True, slots=True)
class HostAssetWriter:
    """Create immutable Assets using the frozen v1 chunk-upload contract."""

    host: HostPort
    page_size: int = 1024 * 1024
    max_bytes: int = MAX_ASSET_BYTES
    max_pages: int = MAX_ASSET_PAGES

    def __post_init__(self) -> None:
        if type(self.page_size) is not int or not 0 < self.page_size <= MAX_PAGE_SIZE:
            raise _invalid("page_size is outside the v1 bound", path="page_size")
        if type(self.max_bytes) is not int or not 0 < self.max_bytes <= MAX_ASSET_BYTES:
            raise _invalid("max_bytes is outside the Host Asset bound", path="max_bytes")
        if type(self.max_pages) is not int or not 0 < self.max_pages <= MAX_ASSET_PAGES:
            raise _invalid("max_pages is outside the Host Asset bound", path="max_pages")

    def write(self, content: bytes | str, *, operation_key: str, mime: str = "application/json") -> AssetRef:
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        if len(data) > self.max_bytes:
            raise _invalid("Host Asset upload exceeded the cumulative byte limit", code=1005)
        key = _id(operation_key, "operation_key")
        digest = sha256_hex(data)
        upload_id = f"prompt-skill-upload-{hashlib.sha256((key + digest).encode()).hexdigest()[:32]}"
        chunks = [b""] if not data else [data[i : i + self.page_size] for i in range(0, len(data), self.page_size)]
        if len(chunks) > self.max_pages:
            raise _invalid("Host Asset upload exceeded the cumulative page limit", code=1005)
        accepted = 0
        asset_id: str | None = None
        for index, chunk in enumerate(chunks):
            chunk_hash = sha256_hex(chunk)
            chunk_operation_key = "prompt-skill-chunk-" + hashlib.sha256(
                f"{key}\n{upload_id}\n{accepted}\n{chunk_hash}".encode("ascii")
            ).hexdigest()[:48]
            result = _host_call(
                self.host,
                "host.asset.create/v1",
                {
                    "operation_key": chunk_operation_key,
                    "upload_id": upload_id,
                    "offset": accepted,
                    "mime": mime,
                    "total_size": len(data),
                    "expected_hash": digest,
                    "chunk_hash": chunk_hash,
                    "base64_chunk": base64.b64encode(chunk).decode("ascii"),
                    "final": index == len(chunks) - 1,
                },
            )
            if result["upload_id"] != upload_id or type(result["accepted_bytes"]) is not int:
                raise _invalid("Host upload acknowledgement is malformed", code=1005)
            if result["accepted_bytes"] != accepted + len(chunk):
                raise _invalid("Host upload acknowledgement is not contiguous", code=1005)
            accepted = result["accepted_bytes"]
            if result["completed"] and not result["asset_id"]:
                raise _invalid("completed Host upload omitted its Asset identity", code=1005)
            if result["asset_id"] is not None:
                asset_id = _id(result["asset_id"], "asset_id")
        if accepted != len(data) or asset_id is None:
            raise _invalid("Host did not complete the Asset upload", code=1005)
        return AssetRef(asset_id, data, digest)


def capability_descriptor(*, release_id: str) -> dict[str, object]:
    """Return the single frozen P0 capability owned by this Runtime plugin."""

    _hash(release_id, "release_id")
    return {
        "schema": "capability-provider/v1",
        "capability_id": CAPABILITY_ID,
        "provider": {"plugin_id": PLUGIN_ID, "release_id": release_id},
        "input_schema": INPUT_SCHEMA,
        "output_schema": OUTPUT_SCHEMA,
        "result_contract": RESULT_CONTRACT,
        "supports": ["run", "resume", "cancel"],
        "deterministic": False,
        "accepted_data_formats": [],
    }


def _anchor(value: Mapping[str, Any]) -> ChainAnchor:
    expected = {"result_bundle_id", "result_item_id", "stream_id", "acked_prefix_hash"}
    if set(value) != expected:
        raise _invalid("anchor must be the closed P0 Skill-chain anchor object", path="anchor")
    return ChainAnchor(
        result_bundle_id=value["result_bundle_id"],
        result_item_id=value["result_item_id"],
        stream_id=value["stream_id"],
        acked_prefix_hash=value["acked_prefix_hash"],
    )


def _snapshot_skill_projection(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "skill_id": item["skill_id"],
            "release_id": item["release_id"],
            "package_hash": item["package_hash"],
            "parameters_asset_id": item["parameters_asset_id"],
            "parameters_content_hash": None
            if item["parameters_asset_id"] is None
            else next(
                asset["sha256"] for asset in snapshot["asset_hashes"] if asset["asset_id"] == item["parameters_asset_id"]
            ),
        }
        for item in sorted(snapshot["skill_releases"], key=lambda item: item["order"])
    ]


def _bind_snapshot(snapshot: Mapping[str, Any], params: Mapping[str, Any]) -> None:
    verify_snapshot(dict(snapshot))
    if snapshot["snapshot_id"] != params["run_snapshot_id"] or snapshot["snapshot_hash"] != params["run_snapshot_hash"]:
        raise _invalid("RunSnapshot identity does not match the execute request")
    if snapshot["workspace_id"] != params["workspace_id"]:
        raise _invalid("RunSnapshot workspace binding drifted")
    releases = [item for item in snapshot["plugin_releases"] if item["plugin_id"] == PLUGIN_ID]
    if params["plugin_id"] != PLUGIN_ID or len(releases) != 1:
        raise _invalid("the installed Prompt Skill release is not the sole Snapshot plugin release")
    release = releases[0]
    for field in ("release_id", "package_hash"):
        request_field = "plugin_" + field if field != "package_hash" else "plugin_package_hash"
        if release[field] != params[request_field]:
            raise _invalid(f"RunSnapshot plugin {field} does not match the request")
    if _snapshot_skill_projection(snapshot) != list(params["skill_releases"]):
        raise _invalid("RunSnapshot Skill release bindings do not match the request")
    if snapshot["model_profile_revision_id"] != params["model_profile_revision_id"]:
        raise _invalid("RunSnapshot Model Profile binding drifted")
    if snapshot["parameters_asset_id"] != params["parameters_asset_id"]:
        raise _invalid("RunSnapshot parameter Asset binding drifted")
    assets = {item["asset_id"]: item["sha256"] for item in snapshot["asset_hashes"]}
    for asset_id, field in (
        (params["input_asset_id"], "input_content_hash"),
        (params["chain_asset_id"], "chain_content_hash"),
    ):
        if assets.get(asset_id) != params[field]:
            raise _invalid(f"{field} is not backed by the frozen RunSnapshot")
    if params["parameters_asset_id"] is not None and assets.get(params["parameters_asset_id"]) != params["parameters_content_hash"]:
        raise _invalid("parameters Asset hash is not backed by the frozen RunSnapshot")


def _canonical_asset(reader: HostAssetReader, asset_id: str, expected_hash: str, label: str) -> tuple[AssetRef, Any]:
    asset = reader(asset_id)
    if asset.sha256 != expected_hash:
        raise _invalid(f"{label} Asset hash does not match its frozen identity", code=1005)
    try:
        value = parse_json_bytes(asset.content)
    except Exception as exc:
        raise _invalid(f"{label} Asset must be canonical UTF-8 JSON", code=1005) from exc
    if canonical_bytes(value) != asset.content:
        raise _invalid(f"{label} Asset is not canonical JSON", code=1005)
    return asset, value


def _authoritative_chain_execution(value: Any) -> ChainExecution:
    if not isinstance(value, ChainExecution):
        raise _invalid("P3/Core must provide the verified ChainExecution seam", code=1005)
    if not isinstance(value.chain, Mapping) or not isinstance(value.receipts, tuple) or not value.receipts:
        raise _invalid("authoritative ChainExecution must contain ordered receipts", code=1005)
    if any(not isinstance(receipt, Mapping) for receipt in value.receipts):
        raise _invalid("authoritative ChainExecution contains a non-object receipt", code=1005)
    return value


def _verify_chain_asset(
    reader: HostAssetReader,
    params: Mapping[str, Any],
    execution: ChainExecution,
) -> None:
    asset, value = _canonical_asset(reader, params["chain_asset_id"], params["chain_content_hash"], "Skill chain")
    if not isinstance(value, Mapping):
        raise _invalid("Skill chain Asset must contain an object", code=1005)
    authoritative = _authoritative_chain_execution(execution)
    authoritative_chain = dict(authoritative.chain)
    authoritative_receipts = tuple(dict(receipt) for receipt in authoritative.receipts)
    if [receipt.get("chain_index") for receipt in authoritative_receipts] != list(range(len(authoritative_receipts))):
        raise _invalid("authoritative ChainExecution receipts are not in frozen order", code=1005)
    try:
        # This is deliberately the closed Core contract.  A permissive
        # projection here would allow a canonical object with just chain_id to
        # acquire the authority of a complete chain later in the pipeline.
        assert_valid("skill-chain-result/v1", dict(value))
        verify_skill_chain(authoritative_chain, authoritative_receipts)
    except ContractError as exc:
        raise _invalid(f"Skill chain failed the verified ChainExecution gate: {exc}", code=1005) from exc
    if canonical_bytes(authoritative_chain) != asset.content:
        raise _invalid("Skill chain Asset is not the authoritative ChainExecution", code=1005)
    if value["chain_id"] != params["chain_id"] or value["run_snapshot_hash"] != params["run_snapshot_hash"]:
        raise _invalid("Skill chain identity does not match the execute identity")
    if value["input_hash"] != params["input_content_hash"]:
        raise _invalid("Skill chain input_hash is not bound to the execute input_content_hash", code=1005)
    receipt_ids = value["receipt_ids"]
    receipt_hashes = value["receipt_hashes"]
    if (
        not isinstance(receipt_ids, list)
        or not receipt_ids
        or not isinstance(receipt_hashes, list)
        or len(receipt_ids) != len(receipt_hashes)
        or len(set(receipt_ids)) != len(receipt_ids)
        or receipt_ids != [receipt["receipt_id"] for receipt in authoritative_receipts]
        or receipt_hashes != [receipt["receipt_hash"] for receipt in authoritative_receipts]
    ):
        raise _invalid("Skill chain must contain the complete ordered authoritative receipt projection", code=1005)
    final_hash = value["final_output_hash"] or "-"
    expected_chain_hash = sha256_hex(
        b"skill-chain/v1\n"
        + b"\n".join(receipt_hash.encode("ascii") for receipt_hash in receipt_hashes)
        + f"\n{final_hash}\n".encode("ascii")
    )
    if value["chain_hash"] != expected_chain_hash:
        raise _invalid("Skill chain self-hash does not match its ordered receipts", code=1005)
    if value["chain_status"] in {"succeeded", "partial"}:
        has_bundle = value["result_bundle_id"] is not None and value["result_item_id"] is not None
        has_stream = value["stream_id"] is not None and value["acked_prefix_hash"] is not None
        if has_bundle == has_stream:
            raise _invalid("successful Skill chain must have exactly one complete output anchor", code=1005)


def _expected_chain_ref(result: Mapping[str, Any], execution: ChainExecution) -> dict[str, Any]:
    chain = execution.chain
    return {
        "schema": "skill-chain-ref/v1",
        "chain_result_id": chain["chain_id"],
        "asset_id": result["chain_asset_id"],
        "asset_hash": result["chain_content_hash"],
        "result_bundle_id": chain["result_bundle_id"],
        "result_item_id": chain["result_item_id"],
        "stream_id": chain["stream_id"],
        "acked_prefix_hash": chain["acked_prefix_hash"],
    }


def _verify_bundle_chain_refs(
    bundle: Mapping[str, Any],
    result: Mapping[str, Any],
    execution: ChainExecution,
) -> None:
    references = bundle.get("skill_chain_result_refs")
    expected = [_expected_chain_ref(result, execution)]
    if references != expected:
        raise _invalid("result Bundle skill_chain_result_refs do not exactly bind the authoritative ChainExecution", code=1005)


_BUNDLE_PRODUCER_BINDINGS = (
    ("plugin_id", "plugin_id"),
    ("release_id", "plugin_release_id"),
    ("capability_id", "capability_id"),
    ("job_id", "job_id"),
    ("step_id", "step_id"),
    ("attempt_id", "attempt_id"),
    ("lease_epoch", "lease_epoch"),
)


def _verify_bundle_producer(bundle: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    expected = {producer_field: result[result_field] for producer_field, result_field in _BUNDLE_PRODUCER_BINDINGS}
    if bundle.get("producer") != expected:
        raise _invalid("result Bundle producer is not exactly bound to the execute identity", code=1005)


def _verify_bundle_asset(
    reader: HostAssetReader,
    result: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    execution: ChainExecution,
) -> None:
    identity = result["result_bundle"]
    assert isinstance(identity, Mapping)
    _asset, bundle = _canonical_asset(reader, identity["asset_id"], identity["content_hash"], "result Bundle")
    if not isinstance(bundle, Mapping):
        raise _invalid("result Bundle Asset must contain an object", code=1005)
    verify_result_bundle(bundle, snapshot_workspace_id=snapshot["workspace_id"], snapshot_hash_value=snapshot["snapshot_hash"])
    _verify_bundle_producer(bundle, result)
    if bundle.get("contract_id") != RESULT_CONTRACT or bundle.get("bundle_id") != identity["bundle_id"]:
        raise _invalid("result Bundle Asset does not match its Core identity")
    if bundle.get("input_snapshot_hash") != identity["run_snapshot_hash"]:
        raise _invalid("result Bundle Snapshot binding does not match its Core identity")
    items = {item["item_id"] for item in bundle.get("items", []) if isinstance(item, Mapping) and "item_id" in item}
    if identity["result_item_id"] not in items:
        raise _invalid("result Bundle Asset does not contain the bound result item")
    chain = _authoritative_chain_execution(execution).chain
    if chain["result_bundle_id"] != identity["bundle_id"] or chain["result_item_id"] != identity["result_item_id"]:
        raise _invalid("authoritative Skill chain anchor does not match the Core result Bundle", code=1005)
    _verify_bundle_chain_refs(bundle, result, execution)


_RECEIPT_CONTEXT_FIELDS = (
    "job_id",
    "step_id",
    "attempt_id",
    "lease_epoch",
    "run_snapshot_hash",
    "generation_id",
    "chain_id",
    "chain_index",
    "skill_id",
    "release_id",
    "package_hash",
    "parameters_asset_id",
    "parameters_hash",
    "input_asset_id",
    "input_hash",
)


def _verify_receipt_context(receipt: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    context = receipt["input_context"]
    if not isinstance(context, Mapping):  # decode_model_receipt already checks this; keep the boundary explicit.
        raise _invalid("ModelReceipt input_context must be an object", code=1005)
    missing = [field for field in _RECEIPT_CONTEXT_FIELDS if field not in context]
    if missing:
        raise _invalid(f"ModelReceipt input_context is missing frozen Skill identity: {missing}", code=1005)
    expected = {
        "job_id": result["job_id"],
        "step_id": result["step_id"],
        "attempt_id": result["attempt_id"],
        "lease_epoch": result["lease_epoch"],
        "run_snapshot_hash": result["run_snapshot_hash"],
        "generation_id": result["generation_id"],
        "chain_id": result["chain_id"],
        "parameters_asset_id": result["parameters_asset_id"],
        "parameters_hash": result["parameters_content_hash"],
        "input_asset_id": result["input_asset_id"],
        "input_hash": result["input_content_hash"],
    }
    for field, value in expected.items():
        if type(context[field]) is not type(value) or context[field] != value:
            raise _invalid(f"ModelReceipt input_context {field} is not bound to the execute identity", code=1005)
    if type(context["chain_index"]) is not int or context["chain_index"] < 0:
        raise _invalid("ModelReceipt input_context chain_index is invalid", code=1005)
    chain_index = context["chain_index"]
    skill_releases = result["skill_releases"]
    if chain_index >= len(skill_releases):
        raise _invalid("ModelReceipt input_context chain_index is outside the frozen chain", code=1005)
    expected_skill = skill_releases[chain_index]
    if {
        "skill_id": context["skill_id"],
        "release_id": context["release_id"],
        "package_hash": context["package_hash"],
        "parameters_asset_id": context["parameters_asset_id"],
        "parameters_content_hash": context["parameters_hash"],
    } != {
        "skill_id": expected_skill["skill_id"],
        "release_id": expected_skill["release_id"],
        "package_hash": expected_skill["package_hash"],
        "parameters_asset_id": expected_skill["parameters_asset_id"],
        "parameters_content_hash": expected_skill["parameters_content_hash"],
    }:
        raise _invalid("ModelReceipt input_context Skill release/index is not bound to the frozen chain", code=1005)
    optional_bindings = {
        "workspace_id": result["workspace_id"],
        "plugin_id": result["plugin_id"],
        "plugin_release_id": result["plugin_release_id"],
        "plugin_package_hash": result["plugin_package_hash"],
        "operation_key": result["operation_key"],
        "run_snapshot_id": result["run_snapshot_id"],
        "run_snapshot_asset_id": result["run_snapshot_asset_id"],
        "chain_asset_id": result["chain_asset_id"],
        "chain_content_hash": result["chain_content_hash"],
    }
    for field, value in optional_bindings.items():
        if field in context and context[field] != value:
            raise _invalid(f"ModelReceipt input_context {field} drifted from the execute identity", code=1005)


def _verify_model_receipt_asset(
    reader: HostAssetReader,
    identity: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    asset, value = _canonical_asset(reader, identity["asset_id"], identity["content_hash"], "ModelReceipt")
    if not isinstance(value, Mapping):
        raise _invalid("ModelReceipt Asset must contain an object", code=1005)
    try:
        decoded = decode_model_receipt(value)
        _verify_receipt_context(decoded, result)
        invocation = FrozenModelInvocation(
            invocation_id=decoded["invocation_id"],
            invocation_key=decoded["invocation_key"],
            request_hash=decoded["request_hash"],
            response_asset_id=decoded["response_asset_id"],
            response_hash=decoded["response_hash"],
            profile_revision_id=decoded["profile_revision_id"],
            provider_plugin_id=decoded["provider_plugin_id"],
            provider_release_id=decoded["provider_release_id"],
            input_context=decoded["input_context"],
            endpoint=decoded["endpoint"],
            model=decoded["model"],
            profile_revision=decoded["profile_revision"],
            max_retries=decoded["retry_count"],
            terminal_receipt_id=decoded["receipt_id"],
            terminal_receipt_hash=decoded["receipt_hash"],
        )
        verify_model_receipt_asset(asset, invocation=invocation, expected_context=decoded["input_context"])
    except ContractError as exc:
        raise _invalid(f"ModelReceipt failed the closed decoder/binding gate: {exc}", code=1005) from exc
    if decoded["receipt_id"] != identity["receipt_id"] or decoded["receipt_hash"] != identity["receipt_hash"]:
        raise _invalid("ModelReceipt Asset does not match its Core identity", code=1005)
    if decoded["profile_revision_id"] != identity["model_profile_revision_id"]:
        raise _invalid("ModelReceipt Profile binding does not match its Core identity", code=1005)


def _verify_result_assets(
    result: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    reader: HostAssetReader,
    execution: ChainExecution,
) -> None:
    params = result
    _verify_chain_asset(reader, params, execution)
    if result["status"] != "succeeded":
        return
    output = result["output"]
    receipt = result["model_receipt"]
    bundle = result["result_bundle"]
    if not isinstance(output, Mapping) or not isinstance(receipt, Mapping) or not isinstance(bundle, Mapping):
        raise _invalid("succeeded result is missing a Core artifact identity")
    output_asset = reader(output["asset_id"])
    if output_asset.sha256 != output["content_hash"]:
        raise _invalid("output Asset acknowledgement does not match its Core identity", code=1005)
    _verify_model_receipt_asset(reader, receipt, result)
    _verify_bundle_asset(reader, result, snapshot, execution)


class PromptSkillHostAdapter:
    """Thin P0-v2 composition around the existing ``PromptSkillRuntime``."""

    def __init__(
        self,
        runtime: PromptSkillRuntime,
        *,
        release_id: str,
        host: HostPort | None = None,
        composition: ResultComposition | None = None,
    ) -> None:
        if not isinstance(runtime, PromptSkillRuntime):
            raise TypeError("runtime must be the integrated PromptSkillRuntime")
        _hash(release_id, "release_id")
        if composition is not None and not callable(getattr(composition, "execute", None)):
            raise TypeError("composition must expose the P3/Core execute seam")
        self.runtime = runtime
        self.release_id = release_id
        self.host = host
        self.composition = composition

    def _reader(self) -> HostAssetReader:
        if self.host is None:
            raise _invalid("P3/Core Host composition is unavailable", code=1010)
        return HostAssetReader(self.host)

    def _snapshot(self, request: PromptSkillExecuteRequest) -> dict[str, Any]:
        params = request["params"]
        _asset, value = _canonical_asset(
            self._reader(), params["run_snapshot_asset_id"], params["run_snapshot_hash"], "RunSnapshot"
        )
        if not isinstance(value, Mapping):
            raise _invalid("RunSnapshot Asset must contain an object", code=1005)
        _bind_snapshot(value, params)
        return dict(value)

    def prepare(self, request: PromptSkillExecuteRequest) -> PreparedSkillRun:
        parsed = parse_prompt_skill_execute_request_v2(request)
        params = parsed["params"]
        snapshot = self._snapshot(parsed)
        return self.runtime.prepare(
            job={"job_id": params["job_id"], "step_id": params["step_id"]},
            snapshot=snapshot,
            generation_id=params["generation_id"],
            chain_id=params["chain_id"],
            input_asset_id=params["input_asset_id"],
            anchor=_anchor(params["anchor"]),
        )

    def execute(
        self,
        request: PromptSkillExecuteRequest,
        *,
        authoritative_context: Mapping[str, Any] | None = None,
    ) -> PromptSkillExecuteResult:
        parsed = parse_prompt_skill_execute_request_v2(request)
        if self.composition is None:
            raise _invalid("prompt.skill.execute/v2 requires the P3/Core composition callback", code=1010)
        snapshot = self._snapshot(parsed)
        context = authoritative_context
        if context is None:
            context = self.composition.authoritative_context(parsed)  # type: ignore[attr-defined]
        prepared = self.runtime.prepare(
            job={"job_id": parsed["params"]["job_id"], "step_id": parsed["params"]["step_id"]},
            snapshot=snapshot,
            generation_id=parsed["params"]["generation_id"],
            chain_id=parsed["params"]["chain_id"],
            input_asset_id=parsed["params"]["input_asset_id"],
            anchor=_anchor(parsed["params"]["anchor"]),
        )
        value = self.composition.execute(parsed, prepared, self.runtime)
        if not isinstance(value, Mapping):
            raise _invalid("P3/Core composition returned a non-object result")
        result = validate_prompt_skill_execute_v2(parsed, value, authoritative_context=context)
        chain_execution = getattr(self.composition, "chain_execution", None)
        if not callable(chain_execution):
            raise _invalid("prompt.skill.execute/v2 requires the verified P3/Core ChainExecution seam", code=1010)
        execution = chain_execution(parsed, result)
        if not isinstance(execution, ChainExecution):
            raise _invalid("P3/Core ChainExecution seam returned an unverified projection", code=1005)
        _verify_result_assets(result, snapshot, self._reader(), execution)
        return result

    run = execute

    def bundle_from_execution(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        """Reject the former local Bundle builder; Core must issue the Bundle."""

        raise _invalid("Prompt Skill Host cannot fabricate a Core result Bundle", code=1010)


HostAdapter = PromptSkillHostAdapter


__all__ = [
    "CAPABILITY_ID",
    "INPUT_SCHEMA",
    "MAX_ASSET_BYTES",
    "MAX_ASSET_PAGES",
    "MAX_PAGE_SIZE",
    "OUTPUT_SCHEMA",
    "PLUGIN_ID",
    "RESULT_CONTRACT",
    "HostAdapter",
    "HostAdapterError",
    "HostAssetReader",
    "HostAssetWriter",
    "HostPort",
    "PromptSkillHostAdapter",
    "ResultComposition",
    "capability_descriptor",
]
