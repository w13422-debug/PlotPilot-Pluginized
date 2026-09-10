from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.plotpilot_core.api.v1.core import (
    AUTHORITY_ROUTE_ALLOWLIST,
    ROUTE_ALLOWLIST,
    CoreHttpAdapter,
    authority_route_inventory,
    create_core_router,
    decode_query_request,
    route_inventory,
)
from backend.plotpilot_core.assets import AssetReferenceError, AssetStore
from backend.plotpilot_core.candidates import CandidateService
from backend.plotpilot_core.publication import PublicationService
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_core.repositories.authority_application import (
    CoreAuthorityApplication,
)
from backend.plotpilot_plugin_sdk.core_api import CORE_API_MATRIX
from backend.plotpilot_plugin_sdk.errors import ContractValidationError

_REQUEST_ERRORS = {
    "malformed_json": {
        "schema": "core-http-request-error/v1",
        "error_code": "malformed_json",
        "message": "Request body is not valid JSON.",
        "retryable": False,
    },
    "invalid_request": {
        "schema": "core-http-request-error/v1",
        "error_code": "invalid_request",
        "message": "Request does not satisfy its closed schema.",
        "retryable": False,
    },
    "invalid_query": {
        "schema": "core-http-request-error/v1",
        "error_code": "invalid_query",
        "message": "Query parameter syntax is invalid.",
        "retryable": False,
    },
    "range_out_of_bounds": {
        "schema": "core-http-request-error/v1",
        "error_code": "range_out_of_bounds",
        "message": "Asset range offset exceeds total size.",
        "retryable": False,
    },
}
_CORE_HTTP_GOLDEN = json.loads(
    (
        ROOT
        / "contracts"
        / "golden"
        / "contract-publication-v1"
        / "core-http.json"
    ).read_text(encoding="utf-8")
)
_MATRIX_BY_ROUTE = {
    route["route_id"]: route for route in CORE_API_MATRIX["routes"]
}
_SUCCESS_EXCHANGES = {
    exchange["route_id"]: exchange
    for exchange in _CORE_HTTP_GOLDEN["exchanges"]
    if exchange["status"] < 300
}
_AUTHORITY_ROUTE_IDS = tuple(
    route["route_id"]
    for route in CORE_API_MATRIX["routes"]
    if route["request_schema"].startswith("core-")
)
_PAGE_ROUTE_IDS = frozenset(
    {
        "workspace.list",
        "document.list",
        "node.list",
        "relation.list",
        "document.revision.list",
        "node.revision.list",
    }
)
_DIRECT_BAD_ID_FIELD = {
    "workspace.get": "workspace_id",
    "workspace.create": "workspace_id",
    "workspace.update": "workspace_id",
    "document.get": "document_id",
    "document.create": "document_id",
    "document.update": "document_id",
    "node.get": "node_id",
    "node.create": "node_id",
    "node.update": "node_id",
    "relation.create": "relation_id",
    "revision.get": "revision_id",
    "document.revision.create": "document_id",
    "node.revision.create": "node_id",
}


class _RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    @staticmethod
    def request_error(error_code: str) -> tuple[int, dict[str, Any]]:
        raise AssertionError(f"unexpected request error: {error_code}")

    def handle(
        self,
        route_id: str,
        request: dict[str, Any],
        *,
        path_identity: dict[str, str],
    ) -> tuple[int, dict[str, str]]:
        self.calls.append((route_id, request, path_identity))
        return 200, {"route_id": route_id}


def _client_for(adapter: Any) -> TestClient:
    application = FastAPI()
    application.include_router(create_core_router(adapter))
    return TestClient(application)


