"""Injectable repositories for immutable model-profile revisions.

The SQLite implementation owns only the small model-profile table it creates
for the supplied connection/path.  It deliberately does not import or run
the P1 Core migration runner and exposes no update/delete operation.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Protocol, runtime_checkable

from .profile import (
    ModelProfileConflictError,
    ModelProfileNotFoundError,
    ModelProfileRevision,
    ModelProfileValidationError,
)


@runtime_checkable
class ModelProfileRepository(Protocol):
    """Storage port for append-only model profile revisions."""

    def append(self, revision: ModelProfileRevision) -> ModelProfileRevision: ...

    def get(self, revision_id: str) -> ModelProfileRevision: ...

    def get_current(self, profile_id: str) -> ModelProfileRevision: ...

    def list(self, profile_id: str | None = None) -> tuple[ModelProfileRevision, ...]: ...


def _check_revision(value: ModelProfileRevision) -> None:
    if not isinstance(value, ModelProfileRevision):
        raise ModelProfileValidationError("repository accepts ModelProfileRevision values only")


def _check_parent(
    revision: ModelProfileRevision,
    parent: ModelProfileRevision | None,
    *,
    existing_count: int,
) -> None:
    """Validate the append-only chain relation before insertion."""

    if revision.parent_revision_id is None:
        if existing_count:
            raise ModelProfileConflictError(
                "a profile with existing history must append from its current parent revision"
            )
        if revision.revision_number != 1:
            raise ModelProfileValidationError("a root model profile revision must have revision_number=1")
        return
    if parent is None:
        raise ModelProfileValidationError("parent_revision_id does not identify an existing revision")
    if parent.profile_id != revision.profile_id:
        raise ModelProfileValidationError("parent revision belongs to a different profile")
    if parent.provider_identity != revision.provider_identity:
        raise ModelProfileValidationError("provider identity cannot change within a profile history")
    if revision.revision_number != parent.revision_number + 1:
        raise ModelProfileValidationError("revision_number must be exactly parent revision_number + 1")


class InMemoryModelProfileRepository:
    """Small deterministic repository useful for unit tests and composition."""

    def __init__(self) -> None:
        self._revisions: dict[str, ModelProfileRevision] = {}
        self._by_profile: dict[str, list[str]] = {}
        self._lock = RLock()

    def append(self, revision: ModelProfileRevision) -> ModelProfileRevision:
        _check_revision(revision)
        with self._lock:
            existing = self._revisions.get(revision.revision_id)
            if existing is not None:
                if existing.revision_hash == revision.revision_hash:
                    return existing
                raise ModelProfileConflictError()
            history_ids = self._by_profile.get(revision.profile_id, [])
            parent = self._revisions.get(revision.parent_revision_id) if revision.parent_revision_id else None
            _check_parent(revision, parent, existing_count=len(history_ids))
            # A revision number is unique within a profile even when a caller
            # submits a malformed parent relationship.
            if any(self._revisions[item].revision_number == revision.revision_number for item in history_ids):
                raise ModelProfileConflictError("revision_number is already used by this profile")
            self._revisions[revision.revision_id] = revision
            history_ids.append(revision.revision_id)
            self._by_profile[revision.profile_id] = history_ids
            return revision

    create = append
    create_revision = append
    append_revision = append
    save = append

    def get(self, revision_id: str) -> ModelProfileRevision:
        with self._lock:
            try:
                return self._revisions[revision_id]
            except KeyError as exc:
                raise ModelProfileNotFoundError(revision_id) from exc

    read = get
    get_revision = get

    def get_current(self, profile_id: str) -> ModelProfileRevision:
        with self._lock:
            history = self._by_profile.get(profile_id)
            if not history:
                raise ModelProfileNotFoundError(profile_id)
            return self._revisions[history[-1]]

    current = get_current
    latest = get_current

    def list(self, profile_id: str | None = None) -> tuple[ModelProfileRevision, ...]:
        with self._lock:
            if profile_id is None:
                values = sorted(self._revisions.values(), key=lambda value: (value.profile_id, value.revision_number, value.revision_id))
            else:
                values = [self._revisions[item] for item in self._by_profile.get(profile_id, [])]
            return tuple(values)

    list_revisions = list
    history = list

    def close(self) -> None:
        """Match the SQLite adapter's lifecycle without invalidating memory."""

    def __len__(self) -> int:
        return len(self._revisions)


