from __future__ import annotations

import dataclasses

import pytest
from helpers import StatusSource, make_app, package_files
from plotpilot_core.plugins.store import PackageStore

ERROR_KEYS = {"schema", "error_code", "message", "retryable"}


def test_release_list_and_exact_lookup_use_real_packagestore(
    package_store: PackageStore,
) -> None:
    second = package_store.publish(
        package_files(
            plugin_id="com.plotpilot.api.zulu",
            version="2.1.0",
            display_name="Zulu",
        )
    )
    first = package_store.publish(package_files())
    _, client = make_app(package_store, StatusSource())

    listing = client.get("/api/v1/plugins/releases")
    assert listing.status_code == 200
    assert listing.json()["schema"] == "plugin-release-list/v1"
    releases = listing.json()["releases"]
    assert [(item["plugin_id"], item["version"]) for item in releases] == [
        (first.plugin_id, first.version),
        (second.plugin_id, second.version),
    ]
    assert all(set(item) == {
        "schema",
        "plugin_id",
        "version",
        "kind",
        "package_hash",
        "release_id",
        "manifest",
    } for item in releases)

    exact = client.get(
        f"/api/v1/plugins/releases/{first.plugin_id}/{first.version}",
        params={"package_hash": first.package_hash},
    )
    assert exact.status_code == 200
    assert exact.json()["release_id"] == first.release_id
    assert exact.json()["manifest"]["plugin_id"] == first.plugin_id


def test_real_packagestore_missing_release_is_typed_404(
    package_store: PackageStore,
) -> None:
    _, client = make_app(package_store, StatusSource())

    response = client.get(
        "/api/v1/plugins/releases/com.plotpilot.missing/1.0.0"
    )

    assert response.status_code == 404
    assert response.json() == {
        "schema": "plugin-api-local-error/v1",
        "error_code": "release_not_found",
        "message": "the requested plugin release does not exist",
        "retryable": False,
    }


@pytest.mark.parametrize("corruption", ["registry", "manifest", "content"])
def test_corrupt_real_packagestore_is_never_disguised_as_404(
    package_store: PackageStore, corruption: str
) -> None:
    package = package_store.publish(package_files())
    if corruption == "registry":
        package_store.registry_path.write_text("{broken", encoding="utf-8")
    elif corruption == "manifest":
        path = package_store.package_path(*package.identity) / "plugin.json"
        path.chmod(0o600)
        path.write_bytes(b"{}")
    else:
        path = package_store.package_path(*package.identity) / "data" / "rules.json"
        path.chmod(0o600)
        path.unlink()
    _, client = make_app(package_store, StatusSource())

    response = client.get(
        f"/api/v1/plugins/releases/{package.plugin_id}/{package.version}"
    )

    assert response.status_code == 500
    assert response.status_code != 404
    assert set(response.json()) == ERROR_KEYS
    assert response.json()["error_code"] == "package_store_error"


def test_wrong_registered_hash_is_typed_conflict_not_not_found(
    package_store: PackageStore,
) -> None:
    package = package_store.publish(package_files())
    _, client = make_app(package_store, StatusSource())

    response = client.get(
        f"/api/v1/plugins/releases/{package.plugin_id}/{package.version}",
        params={"package_hash": "b" * 64},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "release_conflict"


class IncompleteFileStore:
    def get(self, plugin_id: str, version: str, package_hash: str | None = None):
        raise FileNotFoundError("plugin.json")

    def list_releases(self, *, verify: bool = True):
        return ()


def test_internal_filenotfound_shape_is_typed_500_not_404() -> None:
    _, client = make_app(IncompleteFileStore(), StatusSource())

    response = client.get("/api/v1/plugins/releases/com.plotpilot.echo/1.0.0")

    assert response.status_code == 500
    assert response.json()["error_code"] == "package_store_error"


class GuardStore:
    calls = 0

    def get(self, plugin_id: str, version: str, package_hash: str | None = None):
        self.calls += 1
        raise AssertionError("validation must run before authority access")

    def list_releases(self, *, verify: bool = True):
        self.calls += 1
        raise AssertionError("not used")


@pytest.mark.parametrize(
    ("path", "query", "code"),
    [
        ("bad!id/1.0.0", "", "invalid_identifier"),
        ("com.plotpilot.echo/not-semver", "", "invalid_semver"),
        (
            "com.plotpilot.echo/1.0.0",
            "?package_hash=" + "A" * 64,
            "invalid_sha256",
        ),
        (
            "com.plotpilot.echo/1.0.0",
            "?package_hash=abcd",
            "invalid_sha256",
        ),
    ],
)
def test_invalid_transport_identity_is_typed_422_before_authority(
    path: str, query: str, code: str
) -> None:
    store = GuardStore()
    _, client = make_app(store, StatusSource())

    response = client.get(f"/api/v1/plugins/releases/{path}{query}")

    assert response.status_code == 422
    assert response.json()["error_code"] == code
    assert set(response.json()) == ERROR_KEYS
    assert store.calls == 0


class DriftStore:
    def __init__(self, package: object) -> None:
        self.package = package

    def get(self, plugin_id: str, version: str, package_hash: str | None = None):
        return self.package

    def list_releases(self, *, verify: bool = True):
        return (self.package,)


def test_manifest_authority_drift_is_typed_502(package_store: PackageStore) -> None:
    package = package_store.publish(package_files())
    manifest = dict(package.manifest)
    manifest["unexpected"] = True
    drifted = dataclasses.replace(package, manifest=manifest)
    _, client = make_app(DriftStore(drifted), StatusSource())

    response = client.get(
        f"/api/v1/plugins/releases/{package.plugin_id}/{package.version}"
    )

    assert response.status_code == 502
    assert response.json()["error_code"] == "authority_contract_error"
    assert set(response.json()) == ERROR_KEYS
