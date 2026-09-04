"""SQLite-backed authority for plugin install, Generation and LKG lifecycle.

The repository deliberately accepts an existing connection.  P1 may therefore
enclose these mutations in its authoritative transaction; when no transaction
is active this module opens one with ``BEGIN IMMEDIATE``.  Public lifecycle and
Generation objects are stored as immutable/canonical JSON while mutable
pointers and transition state remain in dedicated rows.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from plotpilot_plugin_sdk.canonical import canonical_bytes
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode
from plotpilot_plugin_sdk.verifier import assert_valid

from ..generation import GenerationState, validate_generation

EventWriter = Callable[[sqlite3.Connection, str | None, str, str], None]
GenerationGuard = Callable[[sqlite3.Connection, Mapping[str, Any], str], None]
PinReleaser = Callable[[sqlite3.Connection, str], None]
TransactionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


class LifecycleError(ContractError):
    """A durable lifecycle mutation failed closed."""

    def __init__(
        self,
        message: str,
        *,
        code: int | ErrorCode = ErrorCode.INVALID_TRANSITION,
        details: Any = None,
    ) -> None:
        super().__init__(code, message, details=details)


@dataclass(frozen=True)
class ReconciliationDecision:
    install_operation_id: str
    state: str
    action: str
    reason: str


@dataclass(frozen=True)
class VerifiedQualification:
    qualification_id: str
    generation_id: str
    health_result_asset_id: str
    passed: bool


QualificationVerifier = Callable[
    [sqlite3.Connection, Mapping[str, Any], Mapping[str, Any]],
    VerifiedQualification,
]


def require_verified_qualification(
    value: object,
    generation: Mapping[str, Any],
) -> VerifiedQualification:
    if (
        not isinstance(value, VerifiedQualification)
        or not value.qualification_id
        or not value.passed
        or value.generation_id != generation["generation_id"]
        or value.health_result_asset_id != generation["health_result_asset_id"]
    ):
        raise LifecycleError(
            "qualification authority returned an invalid Generation/health-bound result",
            code=ErrorCode.RESULT_CONTRACT_MISMATCH,
        )
    return value


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS p2_plugin_generation(
        generation_id TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS p2_plugin_generation_pointer(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        current_generation_id TEXT NULL,
        lkg_generation_id TEXT NULL,
        safe_mode INTEGER NOT NULL DEFAULT 0 CHECK(safe_mode IN (0,1)),
        safe_mode_reason TEXT NULL,
        revision INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    INSERT OR IGNORE INTO p2_plugin_generation_pointer(singleton) VALUES(1)
    """,
    """
    CREATE TABLE IF NOT EXISTS p2_plugin_install_attempt(
        install_operation_id TEXT PRIMARY KEY,
        request_hash TEXT NOT NULL,
        request_json TEXT NOT NULL,
        transition_json TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS p2_plugin_release_retirement(
        release_id TEXT PRIMARY KEY,
        retirement_json TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS p2_plugin_retirement_attention(
        release_id TEXT PRIMARY KEY,
        reason TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS p2_plugin_release_pin(
        pin_id TEXT PRIMARY KEY,
        release_id TEXT NOT NULL,
        retire_epoch INTEGER NOT NULL,
        pin_json TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(release_id) REFERENCES p2_plugin_release_retirement(release_id)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS p2_plugin_release_active_pin_owner
        ON p2_plugin_release_pin(release_id,retire_epoch,
             json_extract(pin_json,'$.pin_kind'),json_extract(pin_json,'$.owner_id'))
     WHERE json_extract(pin_json,'$.released_at') IS NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS p2_plugin_shadow_generation(
        shadow_data_generation_id TEXT PRIMARY KEY,
        install_operation_id TEXT NOT NULL UNIQUE,
        release_id TEXT NOT NULL,
        state TEXT NOT NULL,
        db_lease_id TEXT NOT NULL,
        db_lease_epoch INTEGER NOT NULL,
        owner_instance_id TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        renewed_at TEXT NOT NULL,
        lease_state TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0
    )
    """,
)


@dataclass(frozen=True, slots=True)
class LifecycleMigration:
    migration_id: str
    sql: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


LIFECYCLE_MIGRATIONS = (
    LifecycleMigration(
        "0100-plugin-lifecycle-v1",
        ";\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS) + ";",
    ),
)


_NORMAL_NEXT: dict[str, frozenset[str]] = {
    "selected": frozenset({"staged"}),
    "staged": frozenset({"package_published"}),
    "package_published": frozenset({"env_prepared"}),
    "env_prepared": frozenset({"shadow_prepared"}),
    "shadow_prepared": frozenset({"migrated"}),
    "migrated": frozenset({"settings_validated"}),
    # Authority-bearing edges are deliberately absent.  Qualification,
    # current-pointer commit, LKG pending and LKG promotion each have a
    # dedicated repository method so the generic API cannot bypass their
    # verifier/guard/pointer side effects.
    "settings_validated": frozenset(),
    "qualified": frozenset({"pending_apply"}),
    "pending_apply": frozenset(),
    "current_committed": frozenset(),
    "lkg_pending": frozenset(),
}
_PRE_ACTIVATION = frozenset(
    {
        "selected",
        "staged",
        "package_published",
        "env_prepared",
        "shadow_prepared",
        "migrated",
        "settings_validated",
        "qualified",
        "pending_apply",
    }
)
_TERMINAL = frozenset(
    {"lkg_promoted", "failed", "superseded", "rolled_back", "safe_mode"}
)
_ADVANCE_FIELDS: dict[str, frozenset[str]] = {
    "staged": frozenset({"package_store_status"}),
    "package_published": frozenset({"package_store_status"}),
    "env_prepared": frozenset(),
    "shadow_prepared": frozenset({"shadow_data_generation_id"}),
    "migrated": frozenset(),
    "settings_validated": frozenset({"target_settings_revision_ids"}),
    "pending_apply": frozenset(),
    "lkg_pending": frozenset(),
}


