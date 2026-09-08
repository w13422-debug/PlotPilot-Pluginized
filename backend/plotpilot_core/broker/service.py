"""Port-driven Capability Broker for the published v1 contracts.

This module intentionally contains orchestration and value objects only.  A
Core implementation is supplied through small structural Protocols.  In
particular, this module does not import the Job/Event authorities and does
not create a second SQLite authority.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
import inspect
from threading import RLock
import re
from types import MappingProxyType
from typing import Any, Protocol, Self, runtime_checkable

try:  # Works from the repository root and with ``backend`` on sys.path.
    from backend.plotpilot_plugin_sdk import (
        ContractError,
        ContractValidationError,
        ErrorCode,
        assert_valid,
        canonical_bytes,
        parse_json_bytes,
        sha256_hex,
        verify_result_bundle,
        verify_snapshot,
    )
    from backend.plotpilot_plugin_sdk.verifier import (
        validate_rpc_result,
        verify_capability_descriptor,
        verify_provenance_receipt,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised by plugin-style imports
    from plotpilot_plugin_sdk import (
        ContractError,
        ContractValidationError,
        ErrorCode,
        assert_valid,
        canonical_bytes,
        parse_json_bytes,
        sha256_hex,
        verify_result_bundle,
        verify_snapshot,
    )
    from plotpilot_plugin_sdk.verifier import (
        validate_rpc_result,
        verify_capability_descriptor,
        verify_provenance_receipt,
    )


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

RESULT_CONTRACTS = frozenset(
    {"candidate-batch/v1", "artifact-bundle/v1", "diagnostic-bundle/v1"}
)
TERMINAL_STATES = frozenset(
    {
        "succeeded",
        "partial",
        "failed",
        "cancelled",
        "needs_attention",
        "interrupted",
        "fenced",
        "terminal",
    }
)
SATISFYING_STATES = frozenset({"succeeded", "partial"})
_TEST_AUTHORITY_SENTINEL = object()

_ENVELOPE_FIELDS = frozenset(
    {
        "schema",
        "invocation_id",
        "parent_job_id",
        "parent_step_id",
        "parent_attempt_id",
        "invoke_operation_key",
        "binding_id",
        "input_asset_id",
        "input_hash",
        "parameters_asset_id",
        "parameters_hash",
        "expected_result_contract",
        "required",
        "propagate_cancel",
    }
)
_CHILD_RECORD_FIELDS = frozenset(
    {
        "child_job_id",
        "parent_job_id",
        "parent_step_id",
        "parent_attempt_id",
        "invoke_operation_key",
        "binding_id",
        "broker_invocation_asset_id",
        "broker_invocation_hash",
        "child_run_snapshot_asset_id",
        "child_run_snapshot_hash",
        "result_contract",
        "required",
        "propagate_cancel",
        "state",
        "result_bundle_asset_id",
        "provenance_receipt_id",
    }
)
_INVOKE_RESULT_FIELDS = frozenset(
    {
        "accepted",
        "child_job_id",
        "child_step_id",
        "child_run_snapshot_asset_id",
        "child_run_snapshot_hash",
        "child_result_contract",
        "child_job_event_seq",
    }
)
_POLL_RESULT_FIELDS = frozenset(
    {
        "job_snapshot_asset_id",
        "job_event_page_asset_id",
        "next_job_event_seq",
        "terminal",
        "result_bundle_asset_id",
        "provenance_receipt_id",
    }
)
_CANCEL_RESULT_FIELDS = frozenset(
    {"accepted", "terminal_known", "child_state", "child_job_event_seq"}
)


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ContractValidationError(f"{label} is not a v1 ID")
    return value


def _require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ContractValidationError(f"{label} must be a lowercase SHA-256")
    return value


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{label} must be a boolean")
    return value


def _require_nonnegative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContractValidationError(f"{label} must be a non-negative integer")
    return value


def _strict_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ContractValidationError(
            f"{label} fields are not closed: missing={missing}, extra={extra}"
        )


def _call_compatible(
    method: Any,
    positional: tuple[Any, ...],
    keyword: Mapping[str, Any],
    *,
    label: str,
) -> Any:
    """Bind a port call before invoking it, without retrying body failures.

    Several first-slice ports have both a keyword-only production spelling
    and a positional test-double spelling.  Retrying after catching
    ``TypeError`` is unsafe: a port can have performed an external mutation
    before raising a business ``TypeError``.  Signature binding selects one
    compatible spelling before the call, so a ``TypeError`` from the body is
    never replayed.
    """

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        # Some extension/builtin callables have no inspectable signature.  A
        # single canonical call is safer than guessing and retrying it.
        return method(*positional)
    keyword = dict(keyword)
    try:
        signature.bind(*positional, **keyword)
    except TypeError:
        try:
            signature.bind(**keyword)
        except TypeError:
            try:
                signature.bind(*positional)
            except TypeError as exc:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    f"{label} port has an incompatible call signature",
                ) from exc
            return method(*positional)
        return method(**keyword)
    return method(*positional, **keyword)


def _freeze(value: Any) -> Any:
    """Recursively freeze a JSON-shaped value for a value-object boundary."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ContractValidationError("value is not JSON-shaped")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class _PayloadMapping(Mapping[str, Any]):
    """Small immutable DTO mixin with both attribute and mapping access."""

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_dict())


@dataclass(frozen=True, slots=True)
class CallerAttemptContext:
    """The parent Attempt identity presented to every Broker operation."""

    parent_job_id: str
    parent_step_id: str
    parent_attempt_id: str
    lease_epoch: int
    context_identity: str | None = None
    generation_id: str | None = None
    plugin_release_id: str | None = None
    fresh: bool = True
    lease_valid: bool = True

    def __post_init__(self) -> None:
        _require_id(self.parent_job_id, "parent_job_id")
        _require_id(self.parent_step_id, "parent_step_id")
        _require_id(self.parent_attempt_id, "parent_attempt_id")
        if not isinstance(self.lease_epoch, int) or isinstance(self.lease_epoch, bool) or self.lease_epoch < 1:
            raise ContractValidationError("lease_epoch must be a positive integer")
        if self.context_identity is not None:
            _require_id(self.context_identity, "context_identity")
        if self.generation_id is not None:
            _require_id(self.generation_id, "generation_id")
        if self.plugin_release_id is not None:
            _require_hash(self.plugin_release_id, "plugin_release_id")
        _require_bool(self.fresh, "fresh")
        _require_bool(self.lease_valid, "lease_valid")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise ContractValidationError("caller attempt context must be an object")
        return cls(
            parent_job_id=value.get("parent_job_id", value.get("job_id")),
            parent_step_id=value.get("parent_step_id", value.get("step_id")),
            parent_attempt_id=value.get("parent_attempt_id", value.get("attempt_id")),
            lease_epoch=value.get("lease_epoch"),
            context_identity=value.get("context_identity"),
            generation_id=value.get("generation_id"),
            plugin_release_id=value.get("plugin_release_id"),
            fresh=value.get("fresh", value.get("is_fresh", value.get("freshness", True))),
            lease_valid=value.get("lease_valid", True),
        )

    @property
    def job_id(self) -> str:
        return self.parent_job_id

    @property
    def step_id(self) -> str:
        return self.parent_step_id

    @property
    def attempt_id(self) -> str:
        return self.parent_attempt_id

    def identity(self) -> str:
        if self.context_identity is not None:
            return self.context_identity
        # Lease epoch is deliberately absent: a retry after ACK loss must
        # resolve the same operation row, while the injected port fences the
        # current epoch before this identity is consumed.
        return sha256_hex(
            b"broker-attempt-context/v1\n"
            + canonical_bytes(
                {
                    "parent_job_id": self.parent_job_id,
                    "parent_step_id": self.parent_step_id,
                    "parent_attempt_id": self.parent_attempt_id,
                }
            )
        )


@runtime_checkable
class AttemptContextPort(Protocol):
    """Authoritative Attempt lease/freshness fence."""

    def validate_attempt(self, context: CallerAttemptContext) -> None: ...


@dataclass
class InMemoryAttemptContextPort:
    """Deterministic test fence; production supplies the P3 Attempt port."""

    current: dict[tuple[str, str, str], int] = field(default_factory=dict)

    def validate_attempt(self, context: CallerAttemptContext) -> None:
        if not context.fresh or not context.lease_valid:
            raise ContractError(ErrorCode.STALE_LEASE, "caller Attempt context is stale")
        key = (context.parent_job_id, context.parent_step_id, context.parent_attempt_id)
        expected = self.current.get(key)
        if expected is not None and expected != context.lease_epoch:
            raise ContractError(ErrorCode.STALE_LEASE, "caller Attempt lease epoch is stale")


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor(_PayloadMapping):
    """Validated immutable projection of ``capability-provider/v1``."""

    capability_id: str
    plugin_id: str
    release_id: str
    input_schema: str
    output_schema: str
    result_contract: str
    supports: tuple[str, ...]
    deterministic: bool
    accepted_data_formats: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_id(self.capability_id, "capability_id")
        _require_id(self.plugin_id, "provider.plugin_id")
        _require_id(self.release_id, "provider.release_id")
        _require_id(self.input_schema, "input_schema")
        _require_id(self.output_schema, "output_schema")
        if self.result_contract not in RESULT_CONTRACTS:
            raise ContractValidationError("descriptor result_contract is not a v1 profile")
        if (
            not isinstance(self.supports, tuple)
            or any(not isinstance(value, str) for value in self.supports)
            or len(set(self.supports)) != len(self.supports)
        ):
            raise ContractValidationError("descriptor supports must be a unique tuple")
        if not isinstance(self.deterministic, bool):
            raise ContractValidationError("descriptor deterministic must be a boolean")
        if (
            not isinstance(self.accepted_data_formats, tuple)
            or any(not isinstance(value, str) for value in self.accepted_data_formats)
            or len(set(self.accepted_data_formats)) != len(self.accepted_data_formats)
        ):
            raise ContractValidationError("descriptor accepted_data_formats must be unique")
        for value in self.supports:
            if value not in {"run", "resume", "cancel", "validate"}:
                raise ContractValidationError("descriptor supports contains an unknown operation")
        for value in self.accepted_data_formats:
            _require_id(value, "accepted_data_formats[]")

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        expected_capability_id: str | None = None,
        expected_plugin_id: str | None = None,
        expected_release_id: str | None = None,
    ) -> Self:
        raw = dict(value)
        verify_capability_descriptor(
            raw,
            expected_capability_id=expected_capability_id,
            expected_provider=(
                {"plugin_id": expected_plugin_id, "release_id": expected_release_id}
                if expected_plugin_id is not None and expected_release_id is not None
                else None
            ),
        )
        return cls(
            capability_id=raw["capability_id"],
            plugin_id=raw["provider"]["plugin_id"],
            release_id=raw["provider"]["release_id"],
            input_schema=raw["input_schema"],
            output_schema=raw["output_schema"],
            result_contract=raw["result_contract"],
            supports=tuple(raw["supports"]),
            deterministic=raw["deterministic"],
            accepted_data_formats=tuple(raw["accepted_data_formats"]),
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema": "capability-provider/v1",
            "capability_id": self.capability_id,
            "provider": {"plugin_id": self.plugin_id, "release_id": self.release_id},
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "result_contract": self.result_contract,
            "supports": list(self.supports),
            "deterministic": self.deterministic,
            "accepted_data_formats": list(self.accepted_data_formats),
        }
        verify_capability_descriptor(value, expected_capability_id=self.capability_id)
        return value