class SQLiteModelProfileRepository:
    """Append-only SQLite adapter injected with a connection or database path."""

    TABLE = "model_profile_revision"

    def __init__(
        self,
        connection_or_path: sqlite3.Connection | str | Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        path: str | Path | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        supplied = [value is not None for value in (connection_or_path, connection, path, db_path)]
        if sum(supplied) > 1:
            raise ValueError("provide exactly one connection or path")
        source: sqlite3.Connection | str | Path | None = connection_or_path
        if connection is not None:
            source = connection
        elif path is not None:
            source = path
        elif db_path is not None:
            source = db_path
        if source is None:
            raise ValueError("a sqlite connection or path is required")
        self._lock = RLock()
        self._owns_connection = not isinstance(source, sqlite3.Connection)
        if self._owns_connection:
            self.database = str(source)
            self._connection = sqlite3.connect(self.database, isolation_level=None, check_same_thread=False)
        else:
            self.database = None
            self._connection = source
        self._initialize()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    @property
    def db_path(self) -> str | None:
        return self.database

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE} (
                    revision_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    revision_number INTEGER NOT NULL,
                    parent_revision_id TEXT,
                    provider_plugin_id TEXT NOT NULL,
                    provider_release_id TEXT NOT NULL,
                    revision_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(profile_id, revision_number)
                )
                """
            )
            self._connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self.TABLE}_profile ON {self.TABLE}(profile_id, revision_number)"
            )
            # Repository code never updates/deletes rows.  These guards make
            # accidental use of this adapter as a mutable config table fail
            # closed while retaining a deliberately tiny schema.
            self._connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_{self.TABLE}_immutable_update
                BEFORE UPDATE ON {self.TABLE}
                BEGIN SELECT RAISE(ABORT, 'model profile revisions are immutable'); END
                """
            )
            self._connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_{self.TABLE}_immutable_delete
                BEFORE DELETE ON {self.TABLE}
                BEGIN SELECT RAISE(ABORT, 'model profile revisions are append-only'); END
                """
            )
            if self._connection.in_transaction:
                return
            self._connection.commit()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            already_in_transaction = self._connection.in_transaction
            if not already_in_transaction:
                self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
                if not already_in_transaction:
                    self._connection.commit()
            except BaseException:
                if not already_in_transaction:
                    self._connection.rollback()
                raise

    @staticmethod
    def _payload(value: ModelProfileRevision) -> str:
        # canonical_bytes is the SDK's single JSON identity/serialization
        # boundary; it is decoded only for SQLite TEXT storage.
        try:
            from backend.plotpilot_plugin_sdk.canonical import canonical_bytes
        except ModuleNotFoundError:  # pragma: no cover - alternate runner
            from plotpilot_plugin_sdk.canonical import canonical_bytes

        return canonical_bytes(value.to_dict()).decode("utf-8")

    def _row_to_revision(self, row: sqlite3.Row | tuple[object, ...]) -> ModelProfileRevision:
        payload_raw = row[8]
        if not isinstance(payload_raw, str):
            raise ModelProfileValidationError("stored model profile payload is not text")
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError as exc:
            raise ModelProfileValidationError("stored model profile payload is invalid JSON") from exc
        revision = ModelProfileRevision.from_dict(payload)
        columns = (row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7])
        expected = (
            revision.revision_id,
            revision.profile_id,
            revision.revision_number,
            revision.parent_revision_id,
            revision.provider_plugin_id,
            revision.provider_release_id,
            revision.revision_hash,
            revision.created_at,
        )
        if columns != expected:
            raise ModelProfileValidationError("stored model profile row does not match its canonical payload")
        return revision

    def _get_no_lock(self, revision_id: str) -> ModelProfileRevision | None:
        row = self._connection.execute(
            f"SELECT revision_id, profile_id, revision_number, parent_revision_id, provider_plugin_id, provider_release_id, revision_hash, created_at, payload_json FROM {self.TABLE} WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        return None if row is None else self._row_to_revision(row)

    def append(self, revision: ModelProfileRevision) -> ModelProfileRevision:
        _check_revision(revision)
        with self._write_transaction() as connection:
            existing = self._get_no_lock(revision.revision_id)
            if existing is not None:
                if existing.revision_hash == revision.revision_hash:
                    return existing
                raise ModelProfileConflictError()
            count = connection.execute(
                f"SELECT COUNT(*) FROM {self.TABLE} WHERE profile_id=?", (revision.profile_id,)
            ).fetchone()[0]
            parent = self._get_no_lock(revision.parent_revision_id) if revision.parent_revision_id else None
            _check_parent(revision, parent, existing_count=int(count))
            try:
                connection.execute(
                    f"""
                    INSERT INTO {self.TABLE}(
                        revision_id, profile_id, revision_number, parent_revision_id,
                        provider_plugin_id, provider_release_id, revision_hash,
                        created_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision.revision_id,
                        revision.profile_id,
                        revision.revision_number,
                        revision.parent_revision_id,
                        revision.provider_plugin_id,
                        revision.provider_release_id,
                        revision.revision_hash,
                        revision.created_at,
                        self._payload(revision),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ModelProfileConflictError("model profile revision conflicts with existing history") from exc
            return revision

    create = append
    create_revision = append
    append_revision = append
    save = append

    def get(self, revision_id: str) -> ModelProfileRevision:
        with self._lock:
            value = self._get_no_lock(revision_id)
            if value is None:
                raise ModelProfileNotFoundError(revision_id)
            return value

    read = get
    get_revision = get

    def get_current(self, profile_id: str) -> ModelProfileRevision:
        with self._lock:
            row = self._connection.execute(
                f"SELECT revision_id, profile_id, revision_number, parent_revision_id, provider_plugin_id, provider_release_id, revision_hash, created_at, payload_json FROM {self.TABLE} WHERE profile_id=? ORDER BY revision_number DESC LIMIT 1",
                (profile_id,),
            ).fetchone()
            if row is None:
                raise ModelProfileNotFoundError(profile_id)
            return self._row_to_revision(row)

    current = get_current
    latest = get_current

    def list(self, profile_id: str | None = None) -> tuple[ModelProfileRevision, ...]:
        with self._lock:
            if profile_id is None:
                rows = self._connection.execute(
                    f"SELECT revision_id, profile_id, revision_number, parent_revision_id, provider_plugin_id, provider_release_id, revision_hash, created_at, payload_json FROM {self.TABLE} ORDER BY profile_id, revision_number, revision_id"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    f"SELECT revision_id, profile_id, revision_number, parent_revision_id, provider_plugin_id, provider_release_id, revision_hash, created_at, payload_json FROM {self.TABLE} WHERE profile_id=? ORDER BY revision_number, revision_id",
                    (profile_id,),
                ).fetchall()
            return tuple(self._row_to_revision(row) for row in rows)

    list_revisions = list
    history = list

    def close(self) -> None:
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> "SQLiteModelProfileRepository":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


# Naming aliases kept deliberately boring for downstream integration.
ModelProfileRevisionRepository = ModelProfileRepository
InMemoryModelProfileRevisionRepository = InMemoryModelProfileRepository
SQLiteModelProfileRevisionRepository = SQLiteModelProfileRepository
ProviderConfigRepository = ModelProfileRepository


__all__ = [
    "InMemoryModelProfileRepository",
    "InMemoryModelProfileRevisionRepository",
    "ModelProfileRepository",
    "ModelProfileRevisionRepository",
    "ProviderConfigRepository",
    "SQLiteModelProfileRepository",
    "SQLiteModelProfileRevisionRepository",
]
