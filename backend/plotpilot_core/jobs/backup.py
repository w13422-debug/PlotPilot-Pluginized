"""P3 job-runtime backup barrier over the accepted Core repository lock."""

from __future__ import annotations

import base64
import hashlib
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_plugin_sdk import (
    ContractError,
    ContractValidationError,
    ErrorCode,
    canonical_bytes,
    sha256_hex,
)

if TYPE_CHECKING:
    from backend.plotpilot_core.backup.models import BackupBarrier, BackupMode

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_TIMESTAMP = re.compile(
    r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|"
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\."
    r"(?!000)[0-9]{3}Z)$"
)
_TOKEN = re.compile(r"^job-runtime-([1-9][0-9]*)-([0-9a-f]{12})-([0-9a-f]{64})$")
_MODES = frozenset({"full", "data", "workspace"})
_DURABLE_TABLES = (
    "execution_job",
    "execution_step",
    "execution_attempt",
    "execution_checkpoint",
    "execution_checkpoint_operation",
    "execution_control_operation",
    "execution_job_event",
    "execution_core_event",
    "execution_candidate_binding",
    "candidate",
    "candidate_batch_operation",
    "chapter_writer_fence",
    "chapter_candidate_authority",
)


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ContractValidationError(f"{label} is not a v1 ID")
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ContractError(
        ErrorCode.ASSET_ERROR,
        f"unsupported SQLite value in durable job generation: {type(value).__name__}",
    )


@dataclass(frozen=True, slots=True)
class DurableJobGeneration:
    """Non-sensitive summary of the rows frozen by a backup barrier."""

    barrier_token: str
    backup_epoch: int
    job_count: int
    checkpoint_count: int
    durable_generation_hash: str


@dataclass(frozen=True, slots=True)
class _GenerationProjection:
    job_count: int
    checkpoint_count: int
    core_event_high_water: int
    digest: str


class JobRuntimeBackupContributor:
    """Freeze process-local Core writers and attest the durable job generation.

    The full row projection exists only long enough to derive a canonical
    digest.  Callers receive no database paths, files, or row material.
    """

    def __init__(self, repository: CoreAuthorityRepository) -> None:
        if not callable(getattr(repository, "read_connection", None)):
            raise TypeError("job runtime backup requires CoreAuthorityRepository")
        self.repository = repository

    @staticmethod
    def _table_rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            return []
        rows = [
            {name: _json_value(value) for name, value in dict(row).items()}
            for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()
        ]
        rows.sort(key=canonical_bytes)
        return rows

    @classmethod
    def _project(cls, connection: sqlite3.Connection) -> _GenerationProjection:
        tables = [
            {"table": table, "rows": cls._table_rows(connection, table)}
            for table in _DURABLE_TABLES
        ]
        projection = {
            "schema": "job-runtime-durable-generation/v1",
            "tables": tables,
        }
        digest = sha256_hex(
            b"job-runtime-durable-generation/v1\n" + canonical_bytes(projection)
        )
        job_count = len(tables[0]["rows"])
        checkpoint_count = len(tables[3]["rows"])
        core_event_row = connection.execute(
            "SELECT COALESCE(MAX(core_event_seq),0) FROM execution_core_event"
        ).fetchone()
        return _GenerationProjection(
            job_count=job_count,
            checkpoint_count=checkpoint_count,
            core_event_high_water=int(core_event_row[0]),
            digest=digest,
        )

    @staticmethod
    def _validate_request(
        *,
        backup_epoch: int,
        created_at: str,
        mode: BackupMode,
        workspace_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        if (
            isinstance(backup_epoch, bool)
            or not isinstance(backup_epoch, int)
            or backup_epoch < 1
        ):
            raise ContractValidationError("backup_epoch must be a positive integer")
        if not isinstance(created_at, str) or _TIMESTAMP.fullmatch(created_at) is None:
            raise ContractValidationError("created_at is not a v1 timestamp")
        if mode not in _MODES:
            raise ContractValidationError("backup mode is not closed")
        if not isinstance(workspace_ids, tuple):
            raise ContractValidationError("workspace_ids must be an immutable tuple")
        normalized = tuple(
            _require_id(value, "workspace_id") for value in workspace_ids
        )
        if len(set(normalized)) != len(normalized):
            raise ContractValidationError("workspace_ids contains duplicates")
        return normalized

    @staticmethod
    def _token(
        *, backup_epoch: int, workspace_ids: tuple[str, ...], digest: str
    ) -> str:
        scope_hash = hashlib.sha256(canonical_bytes(list(workspace_ids))).hexdigest()[
            :12
        ]
        return f"job-runtime-{backup_epoch}-{scope_hash}-{digest}"

    @contextmanager
    def quiesce(
        self,
        *,
        backup_epoch: int,
        created_at: str,
        mode: BackupMode,
        workspace_ids: tuple[str, ...],
    ) -> Iterator[BackupBarrier]:
        from backend.plotpilot_core.backup.models import BackupBarrier

        normalized = self._validate_request(
            backup_epoch=backup_epoch,
            created_at=created_at,
            mode=mode,
            workspace_ids=workspace_ids,
        )
        # read_connection holds the repository's re-entrant writer lock for
        # the complete caller-owned backup window.  capture_durable_generation
        # can therefore re-enter it without opening another transaction.
        with self.repository.read_connection() as connection:
            projection = self._project(connection)
            yield BackupBarrier(
                token=self._token(
                    backup_epoch=backup_epoch,
                    workspace_ids=normalized,
                    digest=projection.digest,
                ),
                backup_epoch=backup_epoch,
                core_event_high_water=projection.core_event_high_water,
                created_at=created_at,
            )

    hold_for_backup = quiesce

    def capture_durable_generation(
        self, *, barrier: BackupBarrier
    ) -> DurableJobGeneration:
        from backend.plotpilot_core.backup.models import BackupBarrier

        if not isinstance(barrier, BackupBarrier) or not barrier.token:
            raise ContractValidationError("backup barrier is absent")
        if isinstance(barrier.backup_epoch, bool) or barrier.backup_epoch < 1:
            raise ContractValidationError("backup barrier epoch is invalid")
        token = _TOKEN.fullmatch(barrier.token)
        if token is None or int(token.group(1)) != barrier.backup_epoch:
            raise ContractValidationError("backup barrier token is invalid")
        with self.repository.read_connection() as connection:
            projection = self._project(connection)
        if token.group(3) != projection.digest:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "durable job generation changed outside the backup barrier",
            )
        if projection.core_event_high_water != barrier.core_event_high_water:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Core event high-water changed outside the backup barrier",
            )
        return DurableJobGeneration(
            barrier_token=barrier.token,
            backup_epoch=barrier.backup_epoch,
            job_count=projection.job_count,
            checkpoint_count=projection.checkpoint_count,
            durable_generation_hash=projection.digest,
        )


__all__ = ["DurableJobGeneration", "JobRuntimeBackupContributor"]