@dataclass(frozen=True, slots=True)
class CapabilityBinding(_PayloadMapping):
    """Frozen Plan binding; callers may only mirror, never override it."""

    binding_id: str
    capability_id: str
    plugin_id: str
    release_requirement: str
    result_contract: str
    required: bool
    propagate_cancel: bool
    descriptor: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _require_id(self.binding_id, "binding_id")
        _require_id(self.capability_id, "capability_id")
        _require_id(self.plugin_id, "plugin_id")
        if not isinstance(self.release_requirement, str) or _SEMVER.fullmatch(self.release_requirement) is None:
            raise ContractValidationError("release_requirement must be an exact SemVer")
        if self.result_contract not in RESULT_CONTRACTS:
            raise ContractValidationError("binding result_contract is not a v1 profile")
        _require_bool(self.required, "required")
        _require_bool(self.propagate_cancel, "propagate_cancel")
        if self.descriptor is not None:
            frozen = _freeze(dict(self.descriptor))
            if not isinstance(frozen, Mapping):
                raise ContractValidationError("binding descriptor must be an object")
            object.__setattr__(self, "descriptor", frozen)
            descriptor = CapabilityDescriptor.from_mapping(
                _thaw(frozen), expected_capability_id=self.capability_id
            )
            if descriptor.plugin_id != self.plugin_id:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "binding plugin_id does not match descriptor provider",
                )
            if descriptor.result_contract != self.result_contract:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "binding result_contract does not match descriptor",
                )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Self:
        required = {
            "binding_id",
            "capability_id",
            "plugin_id",
            "release_requirement",
            "result_contract",
            "required",
            "propagate_cancel",
        }
        _strict_fields(value, frozenset(required) | {"descriptor"}, "capability binding") if "descriptor" in value else _strict_fields(value, frozenset(required), "capability binding")
        return cls(**dict(value))

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "binding_id": self.binding_id,
            "capability_id": self.capability_id,
            "plugin_id": self.plugin_id,
            "release_requirement": self.release_requirement,
            "result_contract": self.result_contract,
            "required": self.required,
            "propagate_cancel": self.propagate_cancel,
        }
        if self.descriptor is not None:
            value["descriptor"] = _thaw(self.descriptor)
        return value


BrokerBinding = CapabilityBinding


@dataclass(frozen=True, slots=True)
class BrokerInvocationEnvelope(_PayloadMapping):
    """Exact immutable ``broker-invocation-v1`` value object.

    The envelope itself has no extra envelope-hash field in the published
    contract.  ``asset_hash`` is the raw SHA-256 of its canonical bytes and
    is used by the BrokerChildRecord and child snapshot binding.
    """

    invocation_id: str
    parent_job_id: str
    parent_step_id: str
    parent_attempt_id: str
    invoke_operation_key: str
    binding_id: str
    input_asset_id: str
    input_hash: str
    parameters_asset_id: str | None
    parameters_hash: str | None
    expected_result_contract: str
    required: bool
    propagate_cancel: bool
    schema: str = "broker-invocation/v1"

    def __post_init__(self) -> None:
        if self.schema != "broker-invocation/v1":
            raise ContractValidationError("broker invocation schema must be broker-invocation/v1")
        _require_id(self.invocation_id, "invocation_id")
        _require_id(self.parent_job_id, "parent_job_id")
        _require_id(self.parent_step_id, "parent_step_id")
        _require_id(self.parent_attempt_id, "parent_attempt_id")
        _require_id(self.invoke_operation_key, "invoke_operation_key")
        _require_id(self.binding_id, "binding_id")
        _require_id(self.input_asset_id, "input_asset_id")
        _require_hash(self.input_hash, "input_hash")
        if self.parameters_asset_id is None:
            if self.parameters_hash is not None:
                raise ContractValidationError("parameters_asset_id/hash must be null together")
        else:
            _require_id(self.parameters_asset_id, "parameters_asset_id")
            _require_hash(self.parameters_hash, "parameters_hash")
        if self.expected_result_contract not in RESULT_CONTRACTS:
            raise ContractValidationError("expected_result_contract is not a v1 profile")
        _require_bool(self.required, "required")
        _require_bool(self.propagate_cancel, "propagate_cancel")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Self:
        _strict_fields(value, _ENVELOPE_FIELDS, "broker-invocation-v1")
        raw = dict(value)
        assert_valid("broker-invocation-v1", raw)
        return cls(
            invocation_id=raw["invocation_id"],
            parent_job_id=raw["parent_job_id"],
            parent_step_id=raw["parent_step_id"],
            parent_attempt_id=raw["parent_attempt_id"],
            invoke_operation_key=raw["invoke_operation_key"],
            binding_id=raw["binding_id"],
            input_asset_id=raw["input_asset_id"],
            input_hash=raw["input_hash"],
            parameters_asset_id=raw["parameters_asset_id"],
            parameters_hash=raw["parameters_hash"],
            expected_result_contract=raw["expected_result_contract"],
            required=raw["required"],
            propagate_cancel=raw["propagate_cancel"],
            schema=raw["schema"],
        )

    @classmethod
    def from_assets(
        cls,
        *,
        invocation_id: str,
        parent: CallerAttemptContext,
        invoke_operation_key: str,
        binding: CapabilityBinding,
        input_asset_id: str,
        input_asset_bytes: bytes,
        parameters_asset_bytes: bytes | None = None,
        parameters_asset_id: str | None = None,
    ) -> Self:
        if (parameters_asset_bytes is None) != (parameters_asset_id is None):
            raise ContractValidationError("parameters asset ID and bytes must be both present or null")
        return cls(
            invocation_id=invocation_id,
            parent_job_id=parent.parent_job_id,
            parent_step_id=parent.parent_step_id,
            parent_attempt_id=parent.parent_attempt_id,
            invoke_operation_key=invoke_operation_key,
            binding_id=binding.binding_id,
            input_asset_id=_require_id(input_asset_id, "input_asset_id"),
            input_hash=sha256_hex(bytes(input_asset_bytes)),
            parameters_asset_id=parameters_asset_id,
            parameters_hash=(sha256_hex(bytes(parameters_asset_bytes)) if parameters_asset_bytes is not None else None),
            expected_result_contract=binding.result_contract,
            required=binding.required,
            propagate_cancel=binding.propagate_cancel,
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "invocation_id": self.invocation_id,
            "parent_job_id": self.parent_job_id,
            "parent_step_id": self.parent_step_id,
            "parent_attempt_id": self.parent_attempt_id,
            "invoke_operation_key": self.invoke_operation_key,
            "binding_id": self.binding_id,
            "input_asset_id": self.input_asset_id,
            "input_hash": self.input_hash,
            "parameters_asset_id": self.parameters_asset_id,
            "parameters_hash": self.parameters_hash,
            "expected_result_contract": self.expected_result_contract,
            "required": self.required,
            "propagate_cancel": self.propagate_cancel,
        }
        assert_valid("broker-invocation-v1", value)
        return value

    @property
    def asset_hash(self) -> str:
        return sha256_hex(self.canonical_bytes())

    @property
    def envelope_hash(self) -> str:
        return self.asset_hash

    @property
    def hash(self) -> str:
        return self.asset_hash


InvocationEnvelope = BrokerInvocationEnvelope


@dataclass(frozen=True, slots=True)
class BrokerChildRecord(_PayloadMapping):
    """Exact immutable ``broker-child-record-v1`` value object."""

    child_job_id: str
    parent_job_id: str
    parent_step_id: str
    parent_attempt_id: str
    invoke_operation_key: str
    binding_id: str
    broker_invocation_asset_id: str
    broker_invocation_hash: str
    child_run_snapshot_asset_id: str
    child_run_snapshot_hash: str
    result_contract: str
    required: bool
    propagate_cancel: bool
    state: str
    result_bundle_asset_id: str | None
    provenance_receipt_id: str | None

    def __post_init__(self) -> None:
        for field_name in (
            "child_job_id",
            "parent_job_id",
            "parent_step_id",
            "parent_attempt_id",
            "invoke_operation_key",
            "binding_id",
            "broker_invocation_asset_id",
            "child_run_snapshot_asset_id",
            "state",
        ):
            _require_id(getattr(self, field_name), field_name)
        _require_hash(self.broker_invocation_hash, "broker_invocation_hash")
        _require_hash(self.child_run_snapshot_hash, "child_run_snapshot_hash")
        if self.result_contract not in RESULT_CONTRACTS:
            raise ContractValidationError("child result_contract is not a v1 profile")
        _require_bool(self.required, "required")
        _require_bool(self.propagate_cancel, "propagate_cancel")
        if self.result_bundle_asset_id is not None:
            _require_id(self.result_bundle_asset_id, "result_bundle_asset_id")
        if self.provenance_receipt_id is not None:
            _require_id(self.provenance_receipt_id, "provenance_receipt_id")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Self:
        _strict_fields(value, _CHILD_RECORD_FIELDS, "broker-child-record-v1")
        raw = dict(value)
        assert_valid("broker-child-record-v1", raw)
        return cls(
            child_job_id=raw["child_job_id"],
            parent_job_id=raw["parent_job_id"],
            parent_step_id=raw["parent_step_id"],
            parent_attempt_id=raw["parent_attempt_id"],
            invoke_operation_key=raw["invoke_operation_key"],
            binding_id=raw["binding_id"],
            broker_invocation_asset_id=raw["broker_invocation_asset_id"],
            broker_invocation_hash=raw["broker_invocation_hash"],
            child_run_snapshot_asset_id=raw["child_run_snapshot_asset_id"],
            child_run_snapshot_hash=raw["child_run_snapshot_hash"],
            result_contract=raw["result_contract"],
            required=raw["required"],
            propagate_cancel=raw["propagate_cancel"],
            state=raw["state"],
            result_bundle_asset_id=raw["result_bundle_asset_id"],
            provenance_receipt_id=raw["provenance_receipt_id"],
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "child_job_id": self.child_job_id,
            "parent_job_id": self.parent_job_id,
            "parent_step_id": self.parent_step_id,
            "parent_attempt_id": self.parent_attempt_id,
            "invoke_operation_key": self.invoke_operation_key,
            "binding_id": self.binding_id,
            "broker_invocation_asset_id": self.broker_invocation_asset_id,
            "broker_invocation_hash": self.broker_invocation_hash,
            "child_run_snapshot_asset_id": self.child_run_snapshot_asset_id,
            "child_run_snapshot_hash": self.child_run_snapshot_hash,
            "result_contract": self.result_contract,
            "required": self.required,
            "propagate_cancel": self.propagate_cancel,
            "state": self.state,
            "result_bundle_asset_id": self.result_bundle_asset_id,
            "provenance_receipt_id": self.provenance_receipt_id,
        }
        assert_valid("broker-child-record-v1", value)
        return value

    def with_projection(
        self,
        *,
        state: str | None = None,
        result_bundle_asset_id: str | None | object = ...,
        provenance_receipt_id: str | None | object = ...,
    ) -> "BrokerChildRecord":
        return BrokerChildRecord(
            child_job_id=self.child_job_id,
            parent_job_id=self.parent_job_id,
            parent_step_id=self.parent_step_id,
            parent_attempt_id=self.parent_attempt_id,
            invoke_operation_key=self.invoke_operation_key,
            binding_id=self.binding_id,
            broker_invocation_asset_id=self.broker_invocation_asset_id,
            broker_invocation_hash=self.broker_invocation_hash,
            child_run_snapshot_asset_id=self.child_run_snapshot_asset_id,
            child_run_snapshot_hash=self.child_run_snapshot_hash,
            result_contract=self.result_contract,
            required=self.required,
            propagate_cancel=self.propagate_cancel,
            state=self.state if state is None else state,
            result_bundle_asset_id=(
                self.result_bundle_asset_id
                if result_bundle_asset_id is ...
                else result_bundle_asset_id
            ),
            provenance_receipt_id=(
                self.provenance_receipt_id
                if provenance_receipt_id is ...
                else provenance_receipt_id
            ),
        )


@dataclass(frozen=True, slots=True)
class BrokerInvokeResult(_PayloadMapping):
    accepted: bool
    child_job_id: str
    child_step_id: str
    child_run_snapshot_asset_id: str
    child_run_snapshot_hash: str
    child_result_contract: str
    child_job_event_seq: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "child_job_id": self.child_job_id,
            "child_step_id": self.child_step_id,
            "child_run_snapshot_asset_id": self.child_run_snapshot_asset_id,
            "child_run_snapshot_hash": self.child_run_snapshot_hash,
            "child_result_contract": self.child_result_contract,
            "child_job_event_seq": self.child_job_event_seq,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Self:
        _strict_fields(value, _INVOKE_RESULT_FIELDS, "host.capability.invoke/v1 result")
        raw = dict(value)
        validate_rpc_result("host.capability.invoke/v1", raw)
        _require_bool(raw["accepted"], "accepted")
        _require_id(raw["child_job_id"], "child_job_id")
        _require_id(raw["child_step_id"], "child_step_id")
        _require_id(raw["child_run_snapshot_asset_id"], "child_run_snapshot_asset_id")
        _require_hash(raw["child_run_snapshot_hash"], "child_run_snapshot_hash")
        if raw["child_result_contract"] not in RESULT_CONTRACTS:
            raise ContractValidationError("child_result_contract is not a v1 profile")
        _require_nonnegative_int(raw["child_job_event_seq"], "child_job_event_seq")
        return cls(**raw)


InvokeResult = BrokerInvokeResult