def utc_now() -> str:
    value = datetime.now(UTC)
    if value.microsecond // 1000 == 0:
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _dump(value: Mapping[str, Any]) -> str:
    return canonical_bytes(dict(value)).decode("utf-8")


def _load(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise LifecycleError("stored lifecycle JSON is not an object")
    return parsed


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(dict(value))).hexdigest()


def validate_transition(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    assert_valid("plugin-lifecycle-transition/v1", result)
    settings = result["target_settings_revision_ids"]
    plugin_ids = [item["plugin_id"] for item in settings]
    if plugin_ids != sorted(plugin_ids, key=lambda item: item.encode("utf-8")) or len(
        plugin_ids
    ) != len(set(plugin_ids)):
        raise LifecycleError(
            "target settings revisions must be unique and sorted by plugin_id"
        )
    attempt = result["rollback_attempt"]
    token = result["rollback_token"]
    if attempt == 0 and token is not None:
        raise LifecycleError("an unarmed rollback cannot carry a token")
    if result["state"] in {"rollback_armed", "rolled_back"} and (
        attempt != 1 or token is None
    ):
        raise LifecycleError("rollback recovery state requires its one durable token")
    state = result["state"]
    if result["target_generation_id"] is None:
        raise LifecycleError("every install attempt requires a target Generation")
    required_store_status = {
        "selected": "absent",
        "staged": "staged",
        "package_published": "published",
        "env_prepared": "published",
        "shadow_prepared": "published",
        "migrated": "published",
        "settings_validated": "published",
        "qualified": "published",
        "pending_apply": "published",
        "current_committed": "published",
        "lkg_pending": "published",
        "lkg_promoted": "published",
        "rollback_armed": "published",
        "rolled_back": "published",
        "safe_mode": "published",
        "superseded": "published",
    }.get(state)
    if (
        required_store_status is not None
        and result["package_store_status"] != required_store_status
    ):
        raise LifecycleError(
            f"{state} requires package_store_status={required_store_status}"
        )
    if state in {"selected", "staged", "package_published", "env_prepared"} and (
        result["shadow_data_generation_id"] is not None
    ):
        raise LifecycleError(f"{state} cannot reference a shadow Generation")
    if (
        state
        in {
            "selected",
            "staged",
            "package_published",
            "env_prepared",
            "shadow_prepared",
            "migrated",
        }
        and result["target_settings_revision_ids"]
    ):
        raise LifecycleError(f"{state} cannot reference validated Settings")
    if state == "lkg_promoted":
        if result["qualification_id"] is None:
            raise LifecycleError("lkg_promoted requires its qualification identity")
    elif result["qualification_id"] is not None:
        raise LifecycleError(f"{state} cannot carry an LKG qualification identity")
    failure_states = {"failed", "superseded", "safe_mode"}
    if state in failure_states and result["failure_code"] is None:
        raise LifecycleError(f"{state} requires a failure code")
    if state not in failure_states and result["failure_code"] is not None:
        raise LifecycleError(f"{state} cannot carry a failure code")
    return result


class LifecycleRepository:
    """Durable lifecycle store with CAS and replay-safe operations."""

    _REQUIRED_TABLES = frozenset(
        {
            "p2_plugin_generation",
            "p2_plugin_generation_pointer",
            "p2_plugin_install_attempt",
            "p2_plugin_release_retirement",
            "p2_plugin_retirement_attention",
            "p2_plugin_release_pin",
            "p2_plugin_shadow_generation",
        }
    )

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction_factory: TransactionFactory | None = None,
        core_authority_binding: object | None = None,
    ) -> None:
        if transaction_factory is not None and core_authority_binding is None:
            raise LifecycleError(
                "a shared lifecycle repository requires its Core authority binding"
            )
        self.connection = connection
        # This public identity is the only production proof that P1 and P2 were
        # composed over the same CoreAuthorityRepository object.  Standalone
        # P2 tests may omit it, but such a repository cannot enter a backup
        # contributor or any other authority-bound production seam.
        self.core_authority_binding = core_authority_binding
        self._transaction_factory = transaction_factory
        self._lock = threading.RLock()
        self._transaction_owner: int | None = None
        self._transaction_depth = 0
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = self._REQUIRED_TABLES.difference(tables)
        if missing:
            raise LifecycleError(
                "lifecycle schema migration has not been applied by the authoritative owner",
                details={"missing_tables": sorted(missing)},
            )

    @classmethod
    def initialize_standalone_schema_for_tests(
        cls, connection: sqlite3.Connection
    ) -> None:
        """Explicit test-only bootstrap; production schema belongs to P1 migrations."""
        if connection.in_transaction:
            raise LifecycleError("test schema bootstrap requires an idle connection")
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                "INSERT OR IGNORE INTO p2_plugin_generation_pointer(singleton) VALUES(1)"
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self._transaction_factory is not None:
            with self._transaction_factory() as connection:
                if connection is not self.connection:
                    raise LifecycleError(
                        "shared transaction owner returned a different connection"
                    )
                yield connection
            return
        with self._lock:
            thread_id = threading.get_ident()
            nested = self._transaction_depth > 0
            if nested:
                if self._transaction_owner != thread_id:
                    raise LifecycleError(
                        "lifecycle transaction is owned by another thread"
                    )
                self._transaction_depth += 1
            else:
                if self.connection.in_transaction:
                    raise LifecycleError(
                        "external SQLite transaction owner is not composed; refusing unsafe join"
                    )
                self.connection.execute("BEGIN IMMEDIATE")
                self._transaction_owner = thread_id
                self._transaction_depth = 1
            try:
                yield self.connection
                if not nested:
                    self.connection.commit()
            except BaseException:
                if not nested:
                    self.connection.rollback()
                raise
            finally:
                self._transaction_depth -= 1
                if not nested:
                    self._transaction_owner = None

    def create_or_recover_attempt(
        self,
        transition: Mapping[str, Any],
        *,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        value = validate_transition(transition)
        if value["state"] != "selected":
            raise LifecycleError("a new install attempt must begin in selected")
        operation_id = value["install_operation_id"]
        request_hash = _digest(request)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT request_hash,request_json,transition_json FROM p2_plugin_install_attempt WHERE install_operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is not None:
                if row[0] != request_hash:
                    raise LifecycleError(
                        "install_operation_id was reused with different input",
                        code=ErrorCode.DUPLICATE_REQUEST,
                    )
                if row[1] is None:
                    raise LifecycleError(
                        "legacy install attempt lacks its canonical request; reconciliation is required"
                    )
                if _load(row[1]) != dict(request):
                    raise LifecycleError(
                        "install_operation_id was replayed with different canonical input",
                        code=ErrorCode.DUPLICATE_REQUEST,
                    )
                existing = validate_transition(_load(row[2]))
                immutable_fields = (
                    "base_generation_id",
                    "base_lkg_generation_id",
                    "target_generation_id",
                )
                if any(existing[field] != value[field] for field in immutable_fields):
                    raise LifecycleError(
                        "install_operation_id was replayed with different frozen pointers",
                        code=ErrorCode.DUPLICATE_REQUEST,
                    )
                return existing
            connection.execute(
                "INSERT INTO p2_plugin_install_attempt(install_operation_id,request_hash,request_json,transition_json) VALUES(?,?,?,?)",
                (operation_id, request_hash, _dump(request), _dump(value)),
            )
        return value

    def get_attempt(self, install_operation_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT transition_json FROM p2_plugin_install_attempt WHERE install_operation_id=?",
            (install_operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(install_operation_id)
        return validate_transition(_load(row[0]))

    def get_attempt_request(self, install_operation_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT request_json FROM p2_plugin_install_attempt WHERE install_operation_id=?",
            (install_operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(install_operation_id)
        if row[0] is None:
            raise LifecycleError(
                "legacy install attempt lacks its canonical request; reconciliation is required"
            )
        return _load(row[0])

    def list_attempts(
        self, *, include_terminal: bool = True
    ) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            "SELECT transition_json FROM p2_plugin_install_attempt ORDER BY install_operation_id"
        ).fetchall()
        values = tuple(validate_transition(_load(row[0])) for row in rows)
        if include_terminal:
            return values
        return tuple(value for value in values if value["state"] not in _TERMINAL)

    def _put_transition(
        self,
        connection: sqlite3.Connection,
        previous: Mapping[str, Any],
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = validate_transition(value)
        operation_id = result["install_operation_id"]
        changed = connection.execute(
            """
            UPDATE p2_plugin_install_attempt
               SET transition_json=?,revision=revision+1
             WHERE install_operation_id=? AND transition_json=?
            """,
            (_dump(result), operation_id, _dump(previous)),
        ).rowcount
        if changed != 1:
            raise LifecycleError("install transition compare-and-swap failed")
        return result

    @staticmethod
    def _allowed(previous: str, target: str) -> bool:
        return target in _NORMAL_NEXT.get(previous, frozenset())

    def advance_attempt(
        self,
        install_operation_id: str,
        *,
        expected_state: str,
        target_state: str,
        updates: Mapping[str, Any] | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        patch = dict(updates or {})
        permitted = _ADVANCE_FIELDS.get(target_state, frozenset())
        unexpected = set(patch).difference(permitted)
        if unexpected:
            raise LifecycleError(
                f"transition {target_state} cannot replace {sorted(unexpected)}"
            )
        with self.transaction() as connection:
            current = self.get_attempt(install_operation_id)
            if current["state"] == target_state:
                if all(current.get(key) == value for key, value in patch.items()):
                    return current
                raise LifecycleError(
                    "replayed transition has different durable fields",
                    code=ErrorCode.DUPLICATE_REQUEST,
                )
            if current["state"] != expected_state:
                raise LifecycleError(
                    "install transition state compare-and-swap failed",
                    details={"expected": expected_state, "actual": current["state"]},
                )
            if not self._allowed(expected_state, target_state):
                raise LifecycleError(
                    f"invalid install transition {expected_state} -> {target_state}"
                )
            result = copy.deepcopy(current)
            result.update(patch)
            result["state"] = target_state
            result["updated_at"] = at or utc_now()
            return self._put_transition(connection, current, result)

    def fail_attempt(
        self,
        install_operation_id: str,
        *,
        expected_state: str,
        failure_code: str,
        pin_releaser: PinReleaser,
        at: str | None = None,
    ) -> dict[str, Any]:
        if expected_state not in _PRE_ACTIVATION or not failure_code:
            raise LifecycleError("only a pre-activation attempt can fail")
        now = at or utc_now()
        with self.transaction() as connection:
            current = self._attempt_for_update(connection, install_operation_id)
            if current["state"] == "failed":
                if current["failure_code"] == failure_code:
                    return current
                raise LifecycleError(
                    "failed transition replay differs",
                    code=ErrorCode.DUPLICATE_REQUEST,
                )
            if current["state"] != expected_state:
                raise LifecycleError("install failure state compare-and-swap failed")
            result = copy.deepcopy(current)
            result.update(state="failed", failure_code=failure_code, updated_at=now)
            result = self._put_transition(connection, current, result)
            pin_releaser(connection, install_operation_id)
            return result

    def qualify_attempt(
        self,
        install_operation_id: str,
        *,
        generation: Mapping[str, Any],
        evidence: Mapping[str, Any],
        qualification_verifier: QualificationVerifier,
        execution_guard: GenerationGuard,
        at: str | None = None,
    ) -> dict[str, Any]:
        """Consume qualification authority and executable pins in one CAS.

        This edge intentionally cannot be expressed through ``advance_attempt``.
        The guard is the retirement/package authority seam and runs in the same
        SQLite transaction as the durable transition.
        """
        value = validate_generation(generation)
        now = at or utc_now()
        with self.transaction() as connection:
            attempt = self._attempt_for_update(connection, install_operation_id)
            if attempt["target_generation_id"] != value["generation_id"]:
                raise LifecycleError(
                    "qualification target differs from install Generation",
                    code=ErrorCode.INCOMPATIBLE_GENERATION,
                )
            if attempt["state"] not in {"settings_validated", "qualified"}:
                raise LifecycleError(
                    "qualification requires a settings_validated attempt"
                )
            require_verified_qualification(
                qualification_verifier(connection, evidence, value),
                value,
            )
            self._require_frozen_generation_binding(connection, attempt, value)
            self._require_qualified_shadow(connection, attempt)
            digest = _digest(value)
            frozen = connection.execute(
                "SELECT payload_json,payload_hash FROM p2_plugin_generation WHERE generation_id=?",
                (value["generation_id"],),
            ).fetchone()
            if frozen is None:
                connection.execute(
                    "INSERT INTO p2_plugin_generation(generation_id,payload_json,payload_hash) VALUES(?,?,?)",
                    (value["generation_id"], _dump(value), digest),
                )
            elif frozen[1] != digest or _load(frozen[0]) != value:
                raise LifecycleError(
                    "qualified Generation identity is immutable",
                    code=ErrorCode.DUPLICATE_REQUEST,
                )
            execution_guard(connection, value, install_operation_id)
            if attempt["state"] == "qualified":
                return attempt
            qualified = copy.deepcopy(attempt)
            qualified.update(state="qualified", updated_at=now)
            return self._put_transition(connection, attempt, qualified)

    def put_generation(self, generation: Mapping[str, Any]) -> dict[str, Any]:
        value = validate_generation(generation)
        payload_hash = _digest(value)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json,payload_hash FROM p2_plugin_generation WHERE generation_id=?",
                (value["generation_id"],),
            ).fetchone()
            if row is not None:
                if row[1] != payload_hash or _load(row[0]) != value:
                    raise LifecycleError(
                        "Generation identity is immutable",
                        code=ErrorCode.DUPLICATE_REQUEST,
                    )
                return value
            connection.execute(
                "INSERT INTO p2_plugin_generation(generation_id,payload_json,payload_hash) VALUES(?,?,?)",
                (value["generation_id"], _dump(value), payload_hash),
            )
        return value

    def get_generation(self, generation_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT payload_json FROM p2_plugin_generation WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(generation_id)
        return validate_generation(_load(row[0]))

    def generation_state(self) -> GenerationState:
        row = self.connection.execute(
            "SELECT current_generation_id,lkg_generation_id,safe_mode FROM p2_plugin_generation_pointer WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise LifecycleError("Generation pointer row is missing")
        current = None if row[0] is None else self.get_generation(row[0])
        lkg = None if row[1] is None else self.get_generation(row[1])
        return GenerationState(
            current=current, lkg=lkg, safe_mode=bool(row[2]), rollback_consumed=False
        )

    def _attempt_for_update(
        self, connection: sqlite3.Connection, install_operation_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT transition_json FROM p2_plugin_install_attempt WHERE install_operation_id=?",
            (install_operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(install_operation_id)
        return validate_transition(_load(row[0]))

    def _request_for_update(
        self, connection: sqlite3.Connection, install_operation_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT request_json FROM p2_plugin_install_attempt WHERE install_operation_id=?",
            (install_operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(install_operation_id)
        if row[0] is None:
            raise LifecycleError("install attempt lacks its frozen canonical request")
        return _load(row[0])

    def _require_frozen_generation_binding(
        self,
        connection: sqlite3.Connection,
        attempt: Mapping[str, Any],
        generation: Mapping[str, Any],
    ) -> None:
        request = self._request_for_update(
            connection, str(attempt["install_operation_id"])
        )
        plugin_id = request.get("plugin_id")
        release_id = request.get("release_id")
        package_hash = request.get("package_hash")
        if not all(
            isinstance(item, str) and item
            for item in (plugin_id, release_id, package_hash)
        ):
            raise LifecycleError("frozen install request lacks exact package identity")
        members = {member["plugin_id"]: member for member in generation["members"]}
        target_member = members.get(plugin_id)
        if (
            target_member is None
            or target_member["release_id"] != release_id
            or target_member["package_hash"] != package_hash
        ):
            raise LifecycleError(
                "target Generation does not contain the frozen install package member",
                code=ErrorCode.INCOMPATIBLE_GENERATION,
            )
        expected_schema_hash = request.get("settings_schema_hash")
        if target_member["settings_schema_hash"] != expected_schema_hash:
            raise LifecycleError(
                "target member Settings schema differs from the frozen package",
                code=ErrorCode.SETTINGS_INVALID,
            )
        shadow_id = attempt["shadow_data_generation_id"]
        if target_member["data_generation_id"] != shadow_id:
            raise LifecycleError(
                "target member is not bound to the exact qualified shadow Generation",
                code=ErrorCode.INCOMPATIBLE_GENERATION,
            )
        base_members: dict[str, Any] = {}
        if attempt["base_generation_id"] is not None:
            row = connection.execute(
                "SELECT payload_json FROM p2_plugin_generation WHERE generation_id=?",
                (attempt["base_generation_id"],),
            ).fetchone()
            if row is None:
                raise LifecycleError("frozen base Generation is absent")
            base = validate_generation(_load(row[0]))
            base_members = {member["plugin_id"]: member for member in base["members"]}
        unchanged_actual = {
            key: value for key, value in members.items() if key != plugin_id
        }
        unchanged_base = {
            key: value for key, value in base_members.items() if key != plugin_id
        }
        if unchanged_actual != unchanged_base:
            raise LifecycleError(
                "install candidate changed members outside its frozen plugin identity",
                code=ErrorCode.INCOMPATIBLE_GENERATION,
            )
        expected_settings = sorted(
            (
                {
                    "plugin_id": member["plugin_id"],
                    "settings_revision_id": member["global_settings_revision_id"],
                }
                for member in generation["members"]
                if member["global_settings_revision_id"] is not None
            ),
            key=lambda item: item["plugin_id"].encode("utf-8"),
        )
        if attempt["target_settings_revision_ids"] != expected_settings:
            raise LifecycleError(
                "target Settings bindings differ from the frozen validated set",
                code=ErrorCode.SETTINGS_INVALID,
            )

    @staticmethod
    def _require_no_post_activation_competitor(
        connection: sqlite3.Connection, install_operation_id: str
    ) -> None:
        row = connection.execute(
            """
            SELECT install_operation_id FROM p2_plugin_install_attempt
             WHERE install_operation_id<>?
               AND json_extract(transition_json,'$.state') IN
                   ('current_committed','lkg_pending','rollback_armed')
             ORDER BY install_operation_id LIMIT 1
            """,
            (install_operation_id,),
        ).fetchone()
        if row is not None:
            raise LifecycleError(
                "another post-activation attempt must converge before current can advance",
                code=ErrorCode.INVALID_TRANSITION,
                details={"blocking_install_operation_id": str(row[0])},
            )

    def _require_qualified_shadow(
        self, connection: sqlite3.Connection, attempt: Mapping[str, Any]
    ) -> None:
        shadow_id = attempt["shadow_data_generation_id"]
        if shadow_id is None:
            return
        request = self._request_for_update(
            connection, str(attempt["install_operation_id"])
        )
        row = connection.execute(
            """
            SELECT install_operation_id,release_id,state,lease_state
              FROM p2_plugin_shadow_generation
             WHERE shadow_data_generation_id=?
            """,
            (shadow_id,),
        ).fetchone()
        if (
            row is None
            or row[0] != attempt["install_operation_id"]
            or row[1] != request.get("release_id")
            or row[2] != "qualified"
            or row[3] != "released"
        ):
            raise LifecycleError(
                "activation requires its exact immutable qualified shadow",
                code=ErrorCode.MIGRATION_FAILED,
            )

    def commit_current(
        self,
        install_operation_id: str,
        generation: Mapping[str, Any],
        *,
        event_writer: EventWriter,
        execution_guard: GenerationGuard,
        pin_releaser: PinReleaser,
        at: str | None = None,
    ) -> dict[str, Any]:
        """CAS current Generation and attempt state in one SQLite transaction.

        ``event_writer`` is a required P1-owned port.  It runs inside the same
        transaction; an exception rolls back the pointer and transition.
        """
        value = validate_generation(generation)
        now = at or utc_now()
        with self.transaction() as connection:
            attempt = self._attempt_for_update(connection, install_operation_id)
            if attempt["state"] == "current_committed":
                if attempt["target_generation_id"] == value["generation_id"]:
                    pointer = connection.execute(
                        "SELECT current_generation_id FROM p2_plugin_generation_pointer WHERE singleton=1"
                    ).fetchone()
                    if pointer is not None and pointer[0] == value["generation_id"]:
                        return attempt
                raise LifecycleError(
                    "replayed Generation commit does not match durable result",
                    code=ErrorCode.DUPLICATE_REQUEST,
                )
            if attempt["state"] != "pending_apply":
                raise LifecycleError(
                    "Generation commit requires a pending_apply attempt"
                )
            if attempt["target_generation_id"] != value["generation_id"]:
                raise LifecycleError(
                    "attempt target Generation does not match candidate",
                    code=ErrorCode.INCOMPATIBLE_GENERATION,
                )
            if (
                value["base_generation_id"] != attempt["base_generation_id"]
                or value["parent_generation_id"] != attempt["base_generation_id"]
            ):
                raise LifecycleError(
                    "candidate Generation is not bound to the install base",
                    code=ErrorCode.INCOMPATIBLE_GENERATION,
                )
            self._require_frozen_generation_binding(connection, attempt, value)
            self._require_qualified_shadow(connection, attempt)
            existing = connection.execute(
                "SELECT payload_json,payload_hash FROM p2_plugin_generation WHERE generation_id=?",
                (value["generation_id"],),
            ).fetchone()
            digest = _digest(value)
            if existing is None:
                raise LifecycleError(
                    "pending apply lacks its frozen qualified Generation",
                    code=ErrorCode.INCOMPATIBLE_GENERATION,
                )
            if existing[1] != digest or _load(existing[0]) != value:
                raise LifecycleError(
                    "Generation identity is immutable", code=ErrorCode.DUPLICATE_REQUEST
                )
            execution_guard(connection, value, install_operation_id)
            pointer = connection.execute(
                "SELECT current_generation_id,lkg_generation_id FROM p2_plugin_generation_pointer WHERE singleton=1"
            ).fetchone()
            actual = None if pointer is None else pointer[0]
            actual_lkg = None if pointer is None else pointer[1]
            if (
                actual != attempt["base_generation_id"]
                or actual_lkg != attempt["base_lkg_generation_id"]
            ):
                superseded = copy.deepcopy(attempt)
                superseded.update(
                    state="superseded",
                    failure_code="base_generation_or_lkg_changed",
                    updated_at=now,
                )
                self._put_transition(connection, attempt, superseded)
                pin_releaser(connection, install_operation_id)
                return superseded
            self._require_no_post_activation_competitor(
                connection, install_operation_id
            )
            changed = connection.execute(
                """
                UPDATE p2_plugin_generation_pointer
                   SET current_generation_id=?,revision=revision+1
                 WHERE singleton=1 AND current_generation_id IS ? AND lkg_generation_id IS ?
                """,
                (
                    value["generation_id"],
                    attempt["base_generation_id"],
                    attempt["base_lkg_generation_id"],
                ),
            ).rowcount
            if changed != 1:
                raise LifecycleError(
                    "current Generation compare-and-swap failed",
                    code=ErrorCode.INCOMPATIBLE_GENERATION,
                )
            committed = copy.deepcopy(attempt)
            committed.update(state="current_committed", updated_at=now)
            committed = self._put_transition(connection, attempt, committed)
            event_writer(
                connection, actual, value["generation_id"], install_operation_id
            )
            return committed

    def mark_lkg_pending(
        self, install_operation_id: str, *, at: str | None = None
    ) -> dict[str, Any]:
        now = at or utc_now()
        with self.transaction() as connection:
            attempt = self._attempt_for_update(connection, install_operation_id)
            if attempt["state"] == "lkg_pending":
                return attempt
            if attempt["state"] != "current_committed":
                raise LifecycleError("LKG pending requires current_committed")
            pending = copy.deepcopy(attempt)
            pending.update(state="lkg_pending", updated_at=now)
            return self._put_transition(connection, attempt, pending)

    def promote_lkg(
        self,
        install_operation_id: str,
        *,
        evidence: Mapping[str, Any],
        qualification_verifier: QualificationVerifier,
        pin_releaser: PinReleaser,
        at: str | None = None,
    ) -> dict[str, Any]:
        now = at or utc_now()
        with self.transaction() as connection:
            attempt = self._attempt_for_update(connection, install_operation_id)
            target_id = attempt["target_generation_id"]
            if target_id is None:
                raise LifecycleError("LKG promotion has no target Generation")
            generation = self.get_generation(target_id)
            verified = require_verified_qualification(
                qualification_verifier(connection, evidence, generation),
                generation,
            )
            qualification_id = verified.qualification_id
            if attempt["state"] == "lkg_promoted":
                if attempt["qualification_id"] == qualification_id:
                    return attempt
                raise LifecycleError(
                    "qualification replay differs", code=ErrorCode.DUPLICATE_REQUEST
                )
            if attempt["state"] != "lkg_pending":
                raise LifecycleError("LKG promotion requires lkg_pending")
            if attempt["qualification_id"] is not None:
                raise LifecycleError(
                    "LKG qualification identity was already assigned",
                    code=ErrorCode.DUPLICATE_REQUEST,
                )
            pointer = connection.execute(
                "SELECT current_generation_id FROM p2_plugin_generation_pointer WHERE singleton=1"
            ).fetchone()
            if target_id is None or pointer is None or pointer[0] != target_id:
                raise LifecycleError(
                    "LKG candidate is no longer current",
                    code=ErrorCode.INCOMPATIBLE_GENERATION,
                )
            connection.execute(
                """
                UPDATE p2_plugin_generation_pointer
                   SET lkg_generation_id=?,revision=revision+1
                 WHERE singleton=1 AND current_generation_id=?
                """,
                (target_id, target_id),
            )
            promoted = copy.deepcopy(attempt)
            promoted.update(
                state="lkg_promoted", qualification_id=qualification_id, updated_at=now
            )
            promoted = self._put_transition(connection, attempt, promoted)
            pin_releaser(connection, install_operation_id)
            return promoted

    def arm_rollback(
        self,
        install_operation_id: str,
        *,
        token: str | None = None,
        at: str | None = None,
    ) -> str:
        now = at or utc_now()
        with self.transaction() as connection:
            attempt = self._attempt_for_update(connection, install_operation_id)
            if attempt["rollback_attempt"] == 1:
                existing = attempt["rollback_token"]
                if existing is None:
                    raise LifecycleError("durable rollback attempt lost its token")
                if token is not None and token != existing:
                    raise LifecycleError(
                        "a second rollback token is forbidden",
                        code=ErrorCode.DUPLICATE_REQUEST,
                    )
                return existing
            if attempt["state"] not in {"current_committed", "lkg_pending"}:
                raise LifecycleError("rollback can be armed only after current commit")
            rollback_token = token or f"rollback-{uuid.uuid4().hex}"
            armed = copy.deepcopy(attempt)
            armed.update(
                state="rollback_armed",
                rollback_attempt=1,
                rollback_token=rollback_token,
                updated_at=now,
            )
            self._put_transition(connection, attempt, armed)
            return rollback_token

    def complete_rollback(
        self,
        install_operation_id: str,
        *,
        token: str,
        event_writer: EventWriter,
        execution_guard: GenerationGuard,
        pin_releaser: PinReleaser,
        at: str | None = None,
    ) -> dict[str, Any]:
        now = at or utc_now()
        with self.transaction() as connection:
            attempt = self._attempt_for_update(connection, install_operation_id)
            if attempt["rollback_token"] != token or attempt["rollback_attempt"] != 1:
                raise LifecycleError(
                    "rollback token mismatch", code=ErrorCode.DUPLICATE_REQUEST
                )
            if attempt["state"] == "rolled_back":
                return attempt
            if attempt["state"] == "safe_mode":
                return attempt
            if (
                attempt["state"] == "superseded"
                and attempt["failure_code"] == "rollback_target_superseded"
            ):
                return attempt
            if attempt["state"] != "rollback_armed":
                raise LifecycleError("rollback is not armed")
            pointer = connection.execute(
                "SELECT current_generation_id,lkg_generation_id FROM p2_plugin_generation_pointer WHERE singleton=1"
            ).fetchone()
            target_id = attempt["target_generation_id"]
            if pointer is None or pointer[0] != target_id:
                superseded = copy.deepcopy(attempt)
                superseded.update(
                    state="superseded",
                    failure_code="rollback_target_superseded",
                    updated_at=now,
                )
                superseded = self._put_transition(connection, attempt, superseded)
                pin_releaser(connection, install_operation_id)
                return superseded
            restore_id = attempt["base_generation_id"] or pointer[1]
            restore_row = (
                None
                if restore_id is None
                else connection.execute(
                    "SELECT payload_json FROM p2_plugin_generation WHERE generation_id=?",
                    (restore_id,),
                ).fetchone()
            )
            restore_exists = restore_row is not None
            result = copy.deepcopy(attempt)
            if not restore_exists:
                connection.execute(
                    "UPDATE p2_plugin_generation_pointer SET safe_mode=1,safe_mode_reason=?,revision=revision+1 WHERE singleton=1",
                    ("rollback_target_unavailable",),
                )
                result.update(
                    state="safe_mode",
                    failure_code="rollback_target_unavailable",
                    updated_at=now,
                )
            else:
                restore_generation = validate_generation(_load(restore_row[0]))
                try:
                    execution_guard(
                        connection, restore_generation, install_operation_id
                    )
                except LifecycleError:
                    connection.execute(
                        "UPDATE p2_plugin_generation_pointer SET safe_mode=1,safe_mode_reason=?,revision=revision+1 WHERE singleton=1",
                        ("rollback_release_unavailable",),
                    )
                    result.update(
                        state="safe_mode",
                        failure_code="rollback_release_unavailable",
                        updated_at=now,
                    )
                    result = self._put_transition(connection, attempt, result)
                    pin_releaser(connection, install_operation_id)
                    return result
                connection.execute(
                    """
                    UPDATE p2_plugin_generation_pointer
                       SET current_generation_id=?,revision=revision+1
                     WHERE singleton=1 AND current_generation_id=?
                    """,
                    (restore_id, target_id),
                )
                result.update(state="rolled_back", updated_at=now)
                result = self._put_transition(connection, attempt, result)
                event_writer(connection, target_id, restore_id, install_operation_id)
                pin_releaser(connection, install_operation_id)
                return result
            result = self._put_transition(connection, attempt, result)
            pin_releaser(connection, install_operation_id)
            return result

    def enter_safe_mode(
        self,
        install_operation_id: str,
        *,
        reason: str,
        pin_releaser: PinReleaser,
        at: str | None = None,
    ) -> dict[str, Any]:
        if not reason:
            raise LifecycleError("safe mode requires a failure reason")
        now = at or utc_now()
        with self.transaction() as connection:
            attempt = self._attempt_for_update(connection, install_operation_id)
            if attempt["state"] == "safe_mode":
                return attempt
            if attempt["state"] not in {
                "current_committed",
                "lkg_pending",
                "rollback_armed",
            }:
                raise LifecycleError(
                    "safe mode may be entered only after activation risk"
                )
            connection.execute(
                "UPDATE p2_plugin_generation_pointer SET safe_mode=1,safe_mode_reason=?,revision=revision+1 WHERE singleton=1",
                (reason,),
            )
            result = copy.deepcopy(attempt)
            result.update(state="safe_mode", failure_code=reason, updated_at=now)
            result = self._put_transition(connection, attempt, result)
            pin_releaser(connection, install_operation_id)
            return result

    def exit_safe_mode(self, *, generation_id: str) -> GenerationState:
        """Explicitly exit only through a Generation already promoted to LKG."""
        with self.transaction() as connection:
            pointer = connection.execute(
                "SELECT current_generation_id,lkg_generation_id,safe_mode FROM p2_plugin_generation_pointer WHERE singleton=1"
            ).fetchone()
            qualified = connection.execute(
                "SELECT 1 FROM p2_plugin_install_attempt WHERE json_extract(transition_json,'$.state')='lkg_promoted' AND json_extract(transition_json,'$.target_generation_id')=?",
                (generation_id,),
            ).fetchone()
            unresolved = connection.execute(
                """
                SELECT 1 FROM p2_plugin_install_attempt
                 WHERE json_extract(transition_json,'$.state') NOT IN
                       ('lkg_promoted','failed','superseded','rolled_back','safe_mode')
                 LIMIT 1
                """
            ).fetchone()
            if (
                pointer is None
                or pointer[0] != generation_id
                or pointer[1] != generation_id
                or qualified is None
                or unresolved is not None
            ):
                raise LifecycleError(
                    "safe mode exit requires an explicitly reconciled current LKG"
                )
            if pointer[2] == 1:
                connection.execute(
                    "UPDATE p2_plugin_generation_pointer SET safe_mode=0,safe_mode_reason=NULL,revision=revision+1 WHERE singleton=1"
                )
        return self.generation_state()

    def reconcile_attempt(
        self,
        install_operation_id: str,
        *,
        package_present: bool,
    ) -> ReconciliationDecision:
        """Return the single safe next action from durable facts.

        Reconciliation never guesses package identity or activates an orphan.
        The caller performs the returned action with the normal CAS methods.
        """
        attempt = self.get_attempt(install_operation_id)
        state = attempt["state"]
        pointer = self.connection.execute(
            "SELECT current_generation_id,lkg_generation_id FROM p2_plugin_generation_pointer WHERE singleton=1"
        ).fetchone()
        current_id = None if pointer is None else pointer[0]
        if state in _TERMINAL:
            return ReconciliationDecision(
                install_operation_id, state, "none", "terminal"
            )
        # The durable nonterminal attempt is itself the reconciliation fence.
        # Pre-activation crashes do not make the last known-good Generation
        # unsafe, and a global boolean cannot represent multiple attempts.
        if state == "rollback_armed":
            return ReconciliationDecision(
                install_operation_id,
                state,
                "finish_rollback",
                "reuse durable rollback token",
            )
        if state in {"current_committed", "lkg_pending"}:
            if current_id == attempt["target_generation_id"]:
                return ReconciliationDecision(
                    install_operation_id,
                    state,
                    "qualify_or_rollback",
                    "target Generation is current",
                )
            successor = self.connection.execute(
                """
                SELECT 1 FROM p2_plugin_generation
                 WHERE generation_id=?
                   AND json_extract(payload_json,'$.parent_generation_id')=?
                """,
                (current_id, attempt["target_generation_id"]),
            ).fetchone()
            if successor is not None:
                return ReconciliationDecision(
                    install_operation_id,
                    state,
                    "supersede",
                    "a durable successor Generation is current",
                )
            return ReconciliationDecision(
                install_operation_id,
                state,
                "enter_safe_mode",
                "committed transition disagrees with current pointer",
            )
        if (
            state in {"qualified", "pending_apply"}
            and current_id != attempt["base_generation_id"]
        ):
            return ReconciliationDecision(
                install_operation_id, state, "supersede", "base Generation changed"
            )
        if (
            state
            in {
                "package_published",
                "env_prepared",
                "shadow_prepared",
                "migrated",
                "settings_validated",
                "qualified",
                "pending_apply",
            }
            and not package_present
        ):
            return ReconciliationDecision(
                install_operation_id, state, "fail", "referenced package is missing"
            )
        return ReconciliationDecision(
            install_operation_id,
            state,
            "resume",
            "durable prerequisites remain consistent",
        )

    def reconciliation_fences(self) -> tuple[tuple[str, str], ...]:
        """Return independent durable fences without collapsing them to safe mode."""
        return tuple(
            (attempt["install_operation_id"], attempt["state"])
            for attempt in self.list_attempts(include_terminal=False)
        )


def initial_transition(
    install_operation_id: str,
    *,
    base_generation_id: str | None,
    base_lkg_generation_id: str | None,
    target_generation_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    now = created_at or utc_now()
    return validate_transition(
        {
            "schema": "plugin-lifecycle-transition/v1",
            "install_operation_id": install_operation_id,
            "base_generation_id": base_generation_id,
            "base_lkg_generation_id": base_lkg_generation_id,
            "target_generation_id": target_generation_id,
            "state": "selected",
            "package_store_status": "absent",
            "shadow_data_generation_id": None,
            "target_settings_revision_ids": [],
            "qualification_id": None,
            "rollback_attempt": 0,
            "rollback_token": None,
            "failure_code": None,
            "created_at": now,
            "updated_at": now,
        }
    )


__all__ = [
    "LIFECYCLE_MIGRATIONS",
    "EventWriter",
    "GenerationGuard",
    "LifecycleError",
    "LifecycleMigration",
    "LifecycleRepository",
    "PinReleaser",
    "QualificationVerifier",
    "ReconciliationDecision",
    "TransactionFactory",
    "VerifiedQualification",
    "initial_transition",
    "require_verified_qualification",
    "utc_now",
    "validate_transition",
]
