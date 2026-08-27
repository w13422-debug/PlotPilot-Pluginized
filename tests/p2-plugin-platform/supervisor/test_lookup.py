from __future__ import annotations

import json
from dataclasses import replace

import pytest
from conftest import RELEASE_B, UI_BYTES, UI_HASH
from plotpilot_core.supervisor.lookup import ImmutableRuntimeLookup
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode


def test_immutable_worker_and_ui_lookup_bind_all_identities(harness) -> None:
    owner = "lookup-owner-1"
    assert harness.authority.claim("worker-1", owner) == harness.fence
    lookup = ImmutableRuntimeLookup(harness.routes, harness.packages, harness.authority)
    worker = lookup.resolve_worker(harness.fence, owner)
    assert worker.route.entrypoint == "plugin_worker:main"
    assert worker.python_executable == harness.route.python_executable.resolve()

    ui = lookup.resolve_ui_bundle(harness.fence, owner)
    assert ui.content == UI_BYTES
    assert ui.bundle_hash == UI_HASH
    assert ui.route_path == f"/__plotpilot/plugin-worker/{harness.fence.release_id}/{UI_HASH}/worker.js"
    assert ui.content_security_policy == (
        "default-src 'none'; connect-src 'none'; script-src 'none'; "
        "worker-src 'none'; object-src 'none'; base-uri 'none'"
    )


def test_immutable_lookup_rejects_stale_release_and_bundle_tamper(harness) -> None:
    owner = "lookup-owner-2"
    assert harness.authority.claim("worker-1", owner) == harness.fence
    lookup = ImmutableRuntimeLookup(harness.routes, harness.packages, harness.authority)
    stale = replace(harness.fence, release_id=RELEASE_B)
    with pytest.raises(ContractError) as caught:
        lookup.resolve_worker(stale, owner)
    assert caught.value.code == ErrorCode.RELEASE_RETIRING

    harness.packages.package.files["ui/worker.js"] = b"tampered"
    with pytest.raises(ContractError) as caught:
        lookup.resolve_ui_bundle(harness.fence, owner)
    assert caught.value.code == ErrorCode.ASSET_ERROR


def test_immutable_lookup_requires_current_retire_epoch(harness) -> None:
    owner = "lookup-owner-3"
    assert harness.authority.claim("worker-1", owner) == harness.fence
    lookup = ImmutableRuntimeLookup(harness.routes, harness.packages, harness.authority)
    future = replace(harness.fence, retire_epoch=2)
    with pytest.raises(ContractError):
        lookup.resolve_worker(future, owner)


def test_lookup_rejects_retiring_live_route_and_missing_venv_marker(harness) -> None:
    owner = "lookup-owner-4"
    assert harness.authority.claim("worker-1", owner) == harness.fence
    lookup = ImmutableRuntimeLookup(harness.routes, harness.packages, harness.authority)
    harness.authority.values["worker-1"] = replace(harness.fence, allowed=False)
    with pytest.raises(ContractError) as caught:
        lookup.resolve_ui_bundle(harness.fence, owner)
    assert caught.value.code == ErrorCode.RELEASE_RETIRING

    harness.authority.values["worker-1"] = harness.fence
    (harness.route.venv_root / "plotpilot-venv.json").unlink()
    with pytest.raises(ContractError) as caught:
        lookup.resolve_worker(harness.fence, owner)
    assert caught.value.code == ErrorCode.INCOMPATIBLE_GENERATION


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("release_id", "d" * 64),
        ("package_hash", "e" * 64),
        ("python_version", "3.11.9"),
    ],
)
def test_worker_lookup_rejects_old_venv_marker_identity(harness, field: str, value: str) -> None:
    owner = "lookup-marker-owner"
    assert harness.authority.claim("worker-1", owner) == harness.fence
    marker_path = harness.route.venv_root / "plotpilot-venv.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker[field] = value
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    lookup = ImmutableRuntimeLookup(harness.routes, harness.packages, harness.authority)
    with pytest.raises(ContractError) as caught:
        lookup.resolve_worker(harness.fence, owner)
    assert caught.value.code == ErrorCode.INCOMPATIBLE_GENERATION


def test_worker_lookup_rejects_non_content_addressed_venv_root(harness) -> None:
    owner = "lookup-path-owner"
    assert harness.authority.claim("worker-1", owner) == harness.fence
    bad_route = replace(harness.route, venv_root=harness.route.venv_root.parent / "mutable")
    harness.routes.values[(harness.fence.worker_id, harness.fence.generation_id, harness.fence.release_id, 1)] = bad_route
    lookup = ImmutableRuntimeLookup(harness.routes, harness.packages, harness.authority)
    with pytest.raises(ContractError):
        lookup.resolve_worker(harness.fence, owner)


def test_ui_lookup_requires_exact_owner_and_continuously_live_pin(harness) -> None:
    owner = "lookup-live-owner"
    assert harness.authority.claim("worker-1", owner) == harness.fence
    lookup = ImmutableRuntimeLookup(harness.routes, harness.packages, harness.authority)
    with pytest.raises(ContractError) as caught:
        lookup.resolve_ui_bundle(harness.fence, "wrong-owner")
    assert caught.value.code == ErrorCode.RELEASE_RETIRING

    assert lookup.resolve_ui_bundle(harness.fence, owner).content == UI_BYTES
    assert harness.authority.release(harness.fence, owner)
    # The immutable route and package remain present; the live pin alone was
    # removed, so a late lookup must fail rather than serve retained bytes.
    with pytest.raises(ContractError) as caught:
        lookup.resolve_ui_bundle(harness.fence, owner)
    assert caught.value.code == ErrorCode.RELEASE_RETIRING


def test_replacement_authority_rejects_old_fence_even_when_old_route_remains(harness) -> None:
    old_owner = "lookup-old-owner"
    new_owner = "lookup-new-owner"
    assert harness.authority.claim("worker-1", old_owner) == harness.fence
    new_fence = replace(harness.fence, pin_id="pin-worker-2", release_id=RELEASE_B, pin_epoch=2, retire_epoch=2)
    harness.authority.values["worker-1"] = new_fence
    assert harness.authority.claim("worker-1", new_owner) == new_fence
    lookup = ImmutableRuntimeLookup(harness.routes, harness.packages, harness.authority)
    with pytest.raises(ContractError) as caught:
        lookup.resolve_ui_bundle(harness.fence, old_owner)
    assert caught.value.code == ErrorCode.RELEASE_RETIRING
