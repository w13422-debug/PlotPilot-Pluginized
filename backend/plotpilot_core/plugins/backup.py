"""P2 Generation contributor bound to an already-frozen Core backup barrier."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol

from plotpilot_core.backup.models import BackupBarrier, BackupMode, GenerationSnapshot
from plotpilot_core.plugins.generation import GenerationState, validate_generation

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class GenerationBackupError(ValueError):
    """The P2 durable projection is absent, malformed, or not immutable."""


class GenerationStateSource(Protocol):
    def generation_state(self) -> GenerationState: ...


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise GenerationBackupError(f"{label} is not a closed identifier")
    return value


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise GenerationBackupError(f"{label} is not lowercase SHA-256")
    return value


def _generation(value: Mapping[str, Any] | None, label: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise GenerationBackupError(f"{label} Generation is not a mapping")
    try:
        return validate_generation(value)
    except Exception as exc:
        raise GenerationBackupError(f"{label} Generation failed contract validation") from exc


def _release_projection(generations: tuple[Mapping[str, Any] | None, ...]) -> tuple[dict[str, object], ...]:
    """Record only identities that the immutable Generation actually contains.

    A Generation has no version field and this Stage 1 contributor does not
    fabricate one or claim a package archive it did not produce.  Restore can
    therefore report a missing release explicitly instead of substituting one.
    """

    releases: dict[tuple[str, str], dict[str, object]] = {}
    for generation in generations:
        if generation is None:
            continue
        for member in generation["members"]:
            if not isinstance(member, Mapping):
                raise GenerationBackupError("Generation member is not a mapping")
            plugin_id = _id(member.get("plugin_id"), "member.plugin_id")
            release_id = _hash(member.get("release_id"), "member.release_id")
            package_hash = _hash(member.get("package_hash"), "member.package_hash")
            projected = {
                "plugin_id": plugin_id,
                "release_id": release_id,
                "package_hash": package_hash,
                "package_present": False,
            }
            key = plugin_id, release_id
            previous = releases.get(key)
            if previous is not None and previous != projected:
                raise GenerationBackupError("same Generation release identity has conflicting bytes")
            releases[key] = projected
    return tuple(
        releases[key]
        for key in sorted(
            releases,
            key=lambda item: (item[0].encode("utf-8"), item[1].encode("utf-8")),
        )
    )


def _asset_ids(generations: tuple[Mapping[str, Any] | None, ...]) -> tuple[str, ...]:
    values: set[str] = set()
    for generation in generations:
        if generation is None:
            continue
        value = generation["health_result_asset_id"]
        values.add(_id(value, "generation.health_result_asset_id"))
        for member in generation["members"]:
            if not isinstance(member, Mapping):
                raise GenerationBackupError("Generation member is not a mapping")
            value = member.get("data_bundle_asset_id")
            if value is not None:
                values.add(_id(value, "member.data_bundle_asset_id"))
    return tuple(sorted(values, key=lambda value: value.encode("utf-8")))


class GenerationBackupContributor:
    """Read immutable P2 pointers; never repairs, switches, or substitutes them."""

    def __init__(self, generations: GenerationStateSource) -> None:
        self._generations = generations

    def capture_for_backup(
        self,
        *,
        barrier: BackupBarrier,
        core_database: object,
        core_snapshot_hash: str,
        mode: BackupMode,
        workspace_ids: tuple[str, ...],
    ) -> GenerationSnapshot:
        del core_database, workspace_ids
        if not isinstance(barrier, BackupBarrier) or not barrier.token:
            raise GenerationBackupError("backup barrier is absent")
        _hash(core_snapshot_hash, "core_snapshot_hash")
        if mode not in {"full", "data", "workspace"}:
            raise GenerationBackupError("backup mode is not closed")
        if mode == "workspace":
            # Workspace bundles intentionally exclude global P2 pointers,
            # package roots, and Asset closure contributions.
            return GenerationSnapshot(
                barrier_token=barrier.token,
                bound_core_snapshot_hash=core_snapshot_hash,
                current_generation_id=None,
                lkg_generation_id=None,
                compatible=True,
            )
        state = self._generations.generation_state()
        if not isinstance(state, GenerationState):
            raise GenerationBackupError("generation source returned an untyped state")
        current = _generation(state.current, "current")
        lkg = _generation(state.lkg, "lkg")
        selected = (current, lkg)
        return GenerationSnapshot(
            barrier_token=barrier.token,
            bound_core_snapshot_hash=core_snapshot_hash,
            current_generation_id=None if current is None else str(current["generation_id"]),
            lkg_generation_id=None if lkg is None else str(lkg["generation_id"]),
            compatible=True,
            asset_ids=_asset_ids(selected),
            plugin_releases=_release_projection(selected),
        )


PluginGenerationBackupContributor = GenerationBackupContributor
GenerationBackupAdapter = GenerationBackupContributor

__all__ = [
    "GenerationBackupAdapter",
    "GenerationBackupContributor",
    "GenerationBackupError",
    "GenerationStateSource",
    "PluginGenerationBackupContributor",
]
