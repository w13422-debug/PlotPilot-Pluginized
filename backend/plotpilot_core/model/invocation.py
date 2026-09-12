"""Durable single-database authority for Attempt-scoped model invocations."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from backend.plotpilot_plugin_sdk import (
    ContractError,
    ErrorCode,
    canonical_bytes,
    parse_json_bytes,
)

from ..domain.entities import utc_now
from ..repositories.authority import CoreAuthorityRepository

HOST_MODEL_METHOD = "host.model.invoke/v1"
MODEL_INVOCATION_STATES = (
    "reserved",
    "dispatching",
    "received",
    "failed",
    "uncertain",
)
MODEL_INVOCATION_TERMINAL_STATES = ("received", "failed", "uncertain")

_RESERVATION_FIELDS = (
    "context_identity",
    "method",
    "operation_key",
    "workspace_id",
    "job_id",
    "step_id",
    "attempt_id",
    "lease_epoch",
    "caller_plugin_id",
    "caller_plugin_release_id",
    "caller_plugin_package_hash",
    "generation_id",
    "run_snapshot_id",
    "run_snapshot_asset_id",
    "run_snapshot_hash",
    "plan_revision_id",
    "plan_revision_hash",
    "model_profile_revision_id",
    "model_profile_revision_hash",
    "provider_plugin_id",
    "provider_release_id",
    "invocation_id",
    "invocation_key",
    "replay_policy",
    "host_request_hash",
    "host_request_json",
    "source_request_asset_id",
    "source_request_asset_hash",
)
_TERMINAL_FIELDS = (
    "state",
    "provider_success_json",
    "provider_transport_request_hash",
    "provider_transport_response_hash",
    "response_asset_id",
    "response_asset_hash",
    "model_receipt_id",
    "receipt_asset_id",
    "receipt_asset_hash",
    "receipt_hash",
    "host_result_json",
    "rpc_error_json",
    "uncertainty_json",
)
_RECEIPTLESS_MESSAGE = (
    "Model invocation crossed the durable dispatch barrier without canonical "
    "receipt evidence; automatic resend is forbidden"
)


def canonical_json_text(value: Mapping[str, Any]) -> str:
    return canonical_bytes(dict(value)).decode("utf-8")


def _decode_canonical(raw: object, field: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ContractError(ErrorCode.ASSET_ERROR, f"stored {field} is not text")
    try:
        value = parse_json_bytes(raw.encode("utf-8"))
    except Exception as exc:
        raise ContractError(ErrorCode.ASSET_ERROR, f"stored {field} is invalid JSON") from exc
    if not isinstance(value, Mapping) or canonical_bytes(value).decode("utf-8") != raw:
        raise ContractError(ErrorCode.ASSET_ERROR, f"stored {field} is not canonical JSON")
    return dict(value)


def _asset_binding(asset_id: object, digest: object, field: str) -> None:
    if asset_id is None and digest is None:
        return
    if (
        not isinstance(asset_id, str)
        or not isinstance(digest, str)
        or asset_id != f"asset-sha256-{digest}"
    ):
        raise ContractError(ErrorCode.ASSET_ERROR, f"stored {field} Asset identity drifted")


def receiptless_uncertainty(*, recovered: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    reason = "restart_recovery" if recovered else "receiptless_provider_outcome"
    rpc_error = {
        "code": int(ErrorCode.UNCERTAIN_EXTERNAL_EFFECT),
        "message": _RECEIPTLESS_MESSAGE,
        "retryable": False,
        "details_asset_id": None,
        "reason": reason,
    }
    uncertainty = {
        "code": "uncertain_external_effect",
        "message": _RECEIPTLESS_MESSAGE,
        "details_asset_id": None,
    }
    return rpc_error, uncertainty


@dataclass(frozen=True, slots=True)
class ModelInvocationRecord:
    """Validated projection of one immutable model_invocation row."""

    values: Mapping[str, Any]

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> ModelInvocationRecord:
        values = dict(row)
        if values.get("method") != HOST_MODEL_METHOD or values.get("state") not in MODEL_INVOCATION_STATES:
            raise ContractError(ErrorCode.ASSET_ERROR, "stored model invocation discriminator drifted")
        host_request = _decode_canonical(values.get("host_request_json"), "host_request_json")
        if host_request is None or hashlib.sha256(canonical_bytes(host_request)).hexdigest() != values.get(
            "host_request_hash"
        ):
            raise ContractError(ErrorCode.ASSET_ERROR, "stored Host request hash drifted")
        for field in (
            "provider_request_json",
            "provider_success_json",
            "host_result_json",
            "rpc_error_json",
            "uncertainty_json",
        ):
            _decode_canonical(values.get(field), field)
        _asset_binding(
            values.get("source_request_asset_id"),
            values.get("source_request_asset_hash"),
            "source request",
        )
        _asset_binding(
            values.get("response_asset_id"),
            values.get("response_asset_hash"),
            "response",
        )
        _asset_binding(
            values.get("receipt_asset_id"),
            values.get("receipt_asset_hash"),
            "receipt",
        )
        return cls(values)

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    @property
    def state(self) -> str:
        return str(self.values["state"])

    @property
    def terminal(self) -> bool:
        return self.state in MODEL_INVOCATION_TERMINAL_STATES

    @property
    def receipt_backed(self) -> bool:
        return self.values.get("receipt_asset_id") is not None

    @property
    def host_result(self) -> dict[str, Any] | None:
        return _decode_canonical(self.values.get("host_result_json"), "host_result_json")

    @property
    def provider_request(self) -> dict[str, Any] | None:
        return _decode_canonical(
            self.values.get("provider_request_json"), "provider_request_json"
        )

    @property
    def provider_success(self) -> dict[str, Any] | None:
        return _decode_canonical(
            self.values.get("provider_success_json"), "provider_success_json"
        )

    def assert_reservation(self, expected: Mapping[str, Any]) -> None:
        drift = [field for field in _RESERVATION_FIELDS if self.values.get(field) != expected.get(field)]
        if drift:
            raise ContractError(
                ErrorCode.DUPLICATE_REQUEST,
                "model invocation identity or payload changed for the stable operation key",
                details={"fields": drift},
            )


class ModelInvocationLedger:
    """Transactional state machine over the sole CoreAuthorityRepository."""

    def __init__(self, repository: CoreAuthorityRepository) -> None:
        if not isinstance(repository, CoreAuthorityRepository):
            raise TypeError("model invocation ledger requires CoreAuthorityRepository")
        repository.ensure_model_invocation_schema()
        self.repository = repository

    @staticmethod
    def _require_transaction(connection: sqlite3.Connection) -> None:
        if not connection.in_transaction:
            raise RuntimeError("model invocation mutation requires a Core transaction")

    @staticmethod
    def _key(values: Mapping[str, Any]) -> tuple[Any, Any, Any]:
        return (
            values["context_identity"],
            values.get("method", HOST_MODEL_METHOD),
            values["operation_key"],
        )

    @staticmethod
    def _select(connection: sqlite3.Connection, key: tuple[Any, Any, Any]) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM model_invocation WHERE context_identity=? AND method=? AND operation_key=?",
            key,
        ).fetchone()

    def lookup(
        self,
        connection: sqlite3.Connection,
        *,
        context_identity: str,
        operation_key: str,
    ) -> ModelInvocationRecord | None:
        """Read one row on a caller-owned transaction after its fence check."""

        row = self._select(
            connection, (context_identity, HOST_MODEL_METHOD, operation_key)
        )
        return None if row is None else ModelInvocationRecord.from_row(row)

    def get(
        self, *, context_identity: str, operation_key: str
    ) -> ModelInvocationRecord | None:
        with self.repository.read_connection() as connection:
            row = self._select(
                connection, (context_identity, HOST_MODEL_METHOD, operation_key)
            )
        return None if row is None else ModelInvocationRecord.from_row(row)

    def reserve(
        self,
        connection: sqlite3.Connection,
        values: Mapping[str, Any],
    ) -> tuple[ModelInvocationRecord, bool]:
        """T1: reserve one exact operation inside the caller's Core transaction."""

        self._require_transaction(connection)
        if set(values) != set(_RESERVATION_FIELDS):
            raise ValueError("model invocation reservation fields are incomplete")
        if values["method"] != HOST_MODEL_METHOD:
            raise ValueError("model invocation method drift")
        existing = self._select(connection, self._key(values))
        if existing is not None:
            record = ModelInvocationRecord.from_row(existing)
            record.assert_reservation(values)
            return record, False
        conflict = connection.execute(
            "SELECT * FROM model_invocation WHERE workspace_id=? "
            "AND (invocation_id=? OR invocation_key=?) LIMIT 1",
            (
                values["workspace_id"],
                values["invocation_id"],
                values["invocation_key"],
            ),
        ).fetchone()
        if conflict is not None:
            raise ContractError(
                ErrorCode.DUPLICATE_REQUEST,
                "Workspace invocation_id or invocation_key is already bound",
            )
        columns = (*_RESERVATION_FIELDS, "state", "created_at", "updated_at")
        now = utc_now()
        parameters = tuple(values[field] for field in _RESERVATION_FIELDS) + (
            "reserved",
            now,
            now,
        )
        try:
            connection.execute(
                f"INSERT INTO model_invocation({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                parameters,
            )
        except sqlite3.IntegrityError as exc:
            raise ContractError(
                ErrorCode.DUPLICATE_REQUEST,
                "model invocation reservation conflicted with durable authority",
            ) from exc
        row = self._select(connection, self._key(values))
        if row is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "model invocation reservation disappeared")
        return ModelInvocationRecord.from_row(row), True

    def mark_dispatching(
        self,
        connection: sqlite3.Connection,
        reservation: Mapping[str, Any],
        provider_request: Mapping[str, Any],
    ) -> tuple[ModelInvocationRecord, bool]:
        """T2: commit the reserved-to-dispatching barrier before Provider invoke."""

        self._require_transaction(connection)
        provider_request_json = canonical_json_text(provider_request)
        key = self._key(reservation)
        changed = connection.execute(
            "UPDATE model_invocation SET state='dispatching',provider_request_json=?,updated_at=? "
            "WHERE context_identity=? AND method=? AND operation_key=? AND state='reserved'",
            (provider_request_json, utc_now(), *key),
        ).rowcount
        row = self._select(connection, key)
        if row is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "model invocation dispatch row disappeared")
        record = ModelInvocationRecord.from_row(row)
        record.assert_reservation(reservation)
        if changed == 1:
            return record, True
        if record.provider_request != dict(provider_request):
            raise ContractError(
                ErrorCode.DUPLICATE_REQUEST,
                "model invocation Provider request changed after its dispatch barrier",
            )
        return record, False

    def commit_terminal(
        self,
        connection: sqlite3.Connection,
        reservation: Mapping[str, Any],
        terminal: Mapping[str, Any],
    ) -> tuple[ModelInvocationRecord, bool]:
        """T3: publish one receipt-backed terminal decision atomically."""

        self._require_transaction(connection)
        if set(terminal) != set(_TERMINAL_FIELDS):
            raise ValueError("model invocation terminal fields are incomplete")
        state = terminal["state"]
        if state not in MODEL_INVOCATION_TERMINAL_STATES:
            raise ValueError("invalid model invocation terminal state")
        encoded = dict(terminal)
        for field in (
            "provider_success_json",
            "host_result_json",
            "rpc_error_json",
            "uncertainty_json",
        ):
            value = encoded[field]
            encoded[field] = None if value is None else canonical_json_text(value)
        columns = _TERMINAL_FIELDS
        assignments = ",".join(f"{field}=?" for field in columns)
        key = self._key(reservation)
        changed = connection.execute(
            f"UPDATE model_invocation SET {assignments},updated_at=? "
            "WHERE context_identity=? AND method=? AND operation_key=? AND state='dispatching'",
            tuple(encoded[field] for field in columns) + (utc_now(), *key),
        ).rowcount
        row = self._select(connection, key)
        if row is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "model invocation terminal row disappeared")
        record = ModelInvocationRecord.from_row(row)
        record.assert_reservation(reservation)
        if changed == 1:
            return record, True
        expected = {
            field: encoded[field]
            for field in columns
        }
        if any(record.values.get(field) != value for field, value in expected.items()):
            raise ContractError(
                ErrorCode.UNCERTAIN_EXTERNAL_EFFECT,
                "model invocation terminal authority was already decided differently",
            )
        return record, False

    def mark_receiptless_uncertain(
        self,
        connection: sqlite3.Connection,
        reservation: Mapping[str, Any],
        *,
        recovered: bool,
    ) -> tuple[ModelInvocationRecord, bool]:
        """Close a post-barrier row without fabricating receipt evidence."""

        self._require_transaction(connection)
        rpc_error, uncertainty = receiptless_uncertainty(recovered=recovered)
        key = self._key(reservation)
        changed = connection.execute(
            "UPDATE model_invocation SET state='uncertain',rpc_error_json=?,"
            "uncertainty_json=?,updated_at=? WHERE context_identity=? AND method=? "
            "AND operation_key=? AND state='dispatching'",
            (
                canonical_json_text(rpc_error),
                canonical_json_text(uncertainty),
                utc_now(),
                *key,
            ),
        ).rowcount
        row = self._select(connection, key)
        if row is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "model invocation uncertainty row disappeared")
        record = ModelInvocationRecord.from_row(row)
        record.assert_reservation(reservation)
        if changed == 0 and not (
            record.state == "uncertain"
            and not record.receipt_backed
            and _decode_canonical(record.values.get("rpc_error_json"), "rpc_error_json")
            == rpc_error
            and _decode_canonical(record.values.get("uncertainty_json"), "uncertainty_json")
            == uncertainty
        ):
            raise ContractError(
                ErrorCode.UNCERTAIN_EXTERNAL_EFFECT,
                "model invocation can no longer be closed as receipt-less uncertain",
            )
        return record, changed == 1

    def recover_dispatching(self) -> int:
        """On process restart, close every post-barrier row without resending."""

        rpc_error, uncertainty = receiptless_uncertainty(recovered=True)
        with self.repository.transaction() as connection:
            changed = connection.execute(
                "UPDATE model_invocation SET state='uncertain',rpc_error_json=?,"
                "uncertainty_json=?,updated_at=? WHERE state='dispatching'",
                (
                    canonical_json_text(rpc_error),
                    canonical_json_text(uncertainty),
                    utc_now(),
                ),
            ).rowcount
        return int(changed)


__all__ = [
    "HOST_MODEL_METHOD",
    "MODEL_INVOCATION_STATES",
    "MODEL_INVOCATION_TERMINAL_STATES",
    "ModelInvocationLedger",
    "ModelInvocationRecord",
    "canonical_json_text",
    "receiptless_uncertainty",
]
