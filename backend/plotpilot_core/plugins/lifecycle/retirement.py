"""Atomic release retirement and executable-reference barriers."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from plotpilot_plugin_sdk.canonical import canonical_bytes
from plotpilot_plugin_sdk.errors import ErrorCode
from plotpilot_plugin_sdk.verifier import assert_valid

from .repository import LifecycleError, LifecycleRepository, utc_now

RetirementEventWriter = Callable[[sqlite3.Connection, str, int, str], None]
PackageRemover = Callable[[], bool]


@dataclass(frozen=True)
class PublicationBarrierDecision:
    release_id: str
    allowed: bool
    blocker_ids: tuple[str, ...] = ()


PublicationBarrier = Callable[[sqlite3.Connection, str], PublicationBarrierDecision]


def _dump(value: Mapping[str, Any]) -> str:
    return canonical_bytes(dict(value)).decode("utf-8")


def _load(value: str) -> dict[str, Any]:
    result = json.loads(value)
    if not isinstance(result, dict):
        raise LifecycleError("stored retirement value is invalid")
    return result


class RetirementManager:
    """Linearize new pins against ``installed -> retiring`` CAS."""

    def __init__(self, repository: LifecycleRepository) -> None:
        self.repository = repository
        with repository.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS p2_plugin_release_retirement(
                    release_id TEXT PRIMARY KEY,
                    retirement_json TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS p2_plugin_retirement_attention(
                    release_id TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS p2_plugin_release_pin(
                    pin_id TEXT PRIMARY KEY,
                    release_id TEXT NOT NULL,
                    retire_epoch INTEGER NOT NULL,
                    pin_json TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(release_id) REFERENCES p2_plugin_release_retirement(release_id)
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS p2_plugin_release_active_pin_owner
                    ON p2_plugin_release_pin(release_id,retire_epoch,
                         json_extract(pin_json,'$.pin_kind'),json_extract(pin_json,'$.owner_id'))
                 WHERE json_extract(pin_json,'$.released_at') IS NULL
                """
            )

    def register_installed(
        self, release_id: str, *, at: str | None = None
    ) -> dict[str, Any]:
        value = {
            "schema": "release-retirement/v1",
            "release_id": release_id,
            "state": "installed",
            "retire_epoch": 1,
            "package_present": True,
            "started_at": None,
            "completed_at": None,
        }
        assert_valid("release-retirement/v1", value)
        with self.repository.transaction() as connection:
            row = connection.execute(
                "SELECT retirement_json FROM p2_plugin_release_retirement WHERE release_id=?",
                (release_id,),
            ).fetchone()
            if row is not None:
                existing = _load(row[0])
                assert_valid("release-retirement/v1", existing)
                if existing["state"] == "retired":
                    raise LifecycleError(
                        "a retired release identity cannot be reinstalled",
                        code=ErrorCode.RELEASE_RETIRING,
                    )
                return existing
            connection.execute(
                "INSERT INTO p2_plugin_release_retirement(release_id,retirement_json) VALUES(?,?)",
                (release_id, _dump(value)),
            )
        return value

    def get(self, release_id: str) -> dict[str, Any]:
        row = self.repository.connection.execute(
            "SELECT retirement_json FROM p2_plugin_release_retirement WHERE release_id=?",
            (release_id,),
        ).fetchone()
        if row is None:
            raise KeyError(release_id)
        value = _load(row[0])
        assert_valid("release-retirement/v1", value)
        return value

    def acquire_pin(
        self,
        release_id: str,
        *,
        pin_kind: str,
        owner_id: str,
        pin_id: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        now = at or utc_now()
        identity = pin_id or f"pin-{uuid.uuid4().hex}"
        with self.repository.transaction() as connection:
            retirement = self.get(release_id)
            existing_row = connection.execute(
                "SELECT pin_json FROM p2_plugin_release_pin WHERE pin_id=?", (identity,)
            ).fetchone()
            if existing_row is not None:
                existing = _load(existing_row[0])
                if (
                    existing["release_id"] == release_id
                    and existing["pin_kind"] == pin_kind
                    and existing["owner_id"] == owner_id
                ):
                    return existing
                raise LifecycleError(
                    "pin_id was reused with different input",
                    code=ErrorCode.DUPLICATE_REQUEST,
                )
            if retirement["state"] != "installed":
                raise LifecycleError(
                    "retiring release cannot receive a new executable pin",
                    code=ErrorCode.RELEASE_RETIRING,
                )
            active = connection.execute(
                """
                SELECT pin_json FROM p2_plugin_release_pin
                 WHERE release_id=? AND retire_epoch=?
                   AND json_extract(pin_json,'$.pin_kind')=?
                   AND json_extract(pin_json,'$.owner_id')=?
                   AND json_extract(pin_json,'$.released_at') IS NULL
                """,
                (release_id, retirement["retire_epoch"], pin_kind, owner_id),
            ).fetchone()
            if active is not None:
                return _load(active[0])
            pin = {
                "schema": "release-pin/v1",
                "pin_id": identity,
                "release_id": release_id,
                "retire_epoch": retirement["retire_epoch"],
                "pin_kind": pin_kind,
                "owner_id": owner_id,
                "created_at": now,
                "released_at": None,
            }
            assert_valid("release-pin/v1", pin)
            connection.execute(
                "INSERT INTO p2_plugin_release_pin(pin_id,release_id,retire_epoch,pin_json) VALUES(?,?,?,?)",
                (identity, release_id, retirement["retire_epoch"], _dump(pin)),
            )
            return pin

    def release_pin(self, pin_id: str, *, at: str | None = None) -> dict[str, Any]:
        now = at or utc_now()
        with self.repository.transaction() as connection:
            row = connection.execute(
                "SELECT pin_json FROM p2_plugin_release_pin WHERE pin_id=?", (pin_id,)
            ).fetchone()
            if row is None:
                raise KeyError(pin_id)
            pin = _load(row[0])
            assert_valid("release-pin/v1", pin)
            if pin["released_at"] is not None:
                return pin
            result = copy.deepcopy(pin)
            result["released_at"] = now
            assert_valid("release-pin/v1", result)
            changed = connection.execute(
                "UPDATE p2_plugin_release_pin SET pin_json=?,revision=revision+1 WHERE pin_id=? AND pin_json=?",
                (_dump(result), pin_id, _dump(pin)),
            ).rowcount
            if changed != 1:
                raise LifecycleError(
                    "release pin compare-and-swap failed",
                    code=ErrorCode.RELEASE_RETIRING,
                )
            return result

    def active_pins(self, release_id: str) -> tuple[dict[str, Any], ...]:
        rows = self.repository.connection.execute(
            """
            SELECT pin_json FROM p2_plugin_release_pin
             WHERE release_id=? AND json_extract(pin_json,'$.released_at') IS NULL
             ORDER BY pin_id
            """,
            (release_id,),
        ).fetchall()
        return tuple(_load(row[0]) for row in rows)

    def require_install_pin(
        self,
        connection: sqlite3.Connection,
        release_id: str,
        owner_id: str,
    ) -> None:
        """Fence shadow creation to the exact installed release/install owner."""
        row = connection.execute(
            "SELECT retirement_json FROM p2_plugin_release_retirement WHERE release_id=?",
            (release_id,),
        ).fetchone()
        if row is None:
            raise LifecycleError(
                "shadow release is absent from retirement authority",
                code=ErrorCode.RELEASE_RETIRING,
            )
        retirement = _load(row[0])
        if retirement["state"] != "installed" or not retirement["package_present"]:
            raise LifecycleError(
                "shadow release is not installed/executable",
                code=ErrorCode.RELEASE_RETIRING,
            )
        active = connection.execute(
            """
            SELECT 1 FROM p2_plugin_release_pin
             WHERE release_id=? AND retire_epoch=?
               AND json_extract(pin_json,'$.pin_kind')='install'
               AND json_extract(pin_json,'$.owner_id')=?
               AND json_extract(pin_json,'$.released_at') IS NULL
            """,
            (release_id, retirement["retire_epoch"], owner_id),
        ).fetchone()
        if active is None:
            raise LifecycleError(
                "shadow release lost its active install pin",
                code=ErrorCode.RELEASE_RETIRING,
            )

    @staticmethod
    def _generation_pin_id(owner_id: str, release_id: str) -> str:
        digest = hashlib.sha256(f"{owner_id}\0{release_id}".encode()).hexdigest()
        return f"pin-pending-{digest[:40]}"

    def acquire_generation_pins(
        self,
        generations: Sequence[Mapping[str, Any]],
        *,
        owner_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Atomically pin every target and rollback-base release."""
        release_ids = sorted(
            {
                member["release_id"]
                for generation in generations
                for member in generation["members"]
            }
        )
        with self.repository.transaction():
            return tuple(
                self.acquire_pin(
                    release_id,
                    pin_kind="pending_generation",
                    owner_id=owner_id,
                    pin_id=self._generation_pin_id(owner_id, release_id),
                )
                for release_id in release_ids
            )

    def require_generation_executable(
        self,
        connection: sqlite3.Connection,
        generation: Mapping[str, Any],
        owner_id: str,
    ) -> None:
        """Recheck retirement epoch/package state under the caller's transaction."""
        for member in generation["members"]:
            release_id = member["release_id"]
            row = connection.execute(
                "SELECT retirement_json FROM p2_plugin_release_retirement WHERE release_id=?",
                (release_id,),
            ).fetchone()
            if row is None:
                raise LifecycleError(
                    "Generation release is absent from retirement authority",
                    code=ErrorCode.RELEASE_RETIRING,
                )
            retirement = _load(row[0])
            if retirement["state"] != "installed" or not retirement["package_present"]:
                raise LifecycleError(
                    "Generation release is not executable",
                    code=ErrorCode.RELEASE_RETIRING,
                )
            pin = connection.execute(
                """
                SELECT 1 FROM p2_plugin_release_pin
                 WHERE release_id=? AND retire_epoch=?
                   AND json_extract(pin_json,'$.pin_kind')='pending_generation'
                   AND json_extract(pin_json,'$.owner_id')=?
                   AND json_extract(pin_json,'$.released_at') IS NULL
                """,
                (release_id, retirement["retire_epoch"], owner_id),
            ).fetchone()
            if pin is None:
                raise LifecycleError(
                    "Generation release lost its pending pin",
                    code=ErrorCode.RELEASE_RETIRING,
                )

    def release_owner_pins(self, owner_id: str, *, at: str | None = None) -> None:
        now = at or utc_now()
        with self.repository.transaction() as connection:
            self.release_owner_pins_in_transaction(connection, owner_id, at=now)

    def release_owner_pins_in_transaction(
        self,
        connection: sqlite3.Connection,
        owner_id: str,
        *,
        at: str | None = None,
    ) -> None:
        now = at or utc_now()
        rows = connection.execute(
            """
            SELECT pin_id FROM p2_plugin_release_pin
             WHERE json_extract(pin_json,'$.owner_id')=?
               AND json_extract(pin_json,'$.pin_kind') IN ('install','pending_generation')
               AND json_extract(pin_json,'$.released_at') IS NULL
             ORDER BY pin_id
            """,
            (owner_id,),
        ).fetchall()
        for row in rows:
            self.release_pin(str(row[0]), at=now)

    def _generation_barriers(self, release_id: str) -> tuple[str, ...]:
        state = self.repository.generation_state()
        owners: list[str] = []
        for kind, generation in (
            ("current_generation", state.current),
            ("lkg_generation", state.lkg),
        ):
            if generation is not None and any(
                member["release_id"] == release_id for member in generation["members"]
            ):
                owners.append(f"{kind}:{generation['generation_id']}")
        return tuple(owners)

    def start_retirement(
        self,
        release_id: str,
        *,
        operation_id: str,
        event_writer: RetirementEventWriter,
        expected_epoch: int | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        now = at or utc_now()
        with self.repository.transaction() as connection:
            current = self.get(release_id)
            if current["state"] == "retiring":
                return current
            if current["state"] == "retired":
                return current
            if expected_epoch is not None and current["retire_epoch"] != expected_epoch:
                raise LifecycleError(
                    "release retirement epoch is stale", code=ErrorCode.RELEASE_RETIRING
                )
            pins = self.active_pins(release_id)
            generation_barriers = self._generation_barriers(release_id)
            if pins or generation_barriers:
                raise LifecycleError(
                    "active executable references block retirement",
                    code=ErrorCode.RELEASE_RETIRING,
                    details={
                        "pin_ids": [pin["pin_id"] for pin in pins],
                        "generation_owners": list(generation_barriers),
                    },
                )
            result = copy.deepcopy(current)
            result.update(
                state="retiring",
                retire_epoch=current["retire_epoch"] + 1,
                started_at=now,
                completed_at=None,
            )
            assert_valid("release-retirement/v1", result)
            changed = connection.execute(
                "UPDATE p2_plugin_release_retirement SET retirement_json=?,revision=revision+1 WHERE release_id=? AND retirement_json=?",
                (_dump(result), release_id, _dump(current)),
            ).rowcount
            if changed != 1:
                raise LifecycleError(
                    "release retirement compare-and-swap failed",
                    code=ErrorCode.RELEASE_RETIRING,
                )
            event_writer(connection, release_id, result["retire_epoch"], operation_id)
            return result

    def complete_retirement(
        self,
        release_id: str,
        *,
        package_remover: PackageRemover,
        publication_barrier: PublicationBarrier,
        expected_epoch: int | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        now = at or utc_now()
        with self.repository.transaction() as connection:
            current = self.get(release_id)
            if current["state"] == "retired":
                return current
            if current["state"] != "retiring":
                raise LifecycleError(
                    "release must be retiring before completion",
                    code=ErrorCode.RELEASE_RETIRING,
                )
            if expected_epoch is not None and current["retire_epoch"] != expected_epoch:
                raise LifecycleError(
                    "release retirement epoch is stale", code=ErrorCode.RELEASE_RETIRING
                )
            pins = self.active_pins(release_id)
            generation_barriers = self._generation_barriers(release_id)
            if pins or generation_barriers:
                raise LifecycleError(
                    "active references still block retirement",
                    code=ErrorCode.RELEASE_RETIRING,
                )
            decision = publication_barrier(connection, release_id)
            if (
                not isinstance(decision, PublicationBarrierDecision)
                or decision.release_id != release_id
            ):
                raise LifecycleError(
                    "Candidate/Publication barrier returned an invalid release-bound decision",
                    code=ErrorCode.RESULT_CONTRACT_MISMATCH,
                )
            if not decision.allowed:
                connection.execute(
                    """
                    INSERT INTO p2_plugin_retirement_attention(release_id,reason,updated_at)
                    VALUES(?,?,?)
                    ON CONFLICT(release_id) DO UPDATE SET reason=excluded.reason,updated_at=excluded.updated_at
                    """,
                    (
                        release_id,
                        "candidate_or_publication_requires_executable_release:"
                        + ",".join(decision.blocker_ids),
                        now,
                    ),
                )
                return current
            connection.execute(
                "DELETE FROM p2_plugin_retirement_attention WHERE release_id=?",
                (release_id,),
            )
            if not package_remover():
                # Deletion failure is recoverable: remain retiring and retry on startup.
                return current
            result = copy.deepcopy(current)
            result.update(state="retired", package_present=False, completed_at=now)
            assert_valid("release-retirement/v1", result)
            changed = connection.execute(
                "UPDATE p2_plugin_release_retirement SET retirement_json=?,revision=revision+1 WHERE release_id=? AND retirement_json=?",
                (_dump(result), release_id, _dump(current)),
            ).rowcount
            if changed != 1:
                raise LifecycleError(
                    "release retirement completion CAS failed",
                    code=ErrorCode.RELEASE_RETIRING,
                )
            return result

    def attention(self, release_id: str) -> str | None:
        row = self.repository.connection.execute(
            "SELECT reason FROM p2_plugin_retirement_attention WHERE release_id=?",
            (release_id,),
        ).fetchone()
        return None if row is None else str(row[0])

    def require_executable(
        self, release_id: str, *, retire_epoch: int | None = None
    ) -> dict[str, Any]:
        value = self.get(release_id)
        if value["state"] != "installed" or (
            retire_epoch is not None and value["retire_epoch"] != retire_epoch
        ):
            raise LifecycleError(
                "release is retiring or retired", code=ErrorCode.RELEASE_RETIRING
            )
        return value

    def require_post_delete_publication(
        self,
        release_id: str,
        *,
        publication_verifier: PublicationBarrier,
    ) -> None:
        with self.repository.transaction() as connection:
            value = self.get(release_id)
            if value["package_present"]:
                return
            decision = publication_verifier(connection, release_id)
            if (
                not isinstance(decision, PublicationBarrierDecision)
                or decision.release_id != release_id
                or not decision.allowed
            ):
                raise LifecycleError(
                    "post-delete Publication lacks retained Core-owned evidence",
                    code=ErrorCode.RESULT_CONTRACT_MISMATCH,
                )


__all__ = [
    "PackageRemover",
    "PublicationBarrier",
    "PublicationBarrierDecision",
    "RetirementEventWriter",
    "RetirementManager",
]