def _golden_exchange(
    route_id: str,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    exchange = copy.deepcopy(_SUCCESS_EXCHANGES[route_id])
    request = exchange["request"]
    response = exchange["response"]
    if route_id in {"document.revision.create", "node.revision.create"}:
        response["content_hash"] = hashlib.sha256(
            request["content"].encode("utf-8")
        ).hexdigest()
    return request, response, exchange["status"]


def _send_exchange(
    client: TestClient,
    route_id: str,
    request: dict[str, Any],
):
    spec = _MATRIX_BY_ROUTE[route_id]
    path = spec["path_template"]
    for name in spec["path_identity"]:
        path = path.replace("{" + name + "}", quote(request[name], safe=""))
    if spec["method"] == "GET":
        query = [
            (name, str(value))
            for name, value in request.items()
            if name != "schema"
            and name not in spec["path_identity"]
            and value is not None
        ]
        return client.request(spec["method"], path, params=query)
    return client.request(spec["method"], path, json=request)


def _wrong_bound_result(
    route_id: str,
    request: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(response)
    if route_id in _PAGE_ROUTE_IDS:
        result["offset"] = request["offset"] + 1
        end = result["offset"] + len(result["items"])
        result["total"] = max(result["total"], end)
        result["next_offset"] = end if end < result["total"] else None
        return result
    if route_id in _DIRECT_BAD_ID_FIELD:
        result[_DIRECT_BAD_ID_FIELD[route_id]] = "wrong-id"
        return result
    if route_id == "workspace.delete":
        result["operation_key"] = "wrong-operation"
        return result
    if route_id == "node.delete":
        result["entity_id"] = "wrong-node"
        return result
    if route_id == "relation.delete":
        result["workspace_id"] = "wrong-workspace"
        return result
    if route_id == "revision.content":
        result["revision_id"] = "wrong-revision"
        return result
    raise AssertionError(f"missing negative result mutation for {route_id}")


@pytest.fixture
def stack(tmp_path: Path):
    repository = CoreAuthorityRepository(tmp_path / "core.sqlite")
    assets = AssetStore(tmp_path / "assets")
    publication = PublicationService(repository, assets)
    authority = CoreAuthorityApplication(repository, publication)
    adapter = CoreHttpAdapter(authority, publication, assets)
    router = create_core_router(adapter)
    application = FastAPI()
    application.include_router(router)
    try:
        yield {
            "repository": repository,
            "assets": assets,
            "publication": publication,
            "authority": authority,
            "adapter": adapter,
            "router": router,
            "client": TestClient(application),
        }
    finally:
        repository.close()


def _workspace(workspace_id: str = "ws-1", *, operation_key: str = "workspace-create") -> dict[str, object]:
    return {
        "schema": "core-workspace-create-command/v1",
        "operation_key": operation_key,
        "workspace_id": workspace_id,
        "workspace_kind": "Novel",
        "title": "Workspace",
    }


def _document(workspace_id: str = "ws-1", document_id: str = "doc-1") -> dict[str, object]:
    return {
        "schema": "core-document-create-command/v1",
        "operation_key": f"document-create-{document_id}",
        "workspace_id": workspace_id,
        "document_id": document_id,
        "document_type": "Chapter",
        "title": "Chapter One",
    }


def _create_workspace_and_document(client: TestClient, *, workspace_id: str = "ws-1", document_id: str = "doc-1") -> None:
    assert client.post("/api/v1/core/workspaces", json=_workspace(workspace_id)).status_code == 201
    path_id = quote(workspace_id, safe="")
    assert client.post(
        f"/api/v1/core/workspaces/{path_id}/documents",
        json=_document(workspace_id, document_id),
    ).status_code == 201


def test_route_inventory_is_exact_complete_matrix_and_authority_subset(stack) -> None:
    expected = tuple(
        {
            "route_id": value["route_id"],
            "method": value["method"],
            "path": value["path_template"],
            "success_status": value["success_statuses"][0],
        }
        for value in CORE_API_MATRIX["routes"]
    )
    expected_authority = tuple(item for item in expected if item["route_id"] not in {
        "publication.accept",
        "asset.metadata",
        "asset.range",
    })

    assert len(expected) == 26
    assert len(expected_authority) == 23
    assert ROUTE_ALLOWLIST == expected
    assert AUTHORITY_ROUTE_ALLOWLIST == expected_authority
    assert route_inventory(stack["router"]) == expected
    assert authority_route_inventory(stack["router"]) == expected_authority
    assert len(stack["router"].routes) == 26


def test_every_get_query_projection_matches_the_frozen_gateway_shape() -> None:
    fixture = json.loads(
        (ROOT / "contracts" / "golden" / "contract-publication-v1" / "core-http.json").read_text(
            encoding="utf-8"
        )
    )
    examples = {
        value["schema"]: value
        for value in fixture["authority_payloads"]
        if value.get("schema", "").endswith("-query/v1")
        or value.get("schema") in {"asset-query/v1", "asset-read-range-query/v1"}
    }
    examples[fixture["assets"]["query"]["schema"]] = fixture["assets"]["query"]
    examples[fixture["assets"]["range_query"]["schema"]] = fixture["assets"]["range_query"]
    get_routes = [route for route in CORE_API_MATRIX["routes"] if route["method"] == "GET"]
    assert {route["request_schema"] for route in get_routes} == set(examples)

    for route in get_routes:
        example = examples[route["request_schema"]]
        path_identity = {
            name: example[name]
            for name in route["path_identity"]
        }
        query_items = [
            (name, str(value))
            for name, value in example.items()
            if name != "schema" and name not in path_identity and value is not None
        ]
        assert decode_query_request(
            route["route_id"],
            query_items,
            path_identity=path_identity,
        ) == example

def test_home_and_workbench_authority_routes_round_trip_through_http(stack) -> None:
    client = stack["client"]
    _create_workspace_and_document(client)

    listed = client.get("/api/v1/core/workspaces/ws-1/documents?offset=0&limit=100")
    assert listed.status_code == 200
    assert listed.json()["schema"] == "core-document-page/v1"
    assert listed.json()["items"][0]["document_id"] == "doc-1"

    workspace = client.get("/api/v1/core/workspaces/ws-1")
    assert workspace.status_code == 200
    assert workspace.json()["workspace_id"] == "ws-1"

    revision = client.post(
        "/api/v1/core/documents/doc-1/revisions",
        json={
            "schema": "core-document-revision-create-command/v1",
            "operation_key": "revision-create-1",
            "revision_id": "revision-1",
            "workspace_id": "ws-1",
            "document_id": "doc-1",
            "base_revision_id": None,
            "content": "hello workbench",
            "created_by": "editor-1",
            "source_candidate_id": None,
            "payload_schema": "core.document-text/v1",
        },
    )
    assert revision.status_code == 201
    assert revision.json()["revision_id"] == "revision-1"

    content = client.get(
        "/api/v1/core/revisions/revision-1/content?workspace_id=ws-1&offset=0&length=5"
    )
    assert content.status_code == 200
    assert content.json() == {
        "schema": "core-revision-content-page/v1",
        "revision_id": "revision-1",
        "offset": 0,
        "length": 5,
        "total_length": 15,
        "text": "hello",
        "next_offset": 5,
    }


def test_percent_encoded_slash_path_identity_remains_bound(stack) -> None:
    client = stack["client"]
    workspace_id = "ws/one"
    _create_workspace_and_document(client, workspace_id=workspace_id, document_id="doc/slash")
    path_id = quote(workspace_id, safe="")

    listed = client.get(f"/api/v1/core/workspaces/{path_id}/documents?offset=0&limit=100")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["workspace_id"] == workspace_id
    assert listed.json()["items"][0]["document_id"] == "doc/slash"


def test_pre_domain_errors_are_exact_typed_400_without_fastapi_422(stack) -> None:
    client = stack["client"]

    malformed = client.post(
        "/api/v1/core/workspaces",
        content=b'{"schema":',
        headers={"content-type": "application/json"},
    )
    assert (malformed.status_code, malformed.json()) == (400, _REQUEST_ERRORS["malformed_json"])

    unknown = client.post(
        "/api/v1/core/workspaces",
        json={**_workspace(), "unexpected": True},
    )
    assert (unknown.status_code, unknown.json()) == (400, _REQUEST_ERRORS["invalid_request"])

    invalid_query = client.get("/api/v1/core/workspaces?offset=01&limit=100")
    assert (invalid_query.status_code, invalid_query.json()) == (400, _REQUEST_ERRORS["invalid_query"])

    _create_workspace_and_document(client)
    drift = client.post(
        "/api/v1/core/workspaces/ws-1/documents",
        json=_document("ws-other", "doc-other"),
    )
    assert (drift.status_code, drift.json()) == (400, _REQUEST_ERRORS["invalid_request"])


def test_declared_authority_domain_errors_remain_typed(stack) -> None:
    client = stack["client"]
    _create_workspace_and_document(client)

    missing = client.get("/api/v1/core/documents/document-missing?workspace_id=ws-1")
    assert missing.status_code == 404
    assert missing.json()["schema"] == "core-http-error/v1"
    assert missing.json()["error_code"] == "unknown_reference"

    stale = client.patch(
        "/api/v1/core/workspaces/ws-1",
        json={
            "schema": "core-workspace-update-command/v1",
            "operation_key": "workspace-stale",
            "workspace_id": "ws-1",
            "expected_revision": 99,
            "title": "Stale",
            "status": None,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["schema"] == "core-http-error/v1"
    assert stale.json()["error_code"] == "stale_cas"


def test_asset_metadata_and_range_use_the_single_immutable_store(stack) -> None:
    client = stack["client"]
    metadata = stack["assets"].put(
        b"hello",
        mime="text/plain",
        logical_role="revision.content",
        provenance="test:asset-http",
    )
    asset_id = quote(metadata.asset_id, safe="")

    described = client.get(f"/api/v1/core/assets/{asset_id}")
    assert described.status_code == 200
    assert described.json()["schema"] == "asset-metadata/v1"
    assert described.json()["sha256"] == metadata.sha256

    at_end = client.get(f"/api/v1/core/assets/{asset_id}/range?offset=5&length=16")
    assert at_end.status_code == 200
    assert at_end.json() == {
        "schema": "asset-read-range/v1",
        "asset_id": metadata.asset_id,
        "offset": 5,
        "length": 0,
        "total_size": 5,
        "base64_chunk": "",
        "next_offset": None,
        "content_hash": hashlib.sha256(b"").hexdigest(),
    }

    beyond_end = client.get(f"/api/v1/core/assets/{asset_id}/range?offset=6&length=1")
    assert (beyond_end.status_code, beyond_end.json()) == (400, _REQUEST_ERRORS["range_out_of_bounds"])
    assert stack["adapter"].publication.service is stack["publication"]
    assert stack["adapter"].assets is stack["assets"]


def test_publication_route_uses_existing_authority_application(stack) -> None:
    client = stack["client"]
    response = client.post(
        "/api/v1/core/publications/accept",
        json={
            "schema": "publication-command/v1",
            "publication_operation_key": "publication-unknown",
            "workspace_id": "ws-1",
            "candidate_id": "candidate-missing",
            "accepted_by": "editor-1",
        },
    )
    assert response.status_code == 404
    assert response.json()["schema"] == "core-http-error/v1"
    assert response.json()["error_code"] == "unknown_reference"

def test_publication_accept_round_trips_through_the_existing_service(stack) -> None:
    client = stack["client"]
    _create_workspace_and_document(client)
    base_response = client.post(
        "/api/v1/core/documents/doc-1/revisions",
        json={
            "schema": "core-document-revision-create-command/v1",
            "operation_key": "publication-base-revision",
            "revision_id": "publication-base",
            "workspace_id": "ws-1",
            "document_id": "doc-1",
            "base_revision_id": None,
            "content": "base",
            "created_by": "editor-1",
            "source_candidate_id": None,
            "payload_schema": "core.document-text/v1",
        },
    )
    assert base_response.status_code == 201
    base = base_response.json()
    payload = stack["assets"].put(
        b"published text",
        mime="text/plain",
        logical_role="candidate_payload",
        provenance="test:publication-http",
    )
    item = {
        "schema": "candidate-item/v1",
        "item_id": "publication-item",
        "item_kind": "document",
        "target": {
            "workspace_id": "ws-1",
            "entity_kind": "document",
            "entity_id": "doc-1",
        },
        "mutation": {
            "mode": "replace",
            "payload_schema": "core/document-text/v1",
            "payload_hash": payload.sha256,
        },
        "payload_asset_id": payload.asset_id,
        "base": {
            "revision_id": base["revision_id"],
            "content_hash": base["content_hash"],
        },
        "write_set": [
            {
                "workspace_id": "ws-1",
                "entity_kind": "document",
                "entity_id": "doc-1",
                "revision_id": base["revision_id"],
                "content_hash": base["content_hash"],
            }
        ],
        "parent_candidate_ids": [],
        "source_refs": [],
        "status": "complete",
    }
    staged = CandidateService(stack["repository"], stack["assets"]).stage("publication-stage", item)

    accepted = client.post(
        "/api/v1/core/publications/accept",
        json={
            "schema": "publication-command/v1",
            "publication_operation_key": "publication-accept",
            "workspace_id": "ws-1",
            "candidate_id": staged.candidate_id,
            "accepted_by": "editor-1",
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["schema"] == "publication-result/v1"
    assert accepted.json()["candidate_id"] == staged.candidate_id
    assert accepted.json()["idempotent"] is False

def test_asset_object_failure_after_metadata_is_not_disguised(stack) -> None:
    metadata = stack["assets"].put(
        b"intact",
        mime="text/plain",
        logical_role="revision.content",
        provenance="test:asset-missing-object",
    )
    (stack["assets"].objects / metadata.sha256[:2] / metadata.sha256).unlink()
    with pytest.raises(FileNotFoundError):
        stack["adapter"].handle(
            "asset.range",
            {
                "schema": "asset-read-range-query/v1",
                "asset_id": metadata.asset_id,
                "offset": 0,
                "length": 6,
            },
            path_identity={"asset_id": metadata.asset_id},
        )


@pytest.mark.parametrize(
    ("route_id", "path", "identity", "query"),
    [
        ("workspace.get", "/api/v1/core/workspaces/ws%2Fdocuments", "ws/documents", {}),
        ("workspace.get", "/api/v1/core/workspaces/ws%2Fnodes", "ws/nodes", {}),
        ("workspace.get", "/api/v1/core/workspaces/ws%2Frelations", "ws/relations", {}),
        (
            "document.get",
            "/api/v1/core/documents/doc%2Frevisions",
            "doc/revisions",
            {"workspace_id": "ws-1"},
        ),
        (
            "node.get",
            "/api/v1/core/nodes/node%2Frevisions",
            "node/revisions",
            {"workspace_id": "ws-1"},
        ),
        (
            "revision.get",
            "/api/v1/core/revisions/rev%2Fcontent",
            "rev/content",
            {"workspace_id": "ws-1"},
        ),
        ("asset.metadata", "/api/v1/core/assets/asset%2Frange", "asset/range", {}),
    ],
)
def test_all_encoded_slash_suffix_identities_reach_the_parent_route(
    route_id: str,
    path: str,
    identity: str,
    query: dict[str, str],
) -> None:
    adapter = _RecordingAdapter()
    response = _client_for(adapter).get(path, params=query)

    assert response.status_code == 200
    assert response.json() == {"route_id": route_id}
    assert adapter.calls[-1][0] == route_id
    identity_name = _MATRIX_BY_ROUTE[route_id]["path_identity"][0]
    assert adapter.calls[-1][2] == {identity_name: identity}
    assert adapter.calls[-1][1][identity_name] == identity


@pytest.mark.parametrize(
    ("route_id", "path", "query"),
    [
        (
            "document.list",
            "/api/v1/core/workspaces/ws/documents",
            {"offset": "0", "limit": "1"},
        ),
        (
            "node.list",
            "/api/v1/core/workspaces/ws/nodes",
            {"offset": "0", "limit": "1"},
        ),
        (
            "relation.list",
            "/api/v1/core/workspaces/ws/relations",
            {"offset": "0", "limit": "1"},
        ),
        (
            "document.revision.list",
            "/api/v1/core/documents/doc/revisions",
            {"workspace_id": "ws", "offset": "0", "limit": "1"},
        ),
        (
            "node.revision.list",
            "/api/v1/core/nodes/node/revisions",
            {"workspace_id": "ws", "offset": "0", "limit": "1"},
        ),
        (
            "revision.content",
            "/api/v1/core/revisions/rev/content",
            {"workspace_id": "ws", "offset": "0", "length": "1"},
        ),
        (
            "asset.range",
            "/api/v1/core/assets/asset/range",
            {"offset": "0", "length": "1"},
        ),
    ],
)
def test_structural_child_routes_remain_reachable(
    route_id: str,
    path: str,
    query: dict[str, str],
) -> None:
    adapter = _RecordingAdapter()
    response = _client_for(adapter).get(path, params=query)

    assert response.status_code == 200
    assert response.json() == {"route_id": route_id}
    assert adapter.calls[-1][0] == route_id


@pytest.mark.parametrize("route_id", _AUTHORITY_ROUTE_IDS)
def test_every_authority_route_rejects_a_schema_valid_unbound_result(
    stack,
    monkeypatch: pytest.MonkeyPatch,
    route_id: str,
) -> None:
    request, response, expected_status = _golden_exchange(route_id)
    current = {"response": response}

    def injected(actual_route: str, _request: dict[str, Any]) -> dict[str, Any]:
        assert actual_route == route_id
        return copy.deepcopy(current["response"])

    monkeypatch.setattr(stack["authority"], "handle", injected)
    accepted = _send_exchange(stack["client"], route_id, request)
    assert accepted.status_code == expected_status

    current["response"] = _wrong_bound_result(route_id, request, response)
    with pytest.raises(ContractValidationError):
        _send_exchange(stack["client"], route_id, request)


@pytest.mark.parametrize("route_id", sorted(_PAGE_ROUTE_IDS))
def test_authority_page_limit_is_request_bound(
    stack,
    monkeypatch: pytest.MonkeyPatch,
    route_id: str,
) -> None:
    request, response, _ = _golden_exchange(route_id)
    response["limit"] = request["limit"] + 1
    monkeypatch.setattr(
        stack["authority"],
        "handle",
        lambda _route, _request: copy.deepcopy(response),
    )

    with pytest.raises(ContractValidationError):
        _send_exchange(stack["client"], route_id, request)


@pytest.mark.parametrize(
    ("route_id", "field", "filter_value"),
    [
        ("workspace.list", "workspace_id", "ws-filter"),
        ("document.list", "document_id", "doc-filter"),
        ("node.list", "node_id", "node-filter"),
        ("node.list", "document_id", "doc-filter"),
        ("node.list", "parent_node_id", "parent-filter"),
        ("relation.list", "relation_id", "relation-filter"),
        ("relation.list", "source_id", "source-filter"),
        ("relation.list", "target_id", "target-filter"),
        ("relation.list", "relation_type", "type-filter"),
        ("document.revision.list", "document_id", "doc-filter"),
        ("document.revision.list", "revision_id", "revision-filter"),
        ("node.revision.list", "node_id", "node-filter"),
        ("node.revision.list", "revision_id", "revision-filter"),
    ],
)
def test_authority_page_filters_and_revision_targets_are_request_bound(
    stack,
    monkeypatch: pytest.MonkeyPatch,
    route_id: str,
    field: str,
    filter_value: str,
) -> None:
    request, response, expected_status = _golden_exchange(route_id)
    request[field] = filter_value
    response["items"][0][field] = filter_value
    current = {"response": response}
    monkeypatch.setattr(
        stack["authority"],
        "handle",
        lambda _route, _request: copy.deepcopy(current["response"]),
    )

    accepted = _send_exchange(stack["client"], route_id, request)
    assert accepted.status_code == expected_status
    current["response"]["items"][0][field] = "wrong-filter"
    with pytest.raises(ContractValidationError):
        _send_exchange(stack["client"], route_id, request)


@pytest.mark.parametrize(
    ("route_id", "field"),
    [
        (route_id, field)
        for route_id in ("workspace.delete", "node.delete", "relation.delete")
        for field in ("operation_key", "workspace_id", "entity_id", "previous_revision")
    ],
)
def test_delete_results_bind_operation_entity_workspace_and_revision(
    stack,
    monkeypatch: pytest.MonkeyPatch,
    route_id: str,
    field: str,
) -> None:
    request, response, _ = _golden_exchange(route_id)
    if field == "previous_revision":
        response[field] = 0 if response[field] is None else response[field] + 1
    else:
        response[field] = "wrong-value"
    monkeypatch.setattr(
        stack["authority"],
        "handle",
        lambda _route, _request: copy.deepcopy(response),
    )

    with pytest.raises(ContractValidationError):
        _send_exchange(stack["client"], route_id, request)


def test_revision_content_identity_offset_and_length_are_request_bound(
    stack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_id = "revision.content"
    request, response, expected_status = _golden_exchange(route_id)
    current = {"response": response}
    monkeypatch.setattr(
        stack["authority"],
        "handle",
        lambda _route, _request: copy.deepcopy(current["response"]),
    )
    assert _send_exchange(stack["client"], route_id, request).status_code == expected_status

    wrong_offset = copy.deepcopy(response)
    wrong_offset["offset"] = 1
    wrong_offset["total_length"] = 6
    current["response"] = wrong_offset
    with pytest.raises(ContractValidationError):
        _send_exchange(stack["client"], route_id, request)

    overlong = copy.deepcopy(response)
    overlong.update(
        {
            "length": request["length"] + 1,
            "total_length": request["length"] + 1,
            "text": response["text"] + "!",
        }
    )
    current["response"] = overlong
    with pytest.raises(ContractValidationError):
        _send_exchange(stack["client"], route_id, request)


def test_adapter_rejects_parallel_publication_service_and_asset_store(tmp_path: Path) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.sqlite")
    authority_assets = AssetStore(tmp_path / "authority-assets")
    parallel_assets = AssetStore(tmp_path / "parallel-assets")
    authority_service = PublicationService(repository, authority_assets)
    parallel_service = PublicationService(repository, parallel_assets)
    authority = CoreAuthorityApplication(repository, authority_service)
    try:
        adapter = CoreHttpAdapter(authority)
        assert adapter.publication_service is authority_service
        assert adapter.assets is authority_assets

        with pytest.raises(ValueError, match="Authority PublicationService"):
            CoreHttpAdapter(authority, parallel_service, parallel_assets)
        with pytest.raises(ValueError, match="Authority PublicationService AssetStore"):
            CoreHttpAdapter(authority, authority_service, parallel_assets)

        unconfigured = CoreAuthorityApplication(repository)
        with pytest.raises(ValueError, match="requires the existing PublicationService"):
            CoreHttpAdapter(unconfigured, authority_service, authority_assets)
    finally:
        repository.close()


def test_invalid_and_unknown_asset_references_are_declared_404(stack) -> None:
    client = stack["client"]
    unknown = "asset-sha256-" + ("0" * 64)
    for asset_id in ("asset-a", unknown):
        encoded = quote(asset_id, safe="")
        metadata = client.get(f"/api/v1/core/assets/{encoded}")
        asset_range = client.get(
            f"/api/v1/core/assets/{encoded}/range?offset=0&length=1"
        )
        assert (metadata.status_code, metadata.json()["error_code"]) == (
            404,
            "unknown_reference",
        )
        assert (asset_range.status_code, asset_range.json()["error_code"]) == (
            404,
            "unknown_reference",
        )


def test_asset_metadata_corruption_is_not_disguised_as_unknown_reference(stack) -> None:
    metadata = stack["assets"].put(
        b"intact",
        mime="text/plain",
        logical_role="revision.content",
        provenance="test:asset-corrupt-metadata",
    )
    metadata_path = stack["assets"].metadata / f"{metadata.sha256}.json"
    metadata_path.write_text("{", encoding="utf-8")
    encoded = quote(metadata.asset_id, safe="")

    with pytest.raises(json.JSONDecodeError):
        stack["client"].get(f"/api/v1/core/assets/{encoded}")


def test_integer_digit_guards_map_to_frozen_parser_errors(stack) -> None:
    over_digit = "9" * 5000
    query = stack["client"].get(
        "/api/v1/core/workspaces",
        params={"offset": over_digit, "limit": "100"},
    )
    assert (query.status_code, query.json()) == (400, _REQUEST_ERRORS["invalid_query"])

    raw = (
        '{"schema":"core-workspace-update-command/v1",'
        '"operation_key":"over-digit-json","workspace_id":"ws-1",'
        f'"expected_revision":{over_digit},"title":"Title","status":null}}'
    ).encode()
    body = stack["client"].patch(
        "/api/v1/core/workspaces/ws-1",
        content=raw,
        headers={"content-type": "application/json"},
    )
    assert (body.status_code, body.json()) == (400, _REQUEST_ERRORS["malformed_json"])


@pytest.mark.parametrize("offset", [1 << 63, 10**100])
def test_legal_offsets_above_sqlite_signed_64_return_contract_valid_empty_pages(
    stack,
    offset: int,
) -> None:
    response = stack["client"].get(
        "/api/v1/core/workspaces",
        params={"offset": str(offset), "limit": "100"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "schema": "core-workspace-page/v1",
        "items": [],
        "offset": offset,
        "limit": 100,
        "total": 0,
        "next_offset": None,
    }


def test_negative_and_overflow_asset_ranges_follow_the_frozen_contract(stack) -> None:
    metadata = stack["assets"].put(
        b"range",
        mime="text/plain",
        logical_role="revision.content",
        provenance="test:asset-range-boundaries",
    )
    encoded = quote(metadata.asset_id, safe="")

    negative = stack["client"].get(
        f"/api/v1/core/assets/{encoded}/range?offset=-1&length=1"
    )
    huge_offset = stack["client"].get(
        f"/api/v1/core/assets/{encoded}/range?offset={1 << 63}&length=1"
    )
    huge_length = stack["client"].get(
        f"/api/v1/core/assets/{encoded}/range?offset=0&length=1048577"
    )

    assert (negative.status_code, negative.json()) == (
        400,
        _REQUEST_ERRORS["invalid_request"],
    )
    assert (huge_offset.status_code, huge_offset.json()) == (
        400,
        _REQUEST_ERRORS["range_out_of_bounds"],
    )
    assert (huge_length.status_code, huge_length.json()) == (
        400,
        _REQUEST_ERRORS["invalid_request"],
    )


def test_asset_reference_error_type_is_narrow() -> None:
    assert issubclass(AssetReferenceError, LookupError)
    assert not issubclass(AssetReferenceError, ValueError)
