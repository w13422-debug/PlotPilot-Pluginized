"""Thin orchestration over verified packages and the durable lifecycle journal."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from plotpilot_plugin_sdk.errors import ErrorCode

from ..generation import validate_generation
from ..install import InstallStager
from ..package import CompatibilityProfile, verify_package
from ..settings import require_activatable
from .repository import (
    EventWriter,
    LifecycleError,
    LifecycleRepository,
    QualificationVerifier,
    initial_transition,
)
from .retirement import RetirementManager
from .shadow import ShadowGenerationManager


class InstallLifecycleService:
    """Drive one public transition; every method is replay-safe."""

    def __init__(
        self,
        repository: LifecycleRepository,
        stager: InstallStager,
        *,
        shadow: ShadowGenerationManager | None = None,
        retirement: RetirementManager | None = None,
    ) -> None:
        self.repository = repository
        self.stager = stager
        self.retirement = (
            retirement if retirement is not None else RetirementManager(repository)
        )
        self.shadow = (
            shadow
            if shadow is not None
            else ShadowGenerationManager(repository, self.retirement.require_install_pin)
        )

    @staticmethod
    def _install_pin_id(install_operation_id: str) -> str:
        return f"pin-install-{hashlib.sha256(install_operation_id.encode('utf-8')).hexdigest()[:32]}"

    def begin(
        self,
        source: Any,
        *,
        install_operation_id: str,
        target_generation_id: str,
        compatibility: CompatibilityProfile | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Verify input, create/recover intent, stage and nonreplace-publish."""
        package = verify_package(
            source, compatibility=compatibility, limits=self.stager.limits
        )
        try:
            frozen = self.repository.get_attempt(install_operation_id)
        except KeyError:
            state = self.repository.generation_state()
            base_id = None if state.current is None else state.current["generation_id"]
            lkg_id = None if state.lkg is None else state.lkg["generation_id"]
        else:
            # Replay remains bound to the original CAS base even if another
            # operation has since changed current/LKG.
            base_id = frozen["base_generation_id"]
            lkg_id = frozen["base_lkg_generation_id"]
        request = {
            "plugin_id": package.plugin_id,
            "version": package.version,
            "package_hash": package.package_hash,
            "release_id": package.release_id,
            "base_generation_id": base_id,
            "base_lkg_generation_id": lkg_id,
            "target_generation_id": target_generation_id,
        }
        attempt = self.repository.create_or_recover_attempt(
            initial_transition(
                install_operation_id,
                base_generation_id=base_id,
                base_lkg_generation_id=lkg_id,
                target_generation_id=target_generation_id,
            ),
            request=request,
        )
        if attempt["state"] == "selected":
            self.stager.stage(
                source,
                install_operation_id=install_operation_id,
                base_generation_id=base_id,
                expected_package_hash=package.package_hash,
                expected_release_id=package.release_id,
                compatibility=compatibility,
            )
            attempt = self.repository.advance_attempt(
                install_operation_id,
                expected_state="selected",
                target_state="staged",
                updates={"package_store_status": "staged"},
            )
        if attempt["state"] == "staged":
            published = self.stager.reconcile(install_operation_id)
            if published.package.release_id != package.release_id:
                raise LifecycleError(
                    "staged package identity changed", code=ErrorCode.ASSET_ERROR
                )
            with self.repository.transaction():
                self.retirement.register_installed(package.release_id)
                self.retirement.acquire_pin(
                    package.release_id,
                    pin_kind="install",
                    owner_id=install_operation_id,
                    pin_id=self._install_pin_id(install_operation_id),
                )
            attempt = self.repository.advance_attempt(
                install_operation_id,
                expected_state="staged",
                target_state="package_published",
                updates={"package_store_status": "published"},
            )
        elif attempt["state"] not in {
            "failed",
            "superseded",
            "rolled_back",
            "safe_mode",
        }:
            # Verify/adopt exact bytes after any later-stage restart as well.
            self.stager.reconcile(install_operation_id)
        return attempt

    def mark_environment_prepared(self, install_operation_id: str) -> dict[str, Any]:
        return self.repository.advance_attempt(
            install_operation_id,
            expected_state="package_published",
            target_state="env_prepared",
        )

    def mark_no_shadow_required(self, install_operation_id: str) -> dict[str, Any]:
        self.repository.advance_attempt(
            install_operation_id,
            expected_state="env_prepared",
            target_state="shadow_prepared",
            updates={"shadow_data_generation_id": None},
        )
        return self.repository.advance_attempt(
            install_operation_id,
            expected_state="shadow_prepared",
            target_state="migrated",
        )

    def mark_settings_validated(
        self,
        install_operation_id: str,
        *,
        generation: Mapping[str, Any],
        revisions: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        candidate = validate_generation(generation)
        attempt = self.repository.get_attempt(install_operation_id)
        if attempt["target_generation_id"] != candidate["generation_id"]:
            raise LifecycleError(
                "settings validation target differs from install Generation",
                code=ErrorCode.INCOMPATIBLE_GENERATION,
            )
        bindings: list[dict[str, str]] = []
        for member in candidate["members"]:
            revision_id = member["global_settings_revision_id"]
            schema_hash = member["settings_schema_hash"]
            if (revision_id is None) != (schema_hash is None):
                raise LifecycleError(
                    "Generation settings ID/schema must be both null or both present",
                    code=ErrorCode.SETTINGS_INVALID,
                )
            if revision_id is None:
                continue
            revision = revisions.get(member["plugin_id"])
            if revision is None or revision.get("settings_revision_id") != revision_id:
                raise LifecycleError(
                    "target settings revision is missing",
                    code=ErrorCode.SETTINGS_INVALID,
                )
            require_activatable(
                revision, release_id=member["release_id"], schema_hash=schema_hash
            )
            bindings.append(
                {"plugin_id": member["plugin_id"], "settings_revision_id": revision_id}
            )
        bindings.sort(key=lambda item: item["plugin_id"].encode("utf-8"))
        return self.repository.advance_attempt(
            install_operation_id,
            expected_state="migrated",
            target_state="settings_validated",
            updates={"target_settings_revision_ids": bindings},
        )

    def qualify(
        self,
        install_operation_id: str,
        *,
        generation: Mapping[str, Any],
        evidence: Mapping[str, Any],
        qualification_verifier: QualificationVerifier,
    ) -> dict[str, Any]:
        candidate = validate_generation(generation)
        attempt = self.repository.get_attempt(install_operation_id)
        if attempt["target_generation_id"] != candidate["generation_id"]:
            raise LifecycleError(
                "qualification target differs from install Generation",
                code=ErrorCode.INCOMPATIBLE_GENERATION,
            )
        if (
            evidence.get("generation_id") != candidate["generation_id"]
            or evidence.get("health_result_asset_id")
            != candidate["health_result_asset_id"]
        ):
            raise LifecycleError(
                "qualification evidence is bound to another Generation/health result",
                code=ErrorCode.RESULT_CONTRACT_MISMATCH,
            )
        if attempt["shadow_data_generation_id"] is not None:
            self.shadow.require_qualified(attempt["shadow_data_generation_id"])
        return self.repository.qualify_attempt(
            install_operation_id,
            generation=candidate,
            evidence=evidence,
            qualification_verifier=qualification_verifier,
            execution_guard=self._qualification_guard,
        )

    def _qualification_guard(
        self,
        _connection: Any,
        generation: Mapping[str, Any],
        install_operation_id: str,
    ) -> None:
        attempt = self.repository.get_attempt(install_operation_id)
        generations = [generation]
        if attempt["base_generation_id"] is not None:
            generations.append(
                self.repository.get_generation(attempt["base_generation_id"])
            )
        self.retirement.acquire_generation_pins(
            generations,
            owner_id=install_operation_id,
        )

    def _execution_guard(
        self,
        connection: Any,
        generation: Mapping[str, Any],
        install_operation_id: str,
    ) -> None:
        self.retirement.require_generation_executable(
            connection,
            generation,
            install_operation_id,
        )
        for member in generation["members"]:
            try:
                self.stager.store.require_release(
                    release_id=member["release_id"],
                    plugin_id=member["plugin_id"],
                    package_hash=member["package_hash"],
                )
            except Exception as exc:
                raise LifecycleError(
                    "Generation package bytes are unavailable",
                    code=ErrorCode.ASSET_ERROR,
                ) from exc

    def commit_current(
        self,
        install_operation_id: str,
        *,
        generation: Mapping[str, Any],
        event_writer: EventWriter,
    ) -> dict[str, Any]:
        attempt = self.repository.get_attempt(install_operation_id)
        if attempt["state"] == "qualified":
            self.repository.advance_attempt(
                install_operation_id,
                expected_state="qualified",
                target_state="pending_apply",
            )
        return self.repository.commit_current(
            install_operation_id,
            generation,
            event_writer=event_writer,
            execution_guard=self._execution_guard,
            pin_releaser=self._pin_releaser,
        )

    def fail(
        self,
        install_operation_id: str,
        *,
        expected_state: str,
        failure_code: str,
    ) -> dict[str, Any]:
        return self.repository.fail_attempt(
            install_operation_id,
            expected_state=expected_state,
            failure_code=failure_code,
            pin_releaser=self._pin_releaser,
        )

    def promote_lkg(
        self,
        install_operation_id: str,
        *,
        evidence: Mapping[str, Any],
        qualification_verifier: QualificationVerifier,
    ) -> dict[str, Any]:
        attempt = self.repository.get_attempt(install_operation_id)
        if attempt["state"] == "current_committed":
            attempt = self.repository.mark_lkg_pending(install_operation_id)
        result = self.repository.promote_lkg(
            install_operation_id,
            evidence=evidence,
            qualification_verifier=qualification_verifier,
            pin_releaser=self._pin_releaser,
        )
        return result

    def rollback(
        self,
        install_operation_id: str,
        *,
        event_writer: EventWriter,
    ) -> dict[str, Any]:
        token = self.repository.arm_rollback(install_operation_id)
        result = self.repository.complete_rollback(
            install_operation_id,
            token=token,
            event_writer=event_writer,
            execution_guard=self._execution_guard,
            pin_releaser=self._pin_releaser,
        )
        return result

    def _pin_releaser(
        self,
        connection: Any,
        install_operation_id: str,
    ) -> None:
        self.retirement.release_owner_pins_in_transaction(
            connection,
            install_operation_id,
        )


__all__ = ["InstallLifecycleService"]
