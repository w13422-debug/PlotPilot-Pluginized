"""Durable shadow data-generation leases and migration checkpoints."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from plotpilot_plugin_sdk.errors import ErrorCode

from .repository import LifecycleError, LifecycleRepository, utc_now

InstallPinGuard = Callable[[sqlite3.Connection, str, str], None]

_STATES = (
    "prepared",
    "applying",
    "applied",
    "verified",
    "qualified",
    "failed",
    "retired",
)
_NEXT = {
    "prepared": "applying",
    "applying": "applied",
    "applied": "verified",
    "verified": "qualified",
}


def _parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LifecycleError(
            "invalid shadow lease timestamp", code=ErrorCode.STALE_LEASE
        ) from exc
    if result.tzinfo is None:
        raise LifecycleError(
            "shadow lease timestamp must be UTC", code=ErrorCode.STALE_LEASE
        )
    return result.astimezone(UTC)


@dataclass(frozen=True)
class ShadowLease:
    shadow_data_generation_id: str
    install_operation_id: str
    release_id: str
    state: str
    db_lease_id: str
    db_lease_epoch: int
    owner_instance_id: str
    issued_at: str
    expires_at: str
    renewed_at: str
    lease_state: str


class ShadowGenerationManager:
    """Manage one fenced migration lease per shadow data generation."""

    def __init__(
        self,
        repository: LifecycleRepository,
        install_pin_guard: InstallPinGuard,
    ) -> None:
        self.repository = repository
        self.install_pin_guard = install_pin_guard

    @staticmethod
    def _row(row: sqlite3.Row | tuple[object, ...]) -> ShadowLease:
        return ShadowLease(
            shadow_data_generation_id=str(row[0]),
            install_operation_id=str(row[1]),
            release_id=str(row[2]),
            state=str(row[3]),
            db_lease_id=str(row[4]),
            db_lease_epoch=int(row[5]),
            owner_instance_id=str(row[6]),
            issued_at=str(row[7]),
            expires_at=str(row[8]),
            renewed_at=str(row[9]),
            lease_state=str(row[10]),
        )

    def get(self, shadow_data_generation_id: str) -> ShadowLease:
        return self._get(self.repository.connection, shadow_data_generation_id)

    def _get(
        self,
        connection: sqlite3.Connection,
        shadow_data_generation_id: str,
    ) -> ShadowLease:
        row = connection.execute(
            """
            SELECT shadow_data_generation_id,install_operation_id,release_id,state,
                   db_lease_id,db_lease_epoch,owner_instance_id,issued_at,expires_at,
                   renewed_at,lease_state
              FROM p2_plugin_shadow_generation WHERE shadow_data_generation_id=?
            """,
            (shadow_data_generation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(shadow_data_generation_id)
        return self._row(row)

    def _current_for_update(
        self,
        connection: sqlite3.Connection,
        shadow_data_generation_id: str,
        *,
        db_lease_id: str,
        db_lease_epoch: int,
        owner_instance_id: str,
        instant: str,
    ) -> ShadowLease:
        current = self._get(connection, shadow_data_generation_id)
        if current.state in {"qualified", "failed", "retired"}:
            raise LifecycleError(
                "terminal shadow Generation is immutable",
                code=ErrorCode.INVALID_TRANSITION,
            )
        attempt = self.repository._attempt_for_update(
            connection, current.install_operation_id
        )
        request = self.repository._request_for_update(
            connection, current.install_operation_id
        )
        if (
            attempt["shadow_data_generation_id"] != shadow_data_generation_id
            or attempt["state"] not in {"shadow_prepared", "migrated"}
            or request.get("release_id") != current.release_id
        ):
            raise LifecycleError(
                "shadow mutation is not bound to its active install Attempt",
                code=ErrorCode.INVALID_TRANSITION,
            )
        self.install_pin_guard(
            connection, current.release_id, current.install_operation_id
        )
        if (
            current.lease_state != "active"
            or current.db_lease_id != db_lease_id
            or current.db_lease_epoch != db_lease_epoch
            or current.owner_instance_id != owner_instance_id
            or _parse_time(current.expires_at) <= _parse_time(instant)
        ):
            raise LifecycleError("stale shadow DB lease", code=ErrorCode.STALE_LEASE)
        return current

    def prepare(
        self,
        *,
        install_operation_id: str,
        shadow_data_generation_id: str,
        release_id: str,
        owner_instance_id: str,
        ttl_seconds: int = 60,
        now: str | None = None,
    ) -> ShadowLease:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        issued = now or utc_now()
        expires = (
            (_parse_time(issued) + timedelta(seconds=ttl_seconds))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        lease_id = f"shadow-lease-{uuid.uuid4().hex}"
        with self.repository.transaction() as connection:
            attempt = self.repository._attempt_for_update(
                connection, install_operation_id
            )
            request = self.repository.get_attempt_request(install_operation_id)
            if request.get("release_id") != release_id:
                raise LifecycleError(
                    "shadow release differs from the frozen install request",
                    code=ErrorCode.DUPLICATE_REQUEST,
                )
            row = connection.execute(
                "SELECT shadow_data_generation_id FROM p2_plugin_shadow_generation WHERE install_operation_id=?",
                (install_operation_id,),
            ).fetchone()
            if row is not None:
                existing = self._get(connection, str(row[0]))
                if (
                    attempt["state"]
                    not in {
                        "shadow_prepared",
                        "migrated",
                        "settings_validated",
                        "qualified",
                        "pending_apply",
                        "current_committed",
                        "lkg_pending",
                        "lkg_promoted",
                    }
                    or attempt["shadow_data_generation_id"] != shadow_data_generation_id
                    or existing.shadow_data_generation_id != shadow_data_generation_id
                    or existing.release_id != release_id
                ):
                    raise LifecycleError(
                        "install operation already owns a different shadow generation",
                        code=ErrorCode.DUPLICATE_REQUEST,
                    )
                return existing
            if attempt["state"] != "env_prepared":
                raise LifecycleError(
                    "shadow generation requires an env_prepared install attempt"
                )
            self.install_pin_guard(connection, release_id, install_operation_id)
            connection.execute(
                """
                INSERT INTO p2_plugin_shadow_generation(
                    shadow_data_generation_id,install_operation_id,release_id,state,
                    db_lease_id,db_lease_epoch,owner_instance_id,issued_at,expires_at,
                    renewed_at,lease_state
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    shadow_data_generation_id,
                    install_operation_id,
                    release_id,
                    "prepared",
                    lease_id,
                    1,
                    owner_instance_id,
                    issued,
                    expires,
                    issued,
                    "active",
                ),
            )
            self.repository.advance_attempt(
                install_operation_id,
                expected_state="env_prepared",
                target_state="shadow_prepared",
                updates={"shadow_data_generation_id": shadow_data_generation_id},
                at=issued,
            )
        return self.get(shadow_data_generation_id)

    def acquire(
        self,
        shadow_data_generation_id: str,
        *,
        owner_instance_id: str,
        ttl_seconds: int = 60,
        now: str | None = None,
    ) -> ShadowLease:
        """Acquire/recover a lease; an expired lease always receives a new epoch."""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        instant = now or utc_now()
        now_dt = _parse_time(instant)
        with self.repository.transaction() as connection:
            current = self._get(connection, shadow_data_generation_id)
            if current.state in {"qualified", "failed", "retired"}:
                raise LifecycleError(
                    "terminal shadow Generation cannot acquire a lease",
                    code=ErrorCode.INVALID_TRANSITION,
                )
            attempt = self.repository._attempt_for_update(
                connection, current.install_operation_id
            )
            request = self.repository._request_for_update(
                connection, current.install_operation_id
            )
            if (
                attempt["shadow_data_generation_id"] != shadow_data_generation_id
                or attempt["state"] not in {"shadow_prepared", "migrated"}
                or request.get("release_id") != current.release_id
            ):
                raise LifecycleError(
                    "shadow lease is not bound to its active install Attempt"
                )
            self.install_pin_guard(
                connection, current.release_id, current.install_operation_id
            )
            if (
                current.lease_state == "active"
                and _parse_time(current.expires_at) > now_dt
            ):
                if current.owner_instance_id != owner_instance_id:
                    raise LifecycleError(
                        "shadow DB lease is owned by another instance",
                        code=ErrorCode.STALE_LEASE,
                    )
                return current
            expires = (
                (now_dt + timedelta(seconds=ttl_seconds))
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            changed = connection.execute(
                """
                UPDATE p2_plugin_shadow_generation
                   SET db_lease_id=?,db_lease_epoch=db_lease_epoch+1,
                       owner_instance_id=?,issued_at=?,expires_at=?,renewed_at=?,
                       lease_state='active',revision=revision+1
                 WHERE shadow_data_generation_id=? AND db_lease_epoch=?
                """,
                (
                    f"shadow-lease-{uuid.uuid4().hex}",
                    owner_instance_id,
                    instant,
                    expires,
                    instant,
                    shadow_data_generation_id,
                    current.db_lease_epoch,
                ),
            ).rowcount
            if changed != 1:
                raise LifecycleError(
                    "shadow DB lease compare-and-swap failed",
                    code=ErrorCode.STALE_LEASE,
                )
        return self.get(shadow_data_generation_id)

    def assert_current(
        self,
        shadow_data_generation_id: str,
        *,
        db_lease_id: str,
        db_lease_epoch: int,
        owner_instance_id: str,
        now: str | None = None,
    ) -> ShadowLease:
        instant = now or utc_now()
        with self.repository.transaction() as connection:
            return self._current_for_update(
                connection,
                shadow_data_generation_id,
                db_lease_id=db_lease_id,
                db_lease_epoch=db_lease_epoch,
                owner_instance_id=owner_instance_id,
                instant=instant,
            )

    def renew(
        self,
        shadow_data_generation_id: str,
        *,
        db_lease_id: str,
        db_lease_epoch: int,
        owner_instance_id: str,
        ttl_seconds: int = 60,
        now: str | None = None,
    ) -> ShadowLease:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        instant = now or utc_now()
        expires = (
            (_parse_time(instant) + timedelta(seconds=ttl_seconds))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        with self.repository.transaction() as connection:
            current = self._current_for_update(
                connection,
                shadow_data_generation_id,
                db_lease_id=db_lease_id,
                db_lease_epoch=db_lease_epoch,
                owner_instance_id=owner_instance_id,
                instant=instant,
            )
            changed = connection.execute(
                """
                UPDATE p2_plugin_shadow_generation SET expires_at=?,renewed_at=?,revision=revision+1
                 WHERE shadow_data_generation_id=? AND db_lease_id=? AND db_lease_epoch=?
                   AND owner_instance_id=? AND lease_state='active' AND expires_at=?
                   AND julianday(expires_at)>julianday(?)
                """,
                (
                    expires,
                    instant,
                    shadow_data_generation_id,
                    current.db_lease_id,
                    current.db_lease_epoch,
                    owner_instance_id,
                    current.expires_at,
                    instant,
                ),
            ).rowcount
            if changed != 1:
                raise LifecycleError(
                    "shadow lease renew lost its fence", code=ErrorCode.STALE_LEASE
                )
        return self.get(shadow_data_generation_id)

    def advance(
        self,
        shadow_data_generation_id: str,
        *,
        expected_state: str,
        target_state: str,
        db_lease_id: str,
        db_lease_epoch: int,
        owner_instance_id: str,
        now: str | None = None,
    ) -> ShadowLease:
        instant = now or utc_now()
        with self.repository.transaction() as connection:
            current = self._current_for_update(
                connection,
                shadow_data_generation_id,
                db_lease_id=db_lease_id,
                db_lease_epoch=db_lease_epoch,
                owner_instance_id=owner_instance_id,
                instant=instant,
            )
            if current.state == target_state:
                return current
            if (
                current.state != expected_state
                or _NEXT.get(expected_state) != target_state
            ):
                raise LifecycleError(
                    f"invalid shadow migration transition {current.state} -> {target_state}",
                    code=ErrorCode.MIGRATION_FAILED,
                )
            terminal_lease = "released" if target_state == "qualified" else "active"
            changed = connection.execute(
                """
                UPDATE p2_plugin_shadow_generation SET state=?,lease_state=?,revision=revision+1
                 WHERE shadow_data_generation_id=? AND state=? AND db_lease_id=?
                   AND db_lease_epoch=? AND owner_instance_id=? AND lease_state='active'
                   AND expires_at=? AND julianday(expires_at)>julianday(?)
                """,
                (
                    target_state,
                    terminal_lease,
                    shadow_data_generation_id,
                    expected_state,
                    db_lease_id,
                    db_lease_epoch,
                    owner_instance_id,
                    current.expires_at,
                    instant,
                ),
            ).rowcount
            if changed != 1:
                raise LifecycleError(
                    "shadow migration compare-and-swap failed",
                    code=ErrorCode.STALE_LEASE,
                )
            if target_state == "qualified":
                attempt = self.repository.get_attempt(current.install_operation_id)
                if attempt["state"] == "shadow_prepared":
                    self.repository.advance_attempt(
                        current.install_operation_id,
                        expected_state="shadow_prepared",
                        target_state="migrated",
                        at=instant,
                    )
        return self.get(shadow_data_generation_id)

    def fail(
        self,
        shadow_data_generation_id: str,
        *,
        db_lease_id: str,
        db_lease_epoch: int,
        owner_instance_id: str,
        now: str | None = None,
    ) -> ShadowLease:
        instant = now or utc_now()
        with self.repository.transaction() as connection:
            current = self._current_for_update(
                connection,
                shadow_data_generation_id,
                db_lease_id=db_lease_id,
                db_lease_epoch=db_lease_epoch,
                owner_instance_id=owner_instance_id,
                instant=instant,
            )
            changed = connection.execute(
                """
                UPDATE p2_plugin_shadow_generation SET state='failed',lease_state='released',revision=revision+1
                 WHERE shadow_data_generation_id=? AND db_lease_id=? AND db_lease_epoch=?
                   AND owner_instance_id=? AND lease_state='active' AND expires_at=?
                   AND julianday(expires_at)>julianday(?)
                """,
                (
                    shadow_data_generation_id,
                    current.db_lease_id,
                    current.db_lease_epoch,
                    owner_instance_id,
                    current.expires_at,
                    instant,
                ),
            ).rowcount
            if changed != 1:
                raise LifecycleError(
                    "shadow failure lost its lease fence", code=ErrorCode.STALE_LEASE
                )
        return self.get(shadow_data_generation_id)

    def release(
        self,
        shadow_data_generation_id: str,
        *,
        db_lease_id: str,
        db_lease_epoch: int,
        owner_instance_id: str,
        now: str | None = None,
    ) -> ShadowLease:
        instant = now or utc_now()
        with self.repository.transaction() as connection:
            current = self._get(connection, shadow_data_generation_id)
            if current.state in {"qualified", "failed", "retired"}:
                raise LifecycleError(
                    "terminal shadow Generation cannot release a lease",
                    code=ErrorCode.INVALID_TRANSITION,
                )
            if current.lease_state == "released":
                if (
                    current.db_lease_id == db_lease_id
                    and current.db_lease_epoch == db_lease_epoch
                    and current.owner_instance_id == owner_instance_id
                ):
                    return current
                raise LifecycleError(
                    "released shadow lease identity differs", code=ErrorCode.STALE_LEASE
                )
            current = self._current_for_update(
                connection,
                shadow_data_generation_id,
                db_lease_id=db_lease_id,
                db_lease_epoch=db_lease_epoch,
                owner_instance_id=owner_instance_id,
                instant=instant,
            )
            changed = connection.execute(
                """
                UPDATE p2_plugin_shadow_generation SET lease_state='released',revision=revision+1
                 WHERE shadow_data_generation_id=? AND db_lease_id=? AND db_lease_epoch=?
                   AND owner_instance_id=? AND lease_state='active' AND expires_at=?
                   AND julianday(expires_at)>julianday(?)
                """,
                (
                    shadow_data_generation_id,
                    db_lease_id,
                    db_lease_epoch,
                    owner_instance_id,
                    current.expires_at,
                    instant,
                ),
            ).rowcount
            if changed != 1:
                raise LifecycleError(
                    "shadow release lost its lease fence", code=ErrorCode.STALE_LEASE
                )
        return self.get(shadow_data_generation_id)

    def require_qualified(self, shadow_data_generation_id: str) -> ShadowLease:
        value = self.get(shadow_data_generation_id)
        if value.state != "qualified" or value.lease_state != "released":
            raise LifecycleError(
                "shadow data generation is not qualified and immutable",
                code=ErrorCode.MIGRATION_FAILED,
            )
        return value


__all__ = ["ShadowGenerationManager", "ShadowLease"]