@dataclass(frozen=True, slots=True)
class BrokerPollProjection(_PayloadMapping):
    job_snapshot_asset_id: str
    job_event_page_asset_id: str
    next_job_event_seq: int
    terminal: bool
    result_bundle_asset_id: str | None
    provenance_receipt_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_snapshot_asset_id": self.job_snapshot_asset_id,
            "job_event_page_asset_id": self.job_event_page_asset_id,
            "next_job_event_seq": self.next_job_event_seq,
            "terminal": self.terminal,
            "result_bundle_asset_id": self.result_bundle_asset_id,
            "provenance_receipt_id": self.provenance_receipt_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Self:
        _strict_fields(value, _POLL_RESULT_FIELDS, "host.capability.poll/v1 result")
        raw = dict(value)
        validate_rpc_result("host.capability.poll/v1", raw)
        _require_id(raw["job_snapshot_asset_id"], "job_snapshot_asset_id")
        _require_id(raw["job_event_page_asset_id"], "job_event_page_asset_id")
        _require_nonnegative_int(raw["next_job_event_seq"], "next_job_event_seq")
        _require_bool(raw["terminal"], "terminal")
        if raw["result_bundle_asset_id"] is not None:
            _require_id(raw["result_bundle_asset_id"], "result_bundle_asset_id")
        if raw["provenance_receipt_id"] is not None:
            _require_id(raw["provenance_receipt_id"], "provenance_receipt_id")
        return cls(**raw)


PollResult = BrokerPollProjection


@dataclass(frozen=True, slots=True)
class BrokerCancelResult(_PayloadMapping):
    accepted: bool
    terminal_known: bool
    child_state: str
    child_job_event_seq: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "terminal_known": self.terminal_known,
            "child_state": self.child_state,
            "child_job_event_seq": self.child_job_event_seq,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Self:
        _strict_fields(value, _CANCEL_RESULT_FIELDS, "host.capability.cancel/v1 result")
        raw = dict(value)
        validate_rpc_result("host.capability.cancel/v1", raw)
        _require_bool(raw["accepted"], "accepted")
        _require_bool(raw["terminal_known"], "terminal_known")
        _require_id(raw["child_state"], "child_state")
        _require_nonnegative_int(raw["child_job_event_seq"], "child_job_event_seq")
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ChildCreationRequest:
    """Explicit request passed to the injected child factory."""

    envelope_asset_id: str
    envelope: BrokerInvocationEnvelope
    input_asset_id: str
    parameters_asset_id: str | None
    binding: CapabilityBinding
    descriptor: CapabilityDescriptor | None
    plugin_release_id: str
    generation_id: str
    caller: CallerAttemptContext

    def __post_init__(self) -> None:
        _require_id(self.envelope_asset_id, "envelope_asset_id")
        if self.envelope.binding_id != self.binding.binding_id:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child request binding mismatch")
        if self.envelope.input_asset_id != self.input_asset_id:
            raise ContractError(ErrorCode.ASSET_ERROR, "child request input asset mismatch")
        if self.envelope.parameters_asset_id != self.parameters_asset_id:
            raise ContractError(ErrorCode.ASSET_ERROR, "child request parameters asset mismatch")
        _require_id(self.plugin_release_id, "plugin_release_id")
        _require_id(self.generation_id, "generation_id")


@dataclass(frozen=True, slots=True)
class ChildCreationResult:
    """Factory result with explicit snapshot/binding attestation."""

    child_job_id: str
    child_step_id: str
    child_attempt_id: str
    child_lease_epoch: int
    child_plugin_release_id: str
    child_run_snapshot_asset_id: str
    child_run_snapshot_hash: str
    child_job_event_seq: int = 0
    state: str = "queued"
    child_run_snapshot: Mapping[str, Any] | None = None
    binding_attestation: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _require_id(self.child_job_id, "child_job_id")
        _require_id(self.child_step_id, "child_step_id")
        _require_id(self.child_attempt_id, "child_attempt_id")
        if (
            not isinstance(self.child_lease_epoch, int)
            or isinstance(self.child_lease_epoch, bool)
            or self.child_lease_epoch < 1
        ):
            raise ContractValidationError("child_lease_epoch must be a positive integer")
        _require_id(self.child_plugin_release_id, "child_plugin_release_id")
        _require_id(self.child_run_snapshot_asset_id, "child_run_snapshot_asset_id")
        _require_hash(self.child_run_snapshot_hash, "child_run_snapshot_hash")
        _require_nonnegative_int(self.child_job_event_seq, "child_job_event_seq")
        _require_id(self.state, "state")
        if self.child_run_snapshot is not None and not isinstance(self.child_run_snapshot, Mapping):
            raise ContractValidationError("child_run_snapshot must be an object")
        if self.child_run_snapshot is not None:
            object.__setattr__(self, "child_run_snapshot", _freeze(self.child_run_snapshot))
        if self.binding_attestation is not None and not isinstance(self.binding_attestation, Mapping):
            raise ContractValidationError("binding_attestation must be an object")
        if self.binding_attestation is not None:
            object.__setattr__(self, "binding_attestation", _freeze(self.binding_attestation))

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_job_id": self.child_job_id,
            "child_step_id": self.child_step_id,
            "child_attempt_id": self.child_attempt_id,
            "child_lease_epoch": self.child_lease_epoch,
            "child_plugin_release_id": self.child_plugin_release_id,
            "child_run_snapshot_asset_id": self.child_run_snapshot_asset_id,
            "child_run_snapshot_hash": self.child_run_snapshot_hash,
            "child_job_event_seq": self.child_job_event_seq,
            "state": self.state,
            "child_run_snapshot": (
                _thaw(self.child_run_snapshot) if self.child_run_snapshot is not None else None
            ),
            "binding_attestation": (
                _thaw(self.binding_attestation) if self.binding_attestation is not None else None
            ),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Self:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ContractValidationError("child factory result must be an object")
        snapshot = value.get("child_run_snapshot", value.get("snapshot"))
        attestation = value.get(
            "binding_attestation",
            value.get("snapshot_binding", value.get("child_snapshot_binding")),
        )
        return cls(
            child_job_id=value.get("child_job_id"),
            child_step_id=value.get("child_step_id"),
            child_attempt_id=value.get("child_attempt_id"),
            child_lease_epoch=value.get("child_lease_epoch"),
            child_plugin_release_id=value.get("child_plugin_release_id"),
            child_run_snapshot_asset_id=value.get("child_run_snapshot_asset_id"),
            child_run_snapshot_hash=value.get("child_run_snapshot_hash"),
            child_job_event_seq=value.get("child_job_event_seq", 0),
            state=value.get("state", value.get("child_state", "queued")),
            child_run_snapshot=snapshot,
            binding_attestation=attestation,
        )


@runtime_checkable
class BrokerCoreAssetPort(Protocol):
    def read_asset(self, asset_id: str) -> bytes: ...

    def create_asset(self, content: bytes, *, mime: str) -> str: ...


@runtime_checkable
class BrokerExecutionPort(Protocol):
    def start(self, capability_id: str, snapshot_asset_id: str) -> str: ...

    def poll(self, job_id: str, after_event_seq: int) -> Any: ...

    def cancel(self, operation_key: str, job_id: str) -> Any: ...


@runtime_checkable
class BrokerChildFactoryPort(Protocol):
    def create_or_recover_child(
        self, request: ChildCreationRequest
    ) -> ChildCreationResult | Mapping[str, Any]: ...


