"""SQLite-backed production authority for plugin worker lifecycle claims."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from plotpilot_plugin_sdk.canonical import canonical_bytes
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode
from plotpilot_plugin_sdk.verifier import assert_valid

from ..plugins.generation import validate_generation
from ..plugins.lifecycle.repository import (
    LifecycleError,
    LifecycleRepository,
    utc_now,
    validate_transition,
)
from ..plugins.lifecycle.retirement import RetirementManager
from ..repositories.authority import CoreAuthorityRepository
from ..repositories.execution import ExecutionAuthority
from .models import AttemptFence, InstallFence, WorkerFence
from .routes import PackageGenerationRouteSource

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_ACTIVE_ATTEMPT_STATES = frozenset({"running", "cancelling"})
_TERMINAL_ATTEMPT_STATES = frozenset({"succeeded", "partial", "failed", "cancelled"})
_TERMINAL_JOB_STATES = frozenset({"succeeded", "partial", "failed", "cancelled"})
_ACTIVE_SHADOW_STATES = frozenset({"prepared", "applying", "applied", "verified"})
_ACTIVE_INSTALL_STATES = frozenset({"shadow_prepared", "migrated"})


def _require_id(value: str, label: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH, f"{label} is not a v1 ID"
        )


def _load_object(raw: object, label: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise LifecycleError(f"stored {label} is not JSON text")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise LifecycleError(f"stored {label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise LifecycleError(f"stored {label} is not an object")
    return value


def _dump(value: Mapping[str, Any]) -> str:
    return canonical_bytes(dict(value)).decode("utf-8")


def _future(timestamp: object, now: str) -> bool:
    if not isinstance(timestamp, str):
        return False
    try:
        expires = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires.tzinfo is None or current.tzinfo is None:
        return False
    return expires.astimezone(UTC) > current.astimezone(UTC)


class SQLiteSupervisorAuthority:
    """One durable, CAS-fenced owner per logical worker.

    The authority stores every epoch and release in the same SQLite database
    as Core/Lifecycle/Execution.  Process-local supervisor observations never
    decide ownership.
    """

    def __init__(
        self,
        repository: CoreAuthorityRepository,
        lifecycle: LifecycleRepository,
        retirement: RetirementManager,
        execution: ExecutionAuthority,
        routes: PackageGenerationRouteSource,
        *,
        now: Callable[[], str] = utc_now,
    ) -> None:
        if lifecycle.core_authority_binding is not repository:
            raise ValueError("lifecycle is not bound to the exact Core repository")
        if retirement.repository is not lifecycle:
            raise ValueError("retirement is not bound to the exact lifecycle repository")
        if execution.repository is not repository:
            raise ValueError("execution is not bound to the exact Core repository")
        if routes.lifecycle is not lifecycle or routes.retirement is not retirement:
            raise ValueError("routes are not bound to the exact lifecycle authority")
        with repository.read_connection() as connection:
            if lifecycle.connection is not connection:
                raise ValueError("lifecycle and Core must share the exact SQLite connection")
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plugin_supervisor_claim'"
            ).fetchone()
            if table is None:
                raise ValueError("production supervisor migration is not applied")
        self.repository = repository
        self.lifecycle = lifecycle
        self.retirement = retirement
        self.execution = execution
        self.routes = routes
        self.route_authority = routes
        self._now = now

    @staticmethod
    def _current_member(
        connection: sqlite3.Connection, worker_id: str
    ) -> tuple[str, Mapping[str, Any]] | None:
        pointer = connection.execute(
            """
            SELECT current_generation_id,safe_mode
              FROM p2_plugin_generation_pointer WHERE singleton=1
            """
        ).fetchone()
        if pointer is None or pointer[0] is None or bool(pointer[1]):
            return None
        generation_id = str(pointer[0])
        row = connection.execute(
            "SELECT payload_json FROM p2_plugin_generation WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        if row is None:
            return None
        generation = validate_generation(_load_object(row[0], "plugin Generation"))
        if generation.get("generation_id") != generation_id:
            return None
        members = generation.get("members")
        if not isinstance(members, list):
            return None
        matches = [
            member
            for member in members
            if isinstance(member, Mapping) and member.get("plugin_id") == worker_id
        ]
        if len(matches) != 1:
            return None
        return generation_id, matches[0]

    @staticmethod
    def _retirement(
        connection: sqlite3.Connection, release_id: str
    ) -> Mapping[str, Any] | None:
        row = connection.execute(
            "SELECT retirement_json FROM p2_plugin_release_retirement WHERE release_id=?",
            (release_id,),
        ).fetchone()
        if row is None:
            return None
        value = _load_object(row[0], "release retirement")
        assert_valid("release-retirement/v1", value)
        if (
            value.get("release_id") != release_id
            or value.get("state") != "installed"
            or value.get("package_present") is not True
        ):
            return None
        return value

    @staticmethod
    def _pin(
        connection: sqlite3.Connection, pin_id: str
    ) -> Mapping[str, Any] | None:
        row = connection.execute(
            "SELECT pin_json FROM p2_plugin_release_pin WHERE pin_id=?", (pin_id,)
        ).fetchone()
        if row is None:
            return None
        value = _load_object(row[0], "release pin")
        assert_valid("release-pin/v1", value)
        if value.get("pin_id") != pin_id:
            return None
        return value

    @classmethod
    def _active_claim(
        cls,
        connection: sqlite3.Connection,
        worker_id: str,
    ) -> tuple[sqlite3.Row, WorkerFence] | None:
        row = connection.execute(
            "SELECT * FROM plugin_supervisor_claim WHERE worker_id=? AND state='active'",
            (worker_id,),
        ).fetchone()
        if row is None:
            return None
        current = cls._current_member(connection, worker_id)
        if current is None:
            return None
        generation_id, member = current
        stable = {
            "plugin_id": member.get("plugin_id"),
            "generation_id": generation_id,
            "release_id": member.get("release_id"),
            "package_hash": member.get("package_hash"),
            "data_generation_id": member.get("data_generation_id"),
        }
        if any(row[key] != value for key, value in stable.items()):
            return None
        retirement = cls._retirement(connection, str(row["release_id"]))
        if retirement is None or retirement.get("retire_epoch") != row["retire_epoch"]:
            return None
        pin = cls._pin(connection, str(row["pin_id"]))
        if pin is None or (
            pin.get("release_id") != row["release_id"]
            or pin.get("retire_epoch") != row["retire_epoch"]
            or pin.get("pin_kind") != "worker"
            or pin.get("owner_id") != row["owner_id"]
            or pin.get("released_at") is not None
        ):
            return None
        fence = WorkerFence(
            worker_id=str(row["worker_id"]),
            pin_id=str(row["pin_id"]),
            plugin_id=str(row["plugin_id"]),
            generation_id=str(row["generation_id"]),
            release_id=str(row["release_id"]),
            data_generation_id=(
                None
                if row["data_generation_id"] is None
                else str(row["data_generation_id"])
            ),
            pin_epoch=int(row["pin_epoch"]),
            retire_epoch=int(row["retire_epoch"]),
        )
        return row, fence

    @staticmethod
    def _pin_id(
        worker_id: str, pin_epoch: int, generation_id: str, release_id: str
    ) -> str:
        digest = hashlib.sha256(
            canonical_bytes(
                {
                    "worker_id": worker_id,
                    "pin_epoch": pin_epoch,
                    "generation_id": generation_id,
                    "release_id": release_id,
                }
            )
        ).hexdigest()
        return f"pin-worker-{digest[:48]}"

    def claim(self, worker_id: str, owner_id: str) -> WorkerFence | None:
        _require_id(worker_id, "worker_id")
        _require_id(owner_id, "owner_id")
        route = self.routes.availability(worker_id).route
        if route is None:
            return None
        try:
            with self.lifecycle.transaction() as connection:
                existing_row = connection.execute(
                    """
                    SELECT * FROM plugin_supervisor_claim
                     WHERE worker_id=? AND state='active'
                    """,
                    (worker_id,),
                ).fetchone()
                if existing_row is not None:
                    current = self._active_claim(connection, worker_id)
                    if current is None:
                        return None
                    row, fence = current
                    if (
                        row["owner_id"] != owner_id
                        or row["package_hash"] != route.package_hash
                        or fence.generation_id != route.generation_id
                        or fence.release_id != route.release_id
                        or fence.retire_epoch != route.retire_epoch
                    ):
                        return None
                    return fence

                current = self._current_member(connection, worker_id)
                if current is None:
                    return None
                generation_id, member = current
                retirement = self._retirement(connection, route.release_id)
                if retirement is None:
                    return None
                if (
                    generation_id != route.generation_id
                    or member.get("plugin_id") != route.plugin_id
                    or member.get("release_id") != route.release_id
                    or member.get("package_hash") != route.package_hash
                    or member.get("data_generation_id") != route.data_generation_id
                    or retirement.get("retire_epoch") != route.retire_epoch
                ):
                    return None
                previous = connection.execute(
                    "SELECT COALESCE(MAX(pin_epoch),0) FROM plugin_supervisor_claim WHERE worker_id=?",
                    (worker_id,),
                ).fetchone()
                pin_epoch = int(previous[0]) + 1
                pin_id = self._pin_id(
                    worker_id, pin_epoch, route.generation_id, route.release_id
                )
                now = self._now()
                pin = {
                    "schema": "release-pin/v1",
                    "pin_id": pin_id,
                    "release_id": route.release_id,
                    "retire_epoch": route.retire_epoch,
                    "pin_kind": "worker",
                    "owner_id": owner_id,
                    "created_at": now,
                    "released_at": None,
                }
                assert_valid("release-pin/v1", pin)
                connection.execute(
                    """
                    INSERT INTO p2_plugin_release_pin(
                        pin_id,release_id,retire_epoch,pin_json
                    ) VALUES(?,?,?,?)
                    """,
                    (pin_id, route.release_id, route.retire_epoch, _dump(pin)),
                )
                connection.execute(
                    """
                    INSERT INTO plugin_supervisor_claim(
                        worker_id,pin_epoch,pin_id,owner_id,plugin_id,generation_id,
                        release_id,package_hash,data_generation_id,retire_epoch,state,
                        created_at,updated_at,released_at,revision
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,'active',?,?,NULL,1)
                    """,
                    (
                        worker_id,
                        pin_epoch,
                        pin_id,
                        owner_id,
                        route.plugin_id,
                        route.generation_id,
                        route.release_id,
                        route.package_hash,
                        route.data_generation_id,
                        route.retire_epoch,
                        now,
                        now,
                    ),
                )
                return WorkerFence(
                    worker_id=worker_id,
                    pin_id=pin_id,
                    plugin_id=route.plugin_id,
                    generation_id=route.generation_id,
                    release_id=route.release_id,
                    data_generation_id=route.data_generation_id,
                    pin_epoch=pin_epoch,
                    retire_epoch=route.retire_epoch,
                )
        except sqlite3.IntegrityError:
            return None

    def snapshot(self, worker_id: str) -> WorkerFence | None:
        _require_id(worker_id, "worker_id")
        try:
            with self.repository.read_connection() as connection:
                active = connection.execute(
                    """
                    SELECT 1 FROM plugin_supervisor_claim
                     WHERE worker_id=? AND state='active'
                    """,
                    (worker_id,),
                ).fetchone()
                if active is not None:
                    current = self._active_claim(connection, worker_id)
                    return None if current is None else current[1]

                # PluginProcessSupervisor checks the expected RunSnapshot release
                # before it creates its lifecycle owner.  Expose the deterministic
                # next claim identity without persisting ownership; claim() repeats
                # every check under BEGIN IMMEDIATE before inserting it.
                route = self.routes.availability(worker_id).route
                if route is None:
                    return None
                current = self._current_member(connection, worker_id)
                retirement = self._retirement(connection, route.release_id)
                if current is None or retirement is None:
                    return None
                generation_id, member = current
                if (
                    generation_id != route.generation_id
                    or member.get("plugin_id") != route.plugin_id
                    or member.get("release_id") != route.release_id
                    or member.get("package_hash") != route.package_hash
                    or member.get("data_generation_id") != route.data_generation_id
                    or retirement.get("retire_epoch") != route.retire_epoch
                ):
                    return None
                previous = connection.execute(
                    "SELECT COALESCE(MAX(pin_epoch),0) FROM plugin_supervisor_claim WHERE worker_id=?",
                    (worker_id,),
                ).fetchone()
                pin_epoch = int(previous[0]) + 1
                return WorkerFence(
                    worker_id=worker_id,
                    pin_id=self._pin_id(
                        worker_id,
                        pin_epoch,
                        route.generation_id,
                        route.release_id,
                    ),
                    plugin_id=route.plugin_id,
                    generation_id=route.generation_id,
                    release_id=route.release_id,
                    data_generation_id=route.data_generation_id,
                    pin_epoch=pin_epoch,
                    retire_epoch=route.retire_epoch,
                )
        except (ContractError, KeyError, sqlite3.Error, TypeError, ValueError):
            return None

    def holds(self, fence: WorkerFence, owner_id: str) -> bool:
        if not isinstance(fence, WorkerFence):
            return False
        try:
            _require_id(owner_id, "owner_id")
            with self.repository.read_connection() as connection:
                current = self._active_claim(connection, fence.worker_id)
                return (
                    current is not None
                    and current[1] == fence
                    and current[0]["owner_id"] == owner_id
                )
        except (ContractError, KeyError, sqlite3.Error, TypeError, ValueError):
            return False

    def holds_attempt(
        self, fence: WorkerFence, attempt: AttemptFence, owner_id: str
    ) -> bool:
        if not isinstance(fence, WorkerFence) or not isinstance(attempt, AttemptFence):
            return False
        if attempt.owner_id != owner_id:
            return False
        try:
            with self.repository.read_connection() as connection:
                current = self._active_claim(connection, fence.worker_id)
                if (
                    current is None
                    or current[1] != fence
                    or current[0]["owner_id"] != owner_id
                ):
                    return False
                claim = current[0]
                row = connection.execute(
                    """
                    SELECT a.*,s.state AS step_state,s.active_attempt_id,
                           j.job_state AS job_state
                      FROM execution_attempt a
                      JOIN execution_step s
                        ON s.job_id=a.job_id AND s.step_id=a.step_id
                      JOIN execution_job j ON j.job_id=a.job_id
                     WHERE a.attempt_id=?
                    """,
                    (attempt.attempt_id,),
                ).fetchone()
                return bool(
                    row is not None
                    and row["job_id"] == attempt.job_id
                    and row["step_id"] == attempt.step_id
                    and row["lease_epoch"] == attempt.lease_epoch
                    and row["worker_run_id"] == attempt.owner_id
                    and row["owner_instance_id"] == attempt.owner_id
                    and (
                        (row["state"] == "running" and row["job_state"] == "running")
                        or (
                            row["state"] == "cancelling"
                            and row["job_state"] == "cancelling"
                        )
                    )
                    and row["step_state"] == "running"
                    and row["active_attempt_id"] == attempt.attempt_id
                    and row["plugin_id"] == fence.plugin_id
                    and row["release_id"] == fence.release_id
                    and row["package_hash"] == claim["package_hash"]
                    and row["generation_id"] == fence.generation_id
                    and _future(row["lease_expires_at"], self._now())
                )
        except (ContractError, KeyError, sqlite3.Error, TypeError, ValueError):
            return False

    def closed_attempt(
        self, fence: WorkerFence, attempt: AttemptFence, owner_id: str
    ) -> bool:
        """Prove an exact durable terminal closure before transport unbind."""

        if not isinstance(fence, WorkerFence) or not isinstance(attempt, AttemptFence):
            return False
        if attempt.owner_id != owner_id:
            return False
        try:
            with self.repository.read_connection() as connection:
                current = self._active_claim(connection, fence.worker_id)
                if (
                    current is None
                    or current[1] != fence
                    or current[0]["owner_id"] != owner_id
                ):
                    return False
                claim = current[0]
                row = connection.execute(
                    """
                    SELECT a.*,s.state AS step_state,s.active_attempt_id,
                           j.job_state AS job_state
                      FROM execution_attempt a
                      JOIN execution_step s
                        ON s.job_id=a.job_id AND s.step_id=a.step_id
                      JOIN execution_job j ON j.job_id=a.job_id
                     WHERE a.attempt_id=?
                    """,
                    (attempt.attempt_id,),
                ).fetchone()
                return bool(
                    row is not None
                    and row["job_id"] == attempt.job_id
                    and row["step_id"] == attempt.step_id
                    and row["lease_epoch"] == attempt.lease_epoch
                    and row["worker_run_id"] == attempt.owner_id
                    and row["owner_instance_id"] == attempt.owner_id
                    and row["state"] in _TERMINAL_ATTEMPT_STATES
                    and row["step_state"] == row["state"]
                    and row["active_attempt_id"] == attempt.attempt_id
                    and row["job_state"] in _TERMINAL_JOB_STATES
                    and row["plugin_id"] == fence.plugin_id
                    and row["release_id"] == fence.release_id
                    and row["package_hash"] == claim["package_hash"]
                    and row["generation_id"] == fence.generation_id
                )
        except (ContractError, KeyError, sqlite3.Error, TypeError, ValueError):
            return False

    def holds_install(
        self, fence: WorkerFence, install: InstallFence, owner_id: str
    ) -> bool:
        if not isinstance(fence, WorkerFence) or not isinstance(install, InstallFence):
            return False
        try:
            with self.repository.read_connection() as connection:
                current = self._active_claim(connection, fence.worker_id)
                if (
                    current is None
                    or current[1] != fence
                    or current[0]["owner_id"] != owner_id
                ):
                    return False
                claim = current[0]
                row = connection.execute(
                    """
                    SELECT s.*,a.request_json,a.transition_json
                      FROM p2_plugin_shadow_generation s
                      JOIN p2_plugin_install_attempt a
                        ON a.install_operation_id=s.install_operation_id
                     WHERE s.install_operation_id=?
                    """,
                    (install.install_operation_id,),
                ).fetchone()
                if row is None:
                    return False
                request = _load_object(row["request_json"], "install request")
                transition = validate_transition(
                    _load_object(row["transition_json"], "install transition")
                )
                pin_row = connection.execute(
                    """
                    SELECT pin_id,pin_json FROM p2_plugin_release_pin
                     WHERE release_id=? AND retire_epoch=?
                       AND json_extract(pin_json,'$.pin_kind')='install'
                       AND json_extract(pin_json,'$.owner_id')=?
                       AND json_extract(pin_json,'$.released_at') IS NULL
                    """,
                    (
                        fence.release_id,
                        fence.retire_epoch,
                        install.install_operation_id,
                    ),
                ).fetchone()
                install_pin = (
                    None
                    if pin_row is None
                    else _load_object(pin_row["pin_json"], "install release pin")
                )
                if install_pin is not None:
                    assert_valid("release-pin/v1", install_pin)
                return bool(
                    row["release_id"] == fence.release_id
                    and row["db_lease_epoch"] == install.install_lease_epoch
                    and row["owner_instance_id"] == install.owner_instance_id
                    and row["lease_state"] == "active"
                    and row["state"] in _ACTIVE_SHADOW_STATES
                    and _future(row["expires_at"], self._now())
                    and request.get("plugin_id") == fence.plugin_id
                    and request.get("release_id") == fence.release_id
                    and request.get("package_hash") == claim["package_hash"]
                    and transition.get("install_operation_id")
                    == install.install_operation_id
                    and transition.get("shadow_data_generation_id")
                    == row["shadow_data_generation_id"]
                    and transition.get("state") in _ACTIVE_INSTALL_STATES
                    and install_pin is not None
                    and install_pin.get("pin_id") == pin_row["pin_id"]
                    and install_pin.get("release_id") == fence.release_id
                    and install_pin.get("retire_epoch") == fence.retire_epoch
                    and install_pin.get("pin_kind") == "install"
                    and install_pin.get("owner_id") == install.install_operation_id
                    and install_pin.get("released_at") is None
                )
        except (ContractError, KeyError, sqlite3.Error, TypeError, ValueError):
            return False

    def release(self, fence: WorkerFence, owner_id: str) -> bool:
        if not isinstance(fence, WorkerFence) or not fence.allowed:
            return False
        _require_id(owner_id, "owner_id")
        with self.lifecycle.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM plugin_supervisor_claim
                 WHERE worker_id=? AND pin_epoch=?
                """,
                (fence.worker_id, fence.pin_epoch),
            ).fetchone()
            if row is None or (
                row["pin_id"] != fence.pin_id
                or row["owner_id"] != owner_id
                or row["plugin_id"] != fence.plugin_id
                or row["generation_id"] != fence.generation_id
                or row["release_id"] != fence.release_id
                or row["data_generation_id"] != fence.data_generation_id
                or row["retire_epoch"] != fence.retire_epoch
            ):
                return False
            pin = self._pin(connection, fence.pin_id)
            if pin is None or (
                pin.get("release_id") != fence.release_id
                or pin.get("retire_epoch") != fence.retire_epoch
                or pin.get("pin_kind") != "worker"
                or pin.get("owner_id") != owner_id
            ):
                return False
            if row["state"] == "released":
                return (
                    row["released_at"] is not None
                    and row["released_at"] == pin.get("released_at")
                )
            if pin.get("released_at") is not None:
                return False
            now = self._now()
            released_pin = copy.deepcopy(pin)
            released_pin["released_at"] = now
            assert_valid("release-pin/v1", released_pin)
            changed_pin = connection.execute(
                """
                UPDATE p2_plugin_release_pin
                   SET pin_json=?,revision=revision+1
                 WHERE pin_id=? AND pin_json=?
                """,
                (_dump(released_pin), fence.pin_id, _dump(pin)),
            ).rowcount
            changed_claim = connection.execute(
                """
                UPDATE plugin_supervisor_claim
                   SET state='released',released_at=?,updated_at=?,revision=revision+1
                 WHERE worker_id=? AND pin_epoch=? AND state='active' AND revision=?
                """,
                (
                    now,
                    now,
                    fence.worker_id,
                    fence.pin_epoch,
                    row["revision"],
                ),
            ).rowcount
            if changed_pin != 1 or changed_claim != 1:
                raise LifecycleError(
                    "worker claim release compare-and-swap failed",
                    code=ErrorCode.STALE_LEASE,
                )
            return True

    def interrupt_attempt(
        self,
        fence: WorkerFence,
        attempt: AttemptFence,
        owner_id: str,
        reason: str,
    ) -> bool:
        if (
            not isinstance(fence, WorkerFence)
            or not isinstance(attempt, AttemptFence)
            or not isinstance(reason, str)
            or not reason.strip()
            or attempt.owner_id != owner_id
        ):
            return False
        with self.lifecycle.transaction() as connection:
            current = self._active_claim(connection, fence.worker_id)
            if (
                current is None
                or current[1] != fence
                or current[0]["owner_id"] != owner_id
            ):
                return False
            claim = current[0]
            row = connection.execute(
                """
                SELECT a.*,s.active_attempt_id,s.state AS step_state,
                       j.job_state AS job_state
                  FROM execution_attempt a
                  JOIN execution_step s
                    ON s.job_id=a.job_id AND s.step_id=a.step_id
                  JOIN execution_job j ON j.job_id=a.job_id
                 WHERE a.attempt_id=?
                """,
                (attempt.attempt_id,),
            ).fetchone()
            exact = bool(
                row is not None
                and row["job_id"] == attempt.job_id
                and row["step_id"] == attempt.step_id
                and row["lease_epoch"] == attempt.lease_epoch
                and row["worker_run_id"] == attempt.owner_id
                and row["owner_instance_id"] == attempt.owner_id
                and row["plugin_id"] == fence.plugin_id
                and row["release_id"] == fence.release_id
                and row["package_hash"] == claim["package_hash"]
                and row["generation_id"] == fence.generation_id
                and row["active_attempt_id"] == attempt.attempt_id
                and (
                    (
                        row["state"] in _ACTIVE_ATTEMPT_STATES
                        and row["step_state"] == "running"
                        and row["job_state"] in {"running", "cancelling"}
                    )
                    or (
                        row["state"] == "fenced"
                        and row["step_state"] in {"running", "needs_attention"}
                        and row["job_state"]
                        in {"running", "cancelling", "needs_attention"}
                    )
                )
            )
            if not exact:
                return False
            now = self._now()
            if row["state"] in _ACTIVE_ATTEMPT_STATES:
                changed = connection.execute(
                    """
                    UPDATE execution_attempt
                       SET state='fenced',revision=revision+1,updated_at=?
                     WHERE attempt_id=? AND revision=?
                       AND state IN ('running','cancelling')
                    """,
                    (now, attempt.attempt_id, row["revision"]),
                ).rowcount
                if changed != 1:
                    raise LifecycleError(
                        "Attempt interruption compare-and-swap failed",
                        code=ErrorCode.STALE_LEASE,
                    )
            if row["step_state"] == "running":
                connection.execute(
                    "UPDATE execution_step SET state='needs_attention',"
                    "revision=revision+1,updated_at=? WHERE job_id=? AND step_id=? "
                    "AND active_attempt_id=? AND state='running'",
                    (
                        now,
                        attempt.job_id,
                        attempt.step_id,
                        attempt.attempt_id,
                    ),
                )
            if row["job_state"] in {"running", "cancelling"}:
                connection.execute(
                    "UPDATE execution_job SET job_state='needs_attention',"
                    "job_revision=job_revision+1,updated_at=? WHERE job_id=? "
                    "AND job_state IN ('running','cancelling')",
                    (now, attempt.job_id),
                )
            return True


ProductionSupervisorAuthority = SQLiteSupervisorAuthority


__all__ = ["ProductionSupervisorAuthority", "SQLiteSupervisorAuthority"]