@runtime_checkable
class BrokerChildSnapshotPort(Protocol):
    """Projects execution observations to durable Asset IDs.

    The projection is deliberately separate from ``ExecutionPort.poll``:
    the latter returns the public P3 execution observation, while this adapter
    supplies already durable Core snapshot/event/result/receipt references.
    """

    def project_poll(
        self,
        *,
        child_job_id: str,
        after_job_event_seq: int,
        execution_terminal: bool,
        execution_events: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class BrokerDescriptorPort(Protocol):
    def describe_capability(self, capability_id: str) -> Mapping[str, Any] | CapabilityDescriptor: ...


@runtime_checkable
class BrokerReceiptPort(Protocol):
    def read_receipt(self, receipt_id: str) -> Mapping[str, Any]: ...


@runtime_checkable
class BrokerOperationLedgerPort(Protocol):
    def lookup(self, *, context_identity: str, method: str, operation_key: str) -> Any: ...

    def get_reservation(
        self, *, context_identity: str, method: str, operation_key: str
    ) -> Any: ...

    def reserve(
        self,
        *,
        context_identity: str,
        method: str,
        operation_key: str,
        payload_hash: str,
    ) -> Any: ...

    def attach_envelope(
        self,
        *,
        context_identity: str,
        method: str,
        operation_key: str,
        payload_hash: str,
        envelope_asset_id: str,
    ) -> Any: ...

    def attach_child(
        self,
        *,
        context_identity: str,
        method: str,
        operation_key: str,
        payload_hash: str,
        child_creation: Mapping[str, Any],
    ) -> Any: ...

    def record(
        self,
        *,
        context_identity: str,
        method: str,
        operation_key: str,
        payload_hash: str,
        response: bytes,
    ) -> None: ...


@runtime_checkable
class BrokerChildRecordPort(Protocol):
    def get_by_operation(self, *, context_identity: str, operation_key: str) -> Any: ...

    def get_by_child_job(self, child_job_id: str) -> Any: ...

    def save(self, record: BrokerChildRecord) -> None: ...

    def replace(self, record: BrokerChildRecord) -> None: ...


BrokerChildRecordStore = BrokerChildRecordPort


@dataclass(frozen=True, slots=True)
class BrokerLedgerEntry:
    context_identity: str
    method: str
    operation_key: str
    payload_hash: str
    response: bytes


@dataclass(frozen=True, slots=True)
class BrokerOperationReservation:
    """Durable pre-child reservation for one invoke operation.

    Production adapters persist this record in the P1-owned transaction.  It
    deliberately carries the factory's complete child identity so a retry can
    revalidate and finish the original invocation instead of creating a
    second child after a post-factory failure.
    """

    context_identity: str
    method: str
    operation_key: str
    payload_hash: str
    envelope_asset_id: str | None = None
    child_creation: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _require_id(self.context_identity, "reservation.context_identity")
        _require_id(self.method, "reservation.method")
        _require_id(self.operation_key, "reservation.operation_key")
        _require_hash(self.payload_hash, "reservation.payload_hash")
        if self.envelope_asset_id is not None:
            _require_id(self.envelope_asset_id, "reservation.envelope_asset_id")
        if self.child_creation is not None:
            if not isinstance(self.child_creation, Mapping):
                raise ContractValidationError("reservation child_creation must be an object")
            frozen = _freeze(self.child_creation)
            object.__setattr__(self, "child_creation", frozen)
            ChildCreationResult.from_mapping(_thaw(frozen))

    @classmethod
    def from_value(cls, value: Any) -> "BrokerOperationReservation | None":
        if value is None or isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ContractError(ErrorCode.ASSET_ERROR, "operation reservation is not an object")
        return cls(
            context_identity=value.get("context_identity"),
            method=value.get("method"),
            operation_key=value.get("operation_key"),
            payload_hash=value.get("payload_hash"),
            envelope_asset_id=value.get("envelope_asset_id"),
            child_creation=value.get("child_creation"),
        )

    def with_envelope(self, envelope_asset_id: str) -> "BrokerOperationReservation":
        return BrokerOperationReservation(
            self.context_identity,
            self.method,
            self.operation_key,
            self.payload_hash,
            envelope_asset_id,
            self.child_creation,
        )

    def with_child(self, child_creation: Mapping[str, Any]) -> "BrokerOperationReservation":
        return BrokerOperationReservation(
            self.context_identity,
            self.method,
            self.operation_key,
            self.payload_hash,
            self.envelope_asset_id,
            child_creation,
        )


class InMemoryBrokerOperationLedger:
    """Non-authoritative deterministic ledger for tests and local adapters."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], BrokerLedgerEntry] = {}
        self._reservations: dict[
            tuple[str, str, str], BrokerOperationReservation
        ] = {}
        self._lock = RLock()

    def lookup(self, *, context_identity: str, method: str, operation_key: str) -> BrokerLedgerEntry | None:
        with self._lock:
            return self._rows.get((context_identity, method, operation_key))

    def get_reservation(
        self, *, context_identity: str, method: str, operation_key: str
    ) -> BrokerOperationReservation | None:
        with self._lock:
            return self._reservations.get((context_identity, method, operation_key))

    def reserve(
        self,
        *,
        context_identity: str,
        method: str,
        operation_key: str,
        payload_hash: str,
    ) -> BrokerOperationReservation:
        key = (context_identity, method, operation_key)
        reservation = BrokerOperationReservation(
            context_identity, method, operation_key, payload_hash
        )
        with self._lock:
            committed = self._rows.get(key)
            if committed is not None and committed.payload_hash != payload_hash:
                raise ContractError(
                    ErrorCode.DUPLICATE_REQUEST,
                    "operation key reused with a different payload",
                )
            current = self._reservations.get(key)
            if current is not None:
                if current.payload_hash != payload_hash:
                    raise ContractError(
                        ErrorCode.DUPLICATE_REQUEST,
                        "operation reservation payload drift",
                    )
                return current
            self._reservations[key] = reservation
            return reservation

    def attach_envelope(
        self,
        *,
        context_identity: str,
        method: str,
        operation_key: str,
        payload_hash: str,
        envelope_asset_id: str,
    ) -> BrokerOperationReservation:
        key = (context_identity, method, operation_key)
        with self._lock:
            current = self._reservations.get(key)
            if current is None or current.payload_hash != payload_hash:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "operation must be durably reserved before envelope creation",
                )
            if (
                current.envelope_asset_id is not None
                and current.envelope_asset_id != envelope_asset_id
            ):
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "operation reservation envelope drift",
                )
            updated = current.with_envelope(envelope_asset_id)
            self._reservations[key] = updated
            return updated

    def attach_child(
        self,
        *,
        context_identity: str,
        method: str,
        operation_key: str,
        payload_hash: str,
        child_creation: Mapping[str, Any],
    ) -> BrokerOperationReservation:
        key = (context_identity, method, operation_key)
        with self._lock:
            current = self._reservations.get(key)
            if current is None or current.payload_hash != payload_hash:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "operation must be durably reserved before child creation",
                )
            candidate = ChildCreationResult.from_mapping(child_creation).to_dict()
            if current.child_creation is not None:
                if _thaw(current.child_creation) != candidate:
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "operation reservation child identity drift",
                    )
                return current
            updated = current.with_child(candidate)
            self._reservations[key] = updated
            return updated

    def record(
        self,
        *,
        context_identity: str,
        method: str,
        operation_key: str,
        payload_hash: str,
        response: bytes,
    ) -> None:
        if not isinstance(response, bytes):
            raise ContractError(ErrorCode.ASSET_ERROR, "broker ledger response must be bytes")
        key = (context_identity, method, operation_key)
        entry = BrokerLedgerEntry(context_identity, method, operation_key, payload_hash, bytes(response))
        with self._lock:
            reservation = self._reservations.get(key)
            if method == "host.capability.invoke/v1":
                if reservation is None or reservation.child_creation is None:
                    raise ContractError(
                        ErrorCode.INVALID_TRANSITION,
                        "invoke response cannot commit before durable child reservation",
                    )
                if reservation.payload_hash != payload_hash:
                    raise ContractError(
                        ErrorCode.DUPLICATE_REQUEST,
                        "operation reservation payload drift",
                    )
            current = self._rows.get(key)
            if current is not None:
                if current.payload_hash != payload_hash:
                    raise ContractError(ErrorCode.DUPLICATE_REQUEST, "operation key reused with a different payload")
                if current.response != response:
                    raise ContractError(ErrorCode.DUPLICATE_REQUEST, "operation key response drift")
                return
            self._rows[key] = entry

    @property
    def rows(self) -> Mapping[tuple[str, str, str], BrokerLedgerEntry]:
        with self._lock:
            return MappingProxyType(dict(self._rows))


InMemoryBrokerLedger = InMemoryBrokerOperationLedger


class InMemoryBrokerChildRecordStore:
    """Test-only child record store; no Job or SQLite authority is created."""

    def __init__(self) -> None:
        self._by_operation: dict[tuple[str, str], BrokerChildRecord] = {}
        self._by_job: dict[str, BrokerChildRecord] = {}
        self._lock = RLock()

    def get_by_operation(self, *, context_identity: str, operation_key: str) -> BrokerChildRecord | None:
        with self._lock:
            return self._by_operation.get((context_identity, operation_key))

    def get_by_child_job(self, child_job_id: str) -> BrokerChildRecord | None:
        with self._lock:
            return self._by_job.get(child_job_id)

    def save(self, record: BrokerChildRecord) -> None:
        with self._lock:
            for current in (
                self._by_job.get(record.child_job_id),
                *[value for value in self._by_operation.values() if value.invoke_operation_key == record.invoke_operation_key and value.parent_attempt_id == record.parent_attempt_id],
            ):
                if current is not None and current.to_dict() != record.to_dict():
                    raise ContractError(ErrorCode.DUPLICATE_REQUEST, "child record identity drift")
            self._by_job[record.child_job_id] = record

    def replace(self, record: BrokerChildRecord) -> None:
        with self._lock:
            if record.child_job_id not in self._by_job:
                raise ContractError(ErrorCode.ASSET_ERROR, "unknown child record")
            self._by_job[record.child_job_id] = record
            for key, value in list(self._by_operation.items()):
                if value.child_job_id == record.child_job_id:
                    self._by_operation[key] = record

    def bind_operation(self, *, context_identity: str, operation_key: str, record: BrokerChildRecord) -> None:
        with self._lock:
            current = self._by_operation.get((context_identity, operation_key))
            if current is not None and current.to_dict() != record.to_dict():
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "child operation identity drift")
            self._by_operation[(context_identity, operation_key)] = record
            self._by_job[record.child_job_id] = record

    @property
    def records(self) -> tuple[BrokerChildRecord, ...]:
        with self._lock:
            return tuple(self._by_job.values())


def _ledger_lookup(ledger: Any, *, context_identity: str, method: str, operation_key: str) -> Any:
    method_fn = getattr(ledger, "lookup", None)
    if method_fn is None:
        method_fn = getattr(ledger, "get", None)
    if method_fn is None:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "broker operation ledger port is incomplete")
    return _call_compatible(
        method_fn,
        (context_identity, method, operation_key),
        {
            "context_identity": context_identity,
            "method": method,
            "operation_key": operation_key,
        },
        label="operation ledger lookup",
    )


def _ledger_record(
    ledger: Any,
    *,
    context_identity: str,
    method: str,
    operation_key: str,
    payload_hash: str,
    response: bytes,
) -> None:
    method_fn = getattr(ledger, "record", None)
    if method_fn is None:
        method_fn = getattr(ledger, "put", None)
    if method_fn is None:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "broker operation ledger port is incomplete")
    _call_compatible(
        method_fn,
        (context_identity, method, operation_key, payload_hash, response),
        {
            "context_identity": context_identity,
            "method": method,
            "operation_key": operation_key,
            "payload_hash": payload_hash,
            "response": response,
        },
        label="operation ledger record",
    )


def _reservation_get(
    ledger: Any,
    *,
    context_identity: str,
    method: str,
    operation_key: str,
) -> BrokerOperationReservation | None:
    fn = getattr(ledger, "get_reservation", None)
    if fn is None:
        raise ContractError(
            ErrorCode.INVALID_TRANSITION,
            "durable operation ledger lacks reservation lookup",
        )
    value = _call_compatible(
        fn,
        (context_identity, method, operation_key),
        {
            "context_identity": context_identity,
            "method": method,
            "operation_key": operation_key,
        },
        label="operation reservation lookup",
    )
    return BrokerOperationReservation.from_value(value)


def _reservation_reserve(
    ledger: Any,
    *,
    context_identity: str,
    method: str,
    operation_key: str,
    payload_hash: str,
) -> BrokerOperationReservation:
    fn = getattr(ledger, "reserve", None)
    if fn is None:
        raise ContractError(
            ErrorCode.INVALID_TRANSITION,
            "durable operation ledger lacks pre-child reservation",
        )
    value = _call_compatible(
        fn,
        (context_identity, method, operation_key, payload_hash),
        {
            "context_identity": context_identity,
            "method": method,
            "operation_key": operation_key,
            "payload_hash": payload_hash,
        },
        label="operation reservation",
    )
    reservation = BrokerOperationReservation.from_value(value)
    if reservation is None:
        reservation = _reservation_get(
            ledger,
            context_identity=context_identity,
            method=method,
            operation_key=operation_key,
        )
    if reservation is None or reservation.payload_hash != payload_hash:
        raise ContractError(
            ErrorCode.INVALID_TRANSITION,
            "durable operation reservation was not materialized",
        )
    return reservation


def _reservation_attach_envelope(
    ledger: Any,
    *,
    context_identity: str,
    method: str,
    operation_key: str,
    payload_hash: str,
    envelope_asset_id: str,
) -> BrokerOperationReservation:
    fn = getattr(ledger, "attach_envelope", None)
    if fn is None:
        raise ContractError(
            ErrorCode.INVALID_TRANSITION,
            "durable operation ledger cannot bind the envelope Asset",
        )
    value = _call_compatible(
        fn,
        (context_identity, method, operation_key, payload_hash, envelope_asset_id),
        {
            "context_identity": context_identity,
            "method": method,
            "operation_key": operation_key,
            "payload_hash": payload_hash,
            "envelope_asset_id": envelope_asset_id,
        },
        label="operation envelope attachment",
    )
    reservation = BrokerOperationReservation.from_value(value)
    if reservation is None:
        reservation = _reservation_get(
            ledger,
            context_identity=context_identity,
            method=method,
            operation_key=operation_key,
        )
    if reservation is None or reservation.envelope_asset_id != envelope_asset_id:
        raise ContractError(
            ErrorCode.INVALID_TRANSITION,
            "durable operation reservation did not bind the envelope Asset",
        )
    return reservation


def _reservation_attach_child(
    ledger: Any,
    *,
    context_identity: str,
    method: str,
    operation_key: str,
    payload_hash: str,
    child_creation: ChildCreationResult,
) -> BrokerOperationReservation:
    fn = getattr(ledger, "attach_child", None)
    if fn is None:
        raise ContractError(
            ErrorCode.INVALID_TRANSITION,
            "durable operation ledger cannot bind child creation",
        )
    child_mapping = child_creation.to_dict()
    value = _call_compatible(
        fn,
        (context_identity, method, operation_key, payload_hash, child_mapping),
        {
            "context_identity": context_identity,
            "method": method,
            "operation_key": operation_key,
            "payload_hash": payload_hash,
            "child_creation": child_mapping,
        },
        label="operation child attachment",
    )
    reservation = BrokerOperationReservation.from_value(value)
    if reservation is None:
        reservation = _reservation_get(
            ledger,
            context_identity=context_identity,
            method=method,
            operation_key=operation_key,
        )
    if (
        reservation is None
        or reservation.child_creation is None
        or _thaw(reservation.child_creation) != child_mapping
    ):
        raise ContractError(
            ErrorCode.INVALID_TRANSITION,
            "durable operation reservation did not bind child creation",
        )
    return reservation


def _entry_payload_hash(entry: Any) -> str:
    if isinstance(entry, BrokerLedgerEntry):
        return entry.payload_hash
    if isinstance(entry, Mapping):
        value = entry.get("payload_hash")
        if isinstance(value, str):
            return value
    value = getattr(entry, "payload_hash", None)
    if isinstance(value, str):
        return value
    raise ContractError(ErrorCode.ASSET_ERROR, "broker ledger entry has no payload hash")


def _entry_response(entry: Any) -> bytes:
    if isinstance(entry, (bytes, bytearray)):
        return bytes(entry)
    if isinstance(entry, Mapping):
        value = entry.get("response", entry.get("response_bytes"))
    else:
        value = getattr(entry, "response", getattr(entry, "response_bytes", None))
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, Mapping):
        return canonical_bytes(dict(value))
    raise ContractError(ErrorCode.ASSET_ERROR, "broker ledger entry has no response")


def _record_from_port(value: Any) -> BrokerChildRecord | None:
    if value is None:
        return None
    if isinstance(value, BrokerChildRecord):
        return value
    if isinstance(value, Mapping):
        return BrokerChildRecord.from_mapping(value)
    raise ContractError(ErrorCode.ASSET_ERROR, "child record port returned an invalid record")


def _store_save(store: Any, record: BrokerChildRecord, *, context_identity: str, operation_key: str) -> None:
    save = getattr(store, "save", None)
    if save is None:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "child record port is incomplete")
    save(record)
    bind = getattr(store, "bind_operation", None)
    if bind is not None:
        _call_compatible(
            bind,
            (context_identity, operation_key, record),
            {
                "context_identity": context_identity,
                "operation_key": operation_key,
                "record": record,
            },
            label="child record operation binding",
        )


def _store_replace(store: Any, record: BrokerChildRecord) -> None:
    replace = getattr(store, "replace", None)
    if replace is None:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "child record port is incomplete")
    replace(record)


def _store_get_operation(store: Any, *, context_identity: str, operation_key: str) -> BrokerChildRecord | None:
    fn = getattr(store, "get_by_operation", None)
    if fn is None:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "child record port is incomplete")
    return _record_from_port(
        _call_compatible(
            fn,
            (context_identity, operation_key),
            {"context_identity": context_identity, "operation_key": operation_key},
            label="child record operation lookup",
        )
    )


def _store_get_job(store: Any, child_job_id: str) -> BrokerChildRecord | None:
    fn = getattr(store, "get_by_child_job", None) or getattr(store, "get", None)
    if fn is None:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "child record port is incomplete")
    return _record_from_port(fn(child_job_id))


def _call_runtime(runtime: Any, name: str, *args: Any) -> Any:
    fn = getattr(runtime, name, None)
    if fn is None:
        aliases = {
            "resolve_release": ("resolve_plugin_release", "release_for"),
            "current_generation": ("get_current_generation", "generation"),
        }
        for alias in aliases.get(name, ()):
            fn = getattr(runtime, alias, None)
            if fn is not None:
                break
    if fn is None:
        raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, f"runtime port lacks {name}")
    try:
        return fn(*args)
    except ContractError:
        raise
    except (KeyError, LookupError) as exc:
        raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, f"runtime could not resolve {name}") from exc


def _call_factory(factory: Any, request: ChildCreationRequest) -> ChildCreationResult:
    if factory is None:
        raise ContractError(
            ErrorCode.INVALID_TRANSITION,
            "child creation port is required; Broker refuses to fabricate a Job or RunSnapshot",
        )
    fn = getattr(factory, "create_or_recover_child", None)
    if not callable(fn):
        raise ContractError(
            ErrorCode.INVALID_TRANSITION,
            "child creation port must atomically create-or-recover by operation key",
        )
    value = _call_compatible(
        fn,
        (request,),
        {
            "envelope_asset_id": request.envelope_asset_id,
            "envelope": request.envelope.to_dict(),
            "input_asset_id": request.input_asset_id,
            "parameters_asset_id": request.parameters_asset_id,
            "binding": request.binding,
            "descriptor": request.descriptor,
            "plugin_release_id": request.plugin_release_id,
            "generation_id": request.generation_id,
            "caller": request.caller,
        },
        label="child creation",
    )
    return ChildCreationResult.from_mapping(value)


def _call_execution_poll(execution: Any, child_job_id: str, after_seq: int) -> Any:
    fn = getattr(execution, "poll", None)
    if fn is None:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "execution port lacks poll")
    try:
        return fn(child_job_id, after_seq)
    except ContractError:
        raise
    except KeyError as exc:
        raise ContractError(ErrorCode.ASSET_ERROR, "unknown child job") from exc


def _call_execution_cancel(execution: Any, operation_key: str, child_job_id: str) -> Any:
    fn = getattr(execution, "cancel", None)
    if fn is None:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "execution port lacks cancel")
    try:
        return fn(operation_key, child_job_id)
    except ContractError:
        raise
    except KeyError as exc:
        raise ContractError(ErrorCode.ASSET_ERROR, "unknown child job") from exc


def _execution_observation(value: Any) -> tuple[bool, list[Mapping[str, Any]], Mapping[str, Any] | None]:
    if isinstance(value, Mapping):
        # A P3 adapter may already return the closed poll projection.  Extra
        # internal ``child_state`` is consumed only for local record hydration.
        if _POLL_RESULT_FIELDS.issubset(value.keys()):
            return bool(value.get("terminal")), [], value
        terminal = value.get("terminal", value.get("done", False))
        events = value.get("events", value.get("job_events", []))
        if not isinstance(terminal, bool) or not isinstance(events, Sequence):
            raise ContractError(ErrorCode.ASSET_ERROR, "execution poll returned an invalid observation")
        if any(not isinstance(item, Mapping) for item in events):
            raise ContractError(ErrorCode.ASSET_ERROR, "execution poll events must be objects")
        return terminal, list(events), value
    if isinstance(value, tuple) and len(value) == 2:
        terminal, events = value
        if not isinstance(terminal, bool) or not isinstance(events, Sequence):
            raise ContractError(ErrorCode.ASSET_ERROR, "execution poll returned an invalid observation")
        if any(not isinstance(item, Mapping) for item in events):
            raise ContractError(ErrorCode.ASSET_ERROR, "execution poll events must be objects")
        return terminal, list(events), None
    raise ContractError(ErrorCode.ASSET_ERROR, "execution poll returned an invalid observation")


def _project_poll(
    snapshot_port: Any,
    *,
    child_job_id: str,
    after_seq: int,
    terminal: bool,
    events: Sequence[Mapping[str, Any]],
    direct: Mapping[str, Any] | None,
) -> tuple[BrokerPollProjection, str | None]:
    raw: Mapping[str, Any] | None = direct
    direct_child_state: str | None = None
    if raw is not None:
        direct_child_state = raw.get("child_state") if isinstance(raw.get("child_state"), str) else None
        raw = {key: raw[key] for key in _POLL_RESULT_FIELDS}
    if raw is None and snapshot_port is not None:
        fn = getattr(snapshot_port, "project_poll", None)
        if fn is None:
            fn = getattr(snapshot_port, "poll_projection", None) or getattr(snapshot_port, "read_child_projection", None)
        if fn is None:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "child snapshot projection port is incomplete")
        raw = _call_compatible(
            fn,
            (child_job_id, after_seq, terminal, events),
            {
                "child_job_id": child_job_id,
                "after_job_event_seq": after_seq,
                "execution_terminal": terminal,
                "execution_events": events,
            },
            label="child snapshot projection",
        )
    if raw is None:
        raise ContractError(
            ErrorCode.INVALID_TRANSITION,
            "durable child snapshot/event projection port is required; Broker will not fabricate Asset IDs",
        )
    if not isinstance(raw, Mapping):
        raise ContractError(ErrorCode.ASSET_ERROR, "child snapshot projection is not an object")
    if not _POLL_RESULT_FIELDS.issubset(raw.keys()):
        raise ContractError(ErrorCode.ASSET_ERROR, "child snapshot projection is missing a closed result field")
    if direct_child_state is None:
        candidate = raw.get("child_state")
        direct_child_state = candidate if isinstance(candidate, str) else None
    raw = {key: raw[key] for key in _POLL_RESULT_FIELDS}
    projection = BrokerPollProjection.from_mapping(raw)
    if projection.next_job_event_seq < after_seq:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "child event sequence moved backwards")
    return projection, direct_child_state


def derive_child_cancel_operation_key(parent_operation_key: str, child_job_id: str) -> str:
    """Frozen ``child-cancel-v1`` derivation from §84.12."""

    _require_id(parent_operation_key, "parent_operation_key")
    _require_id(child_job_id, "child_job_id")
    return sha256_hex(
        f"child-cancel/v1\n{parent_operation_key}\n{child_job_id}\n".encode("utf-8")
    )


def verify_child_snapshot_binding(
    snapshot: Mapping[str, Any] | bytes,
    *,
    child_job_id: str,
    child_step_id: str,
    child_attempt_id: str,
    envelope_asset_id: str,
    envelope_hash: str,
    input_asset_id: str,
    input_hash: str,
    parameters_asset_id: str | None,
    parameters_hash: str | None,
    child_run_snapshot_asset_id: str,
    child_run_snapshot_hash: str,
) -> None:
    """Verify a factory-provided child snapshot or explicit attestation.

    A full ``run-snapshot/v1`` is checked with the SDK verifier.  A smaller
    adapter attestation is accepted only when it explicitly names every
    envelope/input binding; the Broker never treats a bare Job ID/hash pair as
    sufficient evidence.
    """

    _require_id(child_job_id, "child_job_id")
    _require_id(child_step_id, "child_step_id")
    _require_id(child_attempt_id, "child_attempt_id")
    _require_id(envelope_asset_id, "envelope_asset_id")
    _require_hash(envelope_hash, "envelope_hash")
    _require_id(input_asset_id, "input_asset_id")
    _require_hash(input_hash, "input_hash")
    _require_id(child_run_snapshot_asset_id, "child_run_snapshot_asset_id")
    _require_hash(child_run_snapshot_hash, "child_run_snapshot_hash")
    if parameters_asset_id is None:
        if parameters_hash is not None:
            raise ContractValidationError("parameters ID/hash must be null together")
    else:
        _require_id(parameters_asset_id, "parameters_asset_id")
        _require_hash(parameters_hash, "parameters_hash")

    raw_bytes: bytes | None = None
    if isinstance(snapshot, bytes):
        raw_bytes = bytes(snapshot)
        value = parse_json_bytes(raw_bytes)
    else:
        value = dict(snapshot)
    if not isinstance(value, Mapping):
        raise ContractValidationError("child snapshot binding must be an object")

    if value.get("schema") == "run-snapshot/v1":
        verify_snapshot(value)
        if value["snapshot_hash"] != child_run_snapshot_hash:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child snapshot hash does not match factory result")
        if value["parameters_asset_id"] != envelope_asset_id:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child snapshot parameters must be the Broker envelope")
        assets = {item["asset_id"]: item["sha256"] for item in value["asset_hashes"]}
        expected = {envelope_asset_id: envelope_hash, input_asset_id: input_hash}
        if parameters_asset_id is not None:
            expected[parameters_asset_id] = parameters_hash
        for asset_id, asset_hash in expected.items():
            if assets.get(asset_id) != asset_hash:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child snapshot asset hash binding mismatch")
        return

    if raw_bytes is None:
        raw_bytes = canonical_bytes(dict(value))
    if sha256_hex(raw_bytes) != child_run_snapshot_hash:
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "child snapshot Asset bytes do not match the factory snapshot hash",
        )

    required = {
        "child_job_id": child_job_id,
        "child_step_id": child_step_id,
        "child_attempt_id": child_attempt_id,
        "broker_invocation_asset_id": envelope_asset_id,
        "broker_invocation_hash": envelope_hash,
        "input_asset_id": input_asset_id,
        "input_hash": input_hash,
        "parameters_asset_id": envelope_asset_id,
    }
    for key, expected in required.items():
        if value.get(key) != expected:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, f"child snapshot attestation mismatch at {key}")
    if parameters_asset_id is not None:
        if value.get("source_parameters_asset_id") != parameters_asset_id or value.get("source_parameters_hash") != parameters_hash:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child snapshot source parameters binding mismatch")


@dataclass(frozen=True, slots=True)
class BrokerAggregation(_PayloadMapping):
    all_terminal: bool
    required_satisfied: bool
    parent_terminal_allowed: bool
    parent_success_allowed: bool
    required_blockers: tuple[str, ...]
    optional_failures: tuple[str, ...]
    child_receipt_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "all_terminal": self.all_terminal,
            "required_satisfied": self.required_satisfied,
            "parent_terminal_allowed": self.parent_terminal_allowed,
            "parent_success_allowed": self.parent_success_allowed,
            "required_blockers": list(self.required_blockers),
            "optional_failures": list(self.optional_failures),
            "child_receipt_ids": list(self.child_receipt_ids),
        }


def _as_child_record(value: BrokerChildRecord | Mapping[str, Any]) -> BrokerChildRecord:
    return value if isinstance(value, BrokerChildRecord) else BrokerChildRecord.from_mapping(value)


def _is_optional_failure(record: BrokerChildRecord) -> bool:
    """Closed v1 rule shared by aggregation and receipt propagation."""

    return (
        not record.required
        and record.state in TERMINAL_STATES
        and record.state not in SATISFYING_STATES
    )


def aggregate_child_outcomes(
    children: Iterable[BrokerChildRecord | Mapping[str, Any]],
) -> BrokerAggregation:
    records = tuple(_as_child_record(value) for value in children)
    for record in records:
        terminal = record.state in TERMINAL_STATES
        if terminal and record.provenance_receipt_id is None:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "terminal child is missing its provenance receipt",
            )
        if not terminal and (
            record.result_bundle_asset_id is not None
            or record.provenance_receipt_id is not None
        ):
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "non-terminal child cannot expose terminal result or receipt",
            )
    all_terminal = all(record.state in TERMINAL_STATES for record in records)
    required_blockers = tuple(
        record.child_job_id
        for record in records
        if record.required and record.state not in SATISFYING_STATES
    )
    optional_failures = tuple(
        record.child_job_id
        for record in records
        if _is_optional_failure(record)
    )
    receipt_ids = tuple(
        dict.fromkeys(
            record.provenance_receipt_id
            for record in records
            if record.provenance_receipt_id is not None
        )
    )
    required_satisfied = not required_blockers and all(
        (not record.required) or record.state in SATISFYING_STATES for record in records
    )
    return BrokerAggregation(
        all_terminal=all_terminal,
        required_satisfied=required_satisfied,
        parent_terminal_allowed=all_terminal,
        parent_success_allowed=all_terminal and required_satisfied,
        required_blockers=required_blockers,
        optional_failures=optional_failures,
        child_receipt_ids=receipt_ids,
    )


@dataclass(frozen=True, slots=True)
class BrokerReceiptPropagation(_PayloadMapping):
    child_receipt_ids: tuple[str, ...]
    required_child_receipt_ids: tuple[str, ...]
    optional_failure_child_job_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_receipt_ids": list(self.child_receipt_ids),
            "required_child_receipt_ids": list(self.required_child_receipt_ids),
            "optional_failure_child_job_ids": list(self.optional_failure_child_job_ids),
        }


def build_receipt_propagation(
    children: Iterable[BrokerChildRecord | Mapping[str, Any]],
) -> BrokerReceiptPropagation:
    records = tuple(_as_child_record(value) for value in children)
    for record in records:
        if record.state in TERMINAL_STATES and record.provenance_receipt_id is None:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "terminal child is missing its provenance receipt",
            )
    all_ids = tuple(
        dict.fromkeys(
            record.provenance_receipt_id
            for record in records
            if record.provenance_receipt_id is not None
        )
    )
    required_ids = tuple(
        dict.fromkeys(
            record.provenance_receipt_id
            for record in records
            if record.required and record.provenance_receipt_id is not None
        )
    )
    optional_failures = tuple(
        record.child_job_id
        for record in records
        if _is_optional_failure(record)
    )
    return BrokerReceiptPropagation(all_ids, required_ids, optional_failures)


def propagate_receipts(
    children: Iterable[BrokerChildRecord | Mapping[str, Any]],
) -> tuple[str, ...]:
    return build_receipt_propagation(children).child_receipt_ids


class CapabilityBroker:
    """Pure orchestration over injected Core/Runtime/Execution ports."""

    INVOKE_METHOD = "host.capability.invoke/v1"
    POLL_METHOD = "host.capability.poll/v1"
    CANCEL_METHOD = "host.capability.cancel/v1"

    def __init__(
        self,
        *,
        core: BrokerCoreAssetPort,
        execution: BrokerExecutionPort,
        runtime: Any,
        bindings: Mapping[str, CapabilityBinding | Mapping[str, Any]] | Iterable[CapabilityBinding],
        child_factory: BrokerChildFactoryPort | Any | None = None,
        operation_ledger: BrokerOperationLedgerPort | Any | None = None,
        child_records: BrokerChildRecordPort | Any | None = None,
        attempt_context: AttemptContextPort | Any | None = None,
        descriptor_port: BrokerDescriptorPort | Any | None = None,
        child_snapshot_port: BrokerChildSnapshotPort | Any | None = None,
        receipt_port: BrokerReceiptPort | Any | None = None,
        _test_authority_token: object | None = None,
    ) -> None:
        test_composition = _test_authority_token is _TEST_AUTHORITY_SENTINEL
        if _test_authority_token is not None and not test_composition:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "invalid internal test-composition token",
            )
        if attempt_context is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "production CapabilityBroker requires an authoritative Attempt context port",
            )
        if operation_ledger is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "production CapabilityBroker requires a durable operation ledger",
            )
        if child_records is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "production CapabilityBroker requires a durable child-record port",
            )
        if not test_composition and (
            isinstance(attempt_context, InMemoryAttemptContextPort)
            or isinstance(operation_ledger, InMemoryBrokerOperationLedger)
            or isinstance(child_records, InMemoryBrokerChildRecordStore)
        ):
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "test-only InMemory authorities are accepted only by CapabilityBroker.for_test",
            )
        reservation_methods = (
            "lookup",
            "get_reservation",
            "reserve",
            "attach_envelope",
            "attach_child",
            "record",
        )
        if any(not callable(getattr(operation_ledger, name, None)) for name in reservation_methods):
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "durable operation ledger lacks the pre-child reservation protocol",
            )
        child_record_methods = ("get_by_operation", "get_by_child_job", "save", "replace")
        if any(not callable(getattr(child_records, name, None)) for name in child_record_methods):
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "durable child-record port is incomplete",
            )
        if child_factory is not None and not callable(
            getattr(child_factory, "create_or_recover_child", None)
        ):
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "child factory must provide atomic create_or_recover_child",
            )
        self.core = core
        self.execution = execution
        self.runtime = runtime
        self.child_factory = child_factory
        self.operation_ledger = operation_ledger
        self.child_records = child_records
        self.attempt_context = attempt_context
        self.descriptor_port = descriptor_port
        self.child_snapshot_port = child_snapshot_port
        self.receipt_port = receipt_port
        if isinstance(bindings, Mapping):
            pairs = bindings.items()
        else:
            pairs = ((item.binding_id, item) for item in bindings)
        normalized: dict[str, CapabilityBinding] = {}
        for key, value in pairs:
            binding = value if isinstance(value, CapabilityBinding) else CapabilityBinding.from_mapping(value)
            if key != binding.binding_id:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "binding registry key does not match binding_id")
            if key in normalized:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "duplicate binding_id")
            normalized[key] = binding
        self.bindings: Mapping[str, CapabilityBinding] = MappingProxyType(normalized)
        self._lock = RLock()

    @classmethod
    def for_test(
        cls,
        *,
        core: BrokerCoreAssetPort,
        execution: BrokerExecutionPort,
        runtime: Any,
        bindings: Mapping[str, CapabilityBinding | Mapping[str, Any]]
        | Iterable[CapabilityBinding],
        child_factory: BrokerChildFactoryPort | Any | None = None,
        operation_ledger: BrokerOperationLedgerPort | Any | None = None,
        child_records: BrokerChildRecordPort | Any | None = None,
        attempt_context: AttemptContextPort | Any | None = None,
        descriptor_port: BrokerDescriptorPort | Any | None = None,
        child_snapshot_port: BrokerChildSnapshotPort | Any | None = None,
        receipt_port: BrokerReceiptPort | Any | None = None,
    ) -> "CapabilityBroker":
        """Explicit test-only composition with in-memory authority doubles."""

        return cls(
            core=core,
            execution=execution,
            runtime=runtime,
            bindings=bindings,
            child_factory=child_factory,
            operation_ledger=(
                InMemoryBrokerOperationLedger()
                if operation_ledger is None
                else operation_ledger
            ),
            child_records=(
                InMemoryBrokerChildRecordStore()
                if child_records is None
                else child_records
            ),
            attempt_context=(
                InMemoryAttemptContextPort()
                if attempt_context is None
                else attempt_context
            ),
            descriptor_port=descriptor_port,
            child_snapshot_port=child_snapshot_port,
            receipt_port=receipt_port,
            _test_authority_token=_TEST_AUTHORITY_SENTINEL,
        )

    def _caller(self, caller: CallerAttemptContext | Mapping[str, Any] | None) -> CallerAttemptContext:
        if caller is None:
            raise ContractValidationError("caller attempt context is required")
        value = caller if isinstance(caller, CallerAttemptContext) else CallerAttemptContext.from_mapping(caller)
        if not value.fresh or not value.lease_valid:
            raise ContractError(ErrorCode.STALE_LEASE, "caller Attempt context is stale")
        port = self.attempt_context
        fn = getattr(port, "validate_attempt", None)
        if fn is None:
            fn = getattr(port, "validate", None)
        if fn is None:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "Attempt context port is incomplete")
        result = fn(value)
        if result is False:
            raise ContractError(ErrorCode.STALE_LEASE, "caller Attempt context is stale")
        return value

    def _binding(self, binding_id: str) -> CapabilityBinding:
        _require_id(binding_id, "binding_id")
        try:
            return self.bindings[binding_id]
        except KeyError as exc:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "unknown capability binding") from exc

    def _descriptor(self, binding: CapabilityBinding, release_id: str) -> CapabilityDescriptor | None:
        raw: Mapping[str, Any] | CapabilityDescriptor | None = binding.descriptor
        if raw is None and self.descriptor_port is not None:
            fn = getattr(self.descriptor_port, "describe_capability", None) or getattr(self.descriptor_port, "describe", None)
            if fn is None:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "descriptor port is incomplete")
            raw = fn(binding.capability_id)
        if raw is None:
            return None
        if isinstance(raw, CapabilityDescriptor):
            descriptor = raw
            if (
                descriptor.capability_id != binding.capability_id
                or descriptor.plugin_id != binding.plugin_id
                or descriptor.release_id != release_id
                or descriptor.result_contract != binding.result_contract
            ):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "binding/descriptor identity mismatch")
            return descriptor
        return CapabilityDescriptor.from_mapping(
            raw,
            expected_capability_id=binding.capability_id,
            expected_plugin_id=binding.plugin_id,
            expected_release_id=release_id,
        )

    def _read_asset(self, asset_id: str, label: str) -> bytes:
        _require_id(asset_id, label)
        fn = getattr(self.core, "read_asset", None)
        if fn is None:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "Core asset port lacks read_asset")
        try:
            content = fn(asset_id)
        except ContractError:
            raise
        except (KeyError, LookupError, OSError) as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, f"{label} does not exist") from exc
        if not isinstance(content, (bytes, bytearray)):
            raise ContractError(ErrorCode.ASSET_ERROR, f"{label} port did not return bytes")
        return bytes(content)

    def _create_asset(self, content: bytes) -> str:
        fn = getattr(self.core, "create_asset", None)
        if fn is None:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "Core asset port lacks create_asset")
        try:
            asset_id = fn(bytes(content), mime="application/json")
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "Core could not create immutable Broker envelope Asset") from exc
        return _require_id(asset_id, "broker_invocation_asset_id")

    def _validate_existing_invoke(
        self,
        entry: Any,
        *,
        payload_hash: str,
        envelope: BrokerInvocationEnvelope,
        caller: CallerAttemptContext,
        binding: CapabilityBinding,
        context_identity: str,
        operation_key: str,
    ) -> BrokerInvokeResult:
        if _entry_payload_hash(entry) != payload_hash:
            raise ContractError(ErrorCode.DUPLICATE_REQUEST, "operation key reused with a different payload")
        try:
            response = parse_json_bytes(_entry_response(entry))
        except ContractError:
            raise
        if not isinstance(response, Mapping):
            raise ContractError(ErrorCode.ASSET_ERROR, "broker ledger response is not an object")
        result = BrokerInvokeResult.from_mapping(response)
        if not result.accepted:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "invoke operation ledger contains a non-accepted response",
            )
        record = self._record_for_caller(
            caller,
            result.child_job_id,
            invoke_operation_key=operation_key,
        )
        direct_resume_alias = record.parent_attempt_id != caller.parent_attempt_id
        if (
            record.parent_job_id != caller.parent_job_id
            or record.parent_step_id != caller.parent_step_id
            or record.invoke_operation_key != operation_key
            or record.binding_id != binding.binding_id
            or record.result_contract != binding.result_contract
            or record.required != binding.required
            or record.propagate_cancel != binding.propagate_cancel
            or (
                not direct_resume_alias
                and record.broker_invocation_hash != payload_hash
            )
        ):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "invoke ledger and child record binding drift",
            )
        reservation = _reservation_get(
            self.operation_ledger,
            context_identity=context_identity,
            method=self.INVOKE_METHOD,
            operation_key=operation_key,
        )
        if (
            reservation is None
            or reservation.envelope_asset_id is None
            or reservation.child_creation is None
            or reservation.payload_hash != payload_hash
        ):
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "invoke replay lacks its durable current-context reservation",
            )
        try:
            persisted = BrokerInvocationEnvelope.from_mapping(
                parse_json_bytes(
                    self._read_asset(
                        reservation.envelope_asset_id,
                        "broker_invocation_asset_id",
                    )
                )
            )
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "stored Broker envelope is not a valid invocation",
            ) from exc
        if persisted.to_dict() != envelope.to_dict() or persisted.asset_hash != payload_hash:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "stored Broker envelope does not match the replay payload",
            )
        if (
            result.child_job_id != record.child_job_id
            or result.child_run_snapshot_asset_id != record.child_run_snapshot_asset_id
            or result.child_run_snapshot_hash != record.child_run_snapshot_hash
            or result.child_result_contract != record.result_contract
        ):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "invoke ledger response and child record binding drift",
            )
        creation = ChildCreationResult.from_mapping(_thaw(reservation.child_creation))
        if (
            creation.child_job_id != result.child_job_id
            or creation.child_step_id != result.child_step_id
            or creation.child_run_snapshot_asset_id != result.child_run_snapshot_asset_id
            or creation.child_run_snapshot_hash != result.child_run_snapshot_hash
            or creation.child_plugin_release_id is None
        ):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "invoke reservation response closure drift")
        snapshot_bytes = self._read_asset(record.child_run_snapshot_asset_id, "child_run_snapshot_asset_id")
        verify_child_snapshot_binding(
            snapshot_bytes,
            child_job_id=creation.child_job_id,
            child_step_id=creation.child_step_id,
            child_attempt_id=creation.child_attempt_id,
            envelope_asset_id=record.broker_invocation_asset_id,
            envelope_hash=record.broker_invocation_hash,
            input_asset_id=envelope.input_asset_id,
            input_hash=envelope.input_hash,
            parameters_asset_id=envelope.parameters_asset_id,
            parameters_hash=envelope.parameters_hash,
            child_run_snapshot_asset_id=creation.child_run_snapshot_asset_id,
            child_run_snapshot_hash=creation.child_run_snapshot_hash,
        )
        validator = getattr(self.child_factory, "validate_committed_child_replay", None)
        if callable(validator):
            validator(
                creation,
                expected_release_id=creation.child_plugin_release_id,
                expected_generation_id=_call_runtime(self.runtime, "current_generation"),
                expected_result_contract=binding.result_contract,
            )
        return result

    def invoke(
        self,
        caller: CallerAttemptContext | Mapping[str, Any] | None = None,
        operation_key: str | None = None,
        binding_id: str | None = None,
        input_asset_id: str | None = None,
        parameters_asset_id: str | None = None,
        expected_result_contract: str | None = None,
        propagate_cancel: bool | None = None,
        *,
        caller_context: CallerAttemptContext | Mapping[str, Any] | None = None,
        context: CallerAttemptContext | Mapping[str, Any] | None = None,
    ) -> BrokerInvokeResult:
        """Create or replay one child invocation, with fencing first."""

        caller = caller if caller is not None else (caller_context if caller_context is not None else context)
        current = self._caller(caller)
        if operation_key is None or binding_id is None or input_asset_id is None:
            raise ContractValidationError("invoke requires operation_key, binding_id and input_asset_id")
        _require_id(operation_key, "operation_key")
        binding = self._binding(binding_id)
        if expected_result_contract is not None and expected_result_contract != binding.result_contract:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "caller result contract differs from frozen binding")
        if propagate_cancel is not None:
            _require_bool(propagate_cancel, "propagate_cancel")
            if propagate_cancel != binding.propagate_cancel:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "caller propagate_cancel differs from frozen binding")
        _require_id(input_asset_id, "input_asset_id")
        if parameters_asset_id is not None:
            _require_id(parameters_asset_id, "parameters_asset_id")

        # The child factory is a hard boundary.  Refuse before creating even
        # the envelope Asset when the composition cannot create a real child.
        if self.child_factory is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "child creation port is required; Broker refuses to fabricate a Job or RunSnapshot",
            )

        with self._lock:
            input_bytes = self._read_asset(input_asset_id, "input_asset_id")
            parameters_bytes = (
                self._read_asset(parameters_asset_id, "parameters_asset_id")
                if parameters_asset_id is not None
                else None
            )
            release_id = _call_runtime(self.runtime, "resolve_release", binding.plugin_id, binding.release_requirement)
            generation_id = _call_runtime(self.runtime, "current_generation")
            _require_id(release_id, "plugin release_id")
            _require_id(generation_id, "generation_id")
            descriptor = self._descriptor(binding, release_id)
            invocation_id = f"broker-invocation-{operation_key}"
            # ID length is part of the contract.  A stable hash is used for a
            # long caller key rather than truncating identity.
            if _ID.fullmatch(invocation_id) is None:
                invocation_id = "broker-invocation-" + sha256_hex(operation_key.encode("utf-8"))[:48]
            envelope = BrokerInvocationEnvelope(
                invocation_id=invocation_id,
                parent_job_id=current.parent_job_id,
                parent_step_id=current.parent_step_id,
                parent_attempt_id=current.parent_attempt_id,
                invoke_operation_key=operation_key,
                binding_id=binding.binding_id,
                input_asset_id=input_asset_id,
                input_hash=sha256_hex(input_bytes),
                parameters_asset_id=parameters_asset_id,
                parameters_hash=(sha256_hex(parameters_bytes) if parameters_bytes is not None else None),
                expected_result_contract=binding.result_contract,
                required=binding.required,
                propagate_cancel=binding.propagate_cancel,
            )
            payload_hash = sha256_hex(envelope.canonical_bytes())
            context_identity = current.identity()
            existing = _ledger_lookup(
                self.operation_ledger,
                context_identity=context_identity,
                method=self.INVOKE_METHOD,
                operation_key=operation_key,
            )
            if existing is not None:
                return self._validate_existing_invoke(
                    existing,
                    payload_hash=payload_hash,
                    envelope=envelope,
                    caller=current,
                    binding=binding,
                    context_identity=context_identity,
                    operation_key=operation_key,
                )

            aliaser = getattr(self.attempt_context, "alias_direct_resume_invoke", None)
            if callable(aliaser):
                aliased = aliaser(
                    caller=current,
                    envelope=envelope,
                    binding=binding,
                    plugin_release_id=release_id,
                    generation_id=generation_id,
                )
                if aliased is not None:
                    existing = _ledger_lookup(
                        self.operation_ledger,
                        context_identity=context_identity,
                        method=self.INVOKE_METHOD,
                        operation_key=operation_key,
                    )
                    if existing is None:
                        raise ContractError(
                            ErrorCode.ASSET_ERROR,
                            "direct resume alias was not durably persisted",
                        )
                    return self._validate_existing_invoke(
                        existing,
                        payload_hash=payload_hash,
                        envelope=envelope,
                        caller=current,
                        binding=binding,
                        context_identity=context_identity,
                        operation_key=operation_key,
                    )

            reservation = _reservation_reserve(
                self.operation_ledger,
                context_identity=context_identity,
                method=self.INVOKE_METHOD,
                operation_key=operation_key,
                payload_hash=payload_hash,
            )
            orphaned_child = _store_get_operation(
                self.child_records,
                context_identity=context_identity,
                operation_key=operation_key,
            )
            if orphaned_child is not None and reservation.child_creation is None:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "child record exists without a durable child reservation",
                )

            if reservation.envelope_asset_id is None:
                envelope_asset_id = self._create_asset(envelope.canonical_bytes())
                reservation = _reservation_attach_envelope(
                    self.operation_ledger,
                    context_identity=context_identity,
                    method=self.INVOKE_METHOD,
                    operation_key=operation_key,
                    payload_hash=payload_hash,
                    envelope_asset_id=envelope_asset_id,
                )
            else:
                envelope_asset_id = reservation.envelope_asset_id
            stored_envelope = self._read_asset(envelope_asset_id, "broker_invocation_asset_id")
            if stored_envelope != envelope.canonical_bytes() or sha256_hex(stored_envelope) != payload_hash:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "durably reserved Broker envelope Asset drifted",
                )
            request = ChildCreationRequest(
                envelope_asset_id=envelope_asset_id,
                envelope=envelope,
                input_asset_id=input_asset_id,
                parameters_asset_id=parameters_asset_id,
                binding=binding,
                descriptor=descriptor,
                plugin_release_id=release_id,
                generation_id=generation_id,
                caller=current,
            )
            if reservation.child_creation is None:
                created = _call_factory(self.child_factory, request)
                reservation = _reservation_attach_child(
                    self.operation_ledger,
                    context_identity=context_identity,
                    method=self.INVOKE_METHOD,
                    operation_key=operation_key,
                    payload_hash=payload_hash,
                    child_creation=created,
                )
            else:
                created = ChildCreationResult.from_mapping(_thaw(reservation.child_creation))
            if created.child_plugin_release_id != release_id:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "child creation release does not match the resolved release",
                )
            if created.state in TERMINAL_STATES:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "child factory cannot publish a terminal child without a verified receipt projection",
                )
            snapshot_bytes = self._read_asset(
                created.child_run_snapshot_asset_id,
                "child_run_snapshot_asset_id",
            )
            verify_child_snapshot_binding(
                snapshot_bytes,
                child_job_id=created.child_job_id,
                child_step_id=created.child_step_id,
                child_attempt_id=created.child_attempt_id,
                envelope_asset_id=envelope_asset_id,
                envelope_hash=envelope.asset_hash,
                input_asset_id=input_asset_id,
                input_hash=envelope.input_hash,
                parameters_asset_id=parameters_asset_id,
                parameters_hash=envelope.parameters_hash,
                child_run_snapshot_asset_id=created.child_run_snapshot_asset_id,
                child_run_snapshot_hash=created.child_run_snapshot_hash,
            )
            persisted_snapshot = parse_json_bytes(snapshot_bytes)
            inline_snapshot = (
                created.child_run_snapshot
                if created.child_run_snapshot is not None
                else created.binding_attestation
            )
            if inline_snapshot is not None and _thaw(inline_snapshot) != persisted_snapshot:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "factory snapshot claim differs from the authoritative Core Asset",
                )
            record = BrokerChildRecord(
                child_job_id=created.child_job_id,
                parent_job_id=current.parent_job_id,
                parent_step_id=current.parent_step_id,
                parent_attempt_id=current.parent_attempt_id,
                invoke_operation_key=operation_key,
                binding_id=binding.binding_id,
                broker_invocation_asset_id=envelope_asset_id,
                broker_invocation_hash=envelope.asset_hash,
                child_run_snapshot_asset_id=created.child_run_snapshot_asset_id,
                child_run_snapshot_hash=created.child_run_snapshot_hash,
                result_contract=binding.result_contract,
                required=binding.required,
                propagate_cancel=binding.propagate_cancel,
                state=created.state,
                result_bundle_asset_id=None,
                provenance_receipt_id=None,
            )
            if orphaned_child is None:
                _store_save(self.child_records, record, context_identity=context_identity, operation_key=operation_key)
            elif orphaned_child.to_dict() != record.to_dict():
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "durable child record drifted from the operation reservation",
                )
            response = BrokerInvokeResult(
                accepted=True,
                child_job_id=created.child_job_id,
                child_step_id=created.child_step_id,
                child_run_snapshot_asset_id=created.child_run_snapshot_asset_id,
                child_run_snapshot_hash=created.child_run_snapshot_hash,
                child_result_contract=binding.result_contract,
                child_job_event_seq=created.child_job_event_seq,
            )
            validate_rpc_result(self.INVOKE_METHOD, response.to_dict())
            _ledger_record(
                self.operation_ledger,
                context_identity=context_identity,
                method=self.INVOKE_METHOD,
                operation_key=operation_key,
                payload_hash=payload_hash,
                response=response.canonical_bytes(),
            )
            return response

    def _record_for_caller(
        self,
        caller: CallerAttemptContext,
        child_job_id: str,
        *,
        invoke_operation_key: str | None = None,
    ) -> BrokerChildRecord:
        record = _store_get_job(self.child_records, child_job_id)
        if record is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "unknown child job")
        if (
            record.parent_job_id != caller.parent_job_id
            or record.parent_step_id != caller.parent_step_id
        ):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "child does not belong to caller Job/Step",
            )
        if record.parent_attempt_id != caller.parent_attempt_id:
            resolver = getattr(
                self.attempt_context, "resolve_direct_resume_child_alias", None
            )
            if not callable(resolver):
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "child does not belong to caller Attempt",
                )
            resolved = resolver(
                caller=caller,
                child_job_id=child_job_id,
                invoke_operation_key=invoke_operation_key,
            )
            resolved_record = _record_from_port(resolved)
            if resolved_record is None or resolved_record.to_dict() != record.to_dict():
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "child is not the caller's persisted direct-resume alias",
                )
        return record

    def _validate_result_binding(
        self,
        caller: CallerAttemptContext,
        record: BrokerChildRecord,
        projection: BrokerPollProjection,
    ) -> None:
        if not projection.terminal and (
            projection.result_bundle_asset_id is not None
            or projection.provenance_receipt_id is not None
        ):
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "non-terminal projection cannot expose a result or receipt",
            )
        if projection.terminal and projection.provenance_receipt_id is None:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "terminal child projection requires a provenance receipt",
            )
        bundle_id: str | None = None
        bundle_hash: str | None = None
        if projection.result_bundle_asset_id is not None:
            bundle_bytes = self._read_asset(projection.result_bundle_asset_id, "result_bundle_asset_id")
            try:
                bundle = parse_json_bytes(bundle_bytes)
            except ContractError:
                raise
            if not isinstance(bundle, Mapping):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "result bundle Asset is not an object")
            if bundle.get("contract_id") != record.result_contract:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child result contract does not match result bundle")
            verify_result_bundle(bundle, snapshot_hash_value=record.child_run_snapshot_hash)
            bundle_id = bundle["bundle_id"]
            bundle_hash = sha256_hex(bundle_bytes)
            if bundle.get("provenance_receipt_id") != projection.provenance_receipt_id:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "result bundle and child projection receipt IDs differ",
                )
        if projection.provenance_receipt_id is not None:
            if self.receipt_port is None:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "terminal child receipt requires an authoritative receipt port",
                )
            fn = getattr(self.receipt_port, "read_receipt", None)
            if fn is None:
                fn = getattr(self.receipt_port, "get", None)
            if fn is None:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "receipt port is incomplete")
            receipt = fn(projection.provenance_receipt_id)
            if not isinstance(receipt, Mapping):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "receipt port returned an invalid receipt")
            receipt = dict(receipt)
            verify_provenance_receipt(receipt)
            reservation = _reservation_get(
                self.operation_ledger,
                context_identity=caller.identity(),
                method=self.INVOKE_METHOD,
                operation_key=record.invoke_operation_key,
            )
            if reservation is None or reservation.child_creation is None:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "child receipt cannot be verified without its durable child reservation",
                )
            creation = ChildCreationResult.from_mapping(_thaw(reservation.child_creation))
            binding = self._binding(record.binding_id)
            if (
                receipt["receipt_id"] != projection.provenance_receipt_id
                or receipt["job_id"] != record.child_job_id
                or receipt["step_id"] != creation.child_step_id
                or receipt["attempt_id"] != creation.child_attempt_id
                or receipt["lease_epoch"] != creation.child_lease_epoch
                or receipt["run_snapshot_hash"] != record.child_run_snapshot_hash
                or receipt["plugin_id"] != binding.plugin_id
                or receipt["capability_id"] != binding.capability_id
                or receipt["release_id"] != creation.child_plugin_release_id
                or receipt["bundle_id"] != bundle_id
                or receipt["bundle_hash"] != bundle_hash
            ):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child provenance receipt is not bound")

    def poll(
        self,
        caller: CallerAttemptContext | Mapping[str, Any] | None = None,
        child_job_id: str | None = None,
        after_job_event_seq: int = 0,
        *,
        caller_context: CallerAttemptContext | Mapping[str, Any] | None = None,
        context: CallerAttemptContext | Mapping[str, Any] | None = None,
        after_event_seq: int | None = None,
    ) -> BrokerPollProjection:
        caller = caller if caller is not None else (caller_context if caller_context is not None else context)
        current = self._caller(caller)
        if child_job_id is None:
            raise ContractValidationError("poll requires child_job_id")
        _require_id(child_job_id, "child_job_id")
        if after_event_seq is not None:
            after_job_event_seq = after_event_seq
        _require_nonnegative_int(after_job_event_seq, "after_job_event_seq")
        record = self._record_for_caller(current, child_job_id)
        with self._lock:
            observed = _call_execution_poll(self.execution, child_job_id, after_job_event_seq)
            terminal, events, direct = _execution_observation(observed)
            projection, child_state = _project_poll(
                self.child_snapshot_port,
                child_job_id=child_job_id,
                after_seq=after_job_event_seq,
                terminal=terminal,
                events=events,
                direct=direct,
            )
            self._validate_result_binding(current, record, projection)
            if record.result_bundle_asset_id is not None and projection.result_bundle_asset_id != record.result_bundle_asset_id:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child result bundle changed")
            if record.provenance_receipt_id is not None and projection.provenance_receipt_id != record.provenance_receipt_id:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child provenance receipt changed")
            if child_state is not None and ((child_state in TERMINAL_STATES) != projection.terminal):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child state and terminal projection disagree")
            # A terminal record is immutable.  A late stale observation cannot
            # downgrade it or replace its result/receipt.
            if record.state in TERMINAL_STATES:
                if not projection.terminal or child_state != record.state:
                    raise ContractError(
                        ErrorCode.INVALID_TRANSITION,
                        "terminal child projection cannot be downgraded or change state",
                    )
                return projection
            if child_state is not None:
                new_state = child_state
            elif projection.terminal:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "terminal projection must include the authoritative child state",
                )
            else:
                new_state = record.state
            updated = record.with_projection(
                state=new_state,
                result_bundle_asset_id=(
                    projection.result_bundle_asset_id
                    if projection.result_bundle_asset_id is not None
                    else record.result_bundle_asset_id
                ),
                provenance_receipt_id=(
                    projection.provenance_receipt_id
                    if projection.provenance_receipt_id is not None
                    else record.provenance_receipt_id
                ),
            )
            if updated.to_dict() != record.to_dict():
                _store_replace(self.child_records, updated)
            return projection

    def _cancel_response_from_ledger(
        self,
        entry: Any,
        *,
        payload_hash: str,
    ) -> BrokerCancelResult:
        if _entry_payload_hash(entry) != payload_hash:
            raise ContractError(ErrorCode.DUPLICATE_REQUEST, "operation key reused with a different payload")
        return BrokerCancelResult.from_mapping(parse_json_bytes(_entry_response(entry)))

    def cancel(
        self,
        caller: CallerAttemptContext | Mapping[str, Any] | None = None,
        operation_key: str | None = None,
        child_job_id: str | None = None,
        reason: str = "parent_cancelled",
        *,
        caller_context: CallerAttemptContext | Mapping[str, Any] | None = None,
        context: CallerAttemptContext | Mapping[str, Any] | None = None,
    ) -> BrokerCancelResult:
        caller = caller if caller is not None else (caller_context if caller_context is not None else context)
        current = self._caller(caller)
        if operation_key is None or child_job_id is None:
            raise ContractValidationError("cancel requires operation_key and child_job_id")
        _require_id(operation_key, "operation_key")
        _require_id(child_job_id, "child_job_id")
        if not isinstance(reason, str) or not reason:
            raise ContractValidationError("cancel reason must be a non-empty string")
        record = self._record_for_caller(current, child_job_id)
        context_identity = current.identity()
        payload = {
            "schema": "broker-child-cancel/v1",
            "parent_job_id": current.parent_job_id,
            "parent_step_id": current.parent_step_id,
            "parent_attempt_id": current.parent_attempt_id,
            "child_job_id": child_job_id,
            "invoke_operation_key": record.invoke_operation_key,
            "operation_key": operation_key,
            "reason": reason,
        }
        payload_hash = sha256_hex(canonical_bytes(payload))
        with self._lock:
            existing = _ledger_lookup(
                self.operation_ledger,
                context_identity=context_identity,
                method=self.CANCEL_METHOD,
                operation_key=operation_key,
            )
            if existing is not None:
                return self._cancel_response_from_ledger(existing, payload_hash=payload_hash)
            prior_reservation = _reservation_get(
                self.operation_ledger,
                context_identity=context_identity,
                method=self.CANCEL_METHOD,
                operation_key=operation_key,
            )
            if prior_reservation is not None:
                if prior_reservation.payload_hash != payload_hash:
                    raise ContractError(ErrorCode.DUPLICATE_REQUEST, "cancel reservation payload drift")
                if record.state == "cancelling" or record.state in TERMINAL_STATES:
                    recovered = BrokerCancelResult(
                        accepted=True,
                        terminal_known=record.state in TERMINAL_STATES,
                        child_state=record.state,
                        child_job_event_seq=0,
                    )
                    _ledger_record(
                        self.operation_ledger,
                        context_identity=context_identity,
                        method=self.CANCEL_METHOD,
                        operation_key=operation_key,
                        payload_hash=payload_hash,
                        response=recovered.canonical_bytes(),
                    )
                    return recovered
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "cancel side-effect outcome is uncertain; refusing duplicate execution",
                )
            # The durable reservation is the recovery boundary.  It must be
            # visible before execution.cancel can mutate child authority.
            _reservation_reserve(
                self.operation_ledger,
                context_identity=context_identity,
                method=self.CANCEL_METHOD,
                operation_key=operation_key,
                payload_hash=payload_hash,
            )
            if record.state in TERMINAL_STATES:
                response = BrokerCancelResult(
                    accepted=True,
                    terminal_known=True,
                    child_state=record.state,
                    child_job_event_seq=0,
                )
            else:
                raw = _call_execution_cancel(self.execution, operation_key, child_job_id)
                if isinstance(raw, bool):
                    accepted = raw
                    terminal_known = False
                    child_state = "cancelling"
                    event_seq = 0
                elif isinstance(raw, Mapping):
                    accepted = raw.get("accepted", True)
                    terminal_known = raw.get("terminal_known", False)
                    child_state = raw.get("child_state", "cancelling")
                    event_seq = raw.get("child_job_event_seq", 0)
                    _require_bool(accepted, "accepted")
                    _require_bool(terminal_known, "terminal_known")
                else:
                    accepted = True
                    terminal_known = False
                    child_state = "cancelling"
                    event_seq = 0
                response = BrokerCancelResult(
                    accepted=accepted,
                    terminal_known=terminal_known,
                    child_state=_require_id(child_state, "child_state"),
                    child_job_event_seq=_require_nonnegative_int(event_seq, "child_job_event_seq"),
                )
                updated = record.with_projection(state=response.child_state)
                if updated.to_dict() != record.to_dict():
                    _store_replace(self.child_records, updated)
            validate_rpc_result(self.CANCEL_METHOD, response.to_dict())
            _ledger_record(
                self.operation_ledger,
                context_identity=context_identity,
                method=self.CANCEL_METHOD,
                operation_key=operation_key,
                payload_hash=payload_hash,
                response=response.canonical_bytes(),
            )
            return response

    def propagate_cancel(
        self,
        caller: CallerAttemptContext | Mapping[str, Any] | None = None,
        child_job_id: str | None = None,
        reason: str = "parent_cancelled",
        *,
        caller_context: CallerAttemptContext | Mapping[str, Any] | None = None,
        context: CallerAttemptContext | Mapping[str, Any] | None = None,
    ) -> BrokerCancelResult | None:
        """Propagate exactly once when the frozen binding permits it."""

        caller = caller if caller is not None else (caller_context if caller_context is not None else context)
        current = self._caller(caller)
        if child_job_id is None:
            raise ContractValidationError("propagate_cancel requires child_job_id")
        record = self._record_for_caller(current, child_job_id)
        if not record.propagate_cancel:
            return None
        if record.state in TERMINAL_STATES:
            # No operation key is derived for a terminal child: this is a
            # terminal no-op and cannot mutate the durable record.
            return BrokerCancelResult(True, True, record.state, 0)
        operation_key = derive_child_cancel_operation_key(record.invoke_operation_key, child_job_id)
        return self.cancel(
            current,
            operation_key=operation_key,
            child_job_id=child_job_id,
            reason=reason,
        )

    cancel_child = cancel
    cancel_propagation = propagate_cancel


__all__ = [
    "AttemptContextPort",
    "BrokerAggregation",
    "BrokerBinding",
    "BrokerCancelResult",
    "BrokerChildFactoryPort",
    "BrokerChildRecord",
    "BrokerChildRecordPort",
    "BrokerChildRecordStore",
    "BrokerChildSnapshotPort",
    "BrokerCoreAssetPort",
    "BrokerDescriptorPort",
    "BrokerExecutionPort",
    "BrokerInvocationEnvelope",
    "BrokerInvokeResult",
    "BrokerLedgerEntry",
    "BrokerOperationLedgerPort",
    "BrokerOperationReservation",
    "BrokerPollProjection",
    "BrokerReceiptPort",
    "BrokerReceiptPropagation",
    "CapabilityBinding",
    "CapabilityDescriptor",
    "CapabilityBroker",
    "CallerAttemptContext",
    "ChildCreationRequest",
    "ChildCreationResult",
    "InMemoryAttemptContextPort",
    "InMemoryBrokerChildRecordStore",
    "InMemoryBrokerOperationLedger",
    "InMemoryBrokerLedger",
    "InvocationEnvelope",
    "InvokeResult",
    "PollResult",
    "RESULT_CONTRACTS",
    "TERMINAL_STATES",
    "aggregate_child_outcomes",
    "build_receipt_propagation",
    "derive_child_cancel_operation_key",
    "propagate_receipts",
    "verify_child_snapshot_binding",
]
