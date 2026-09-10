"""Transport-independent adapter for the frozen Core v1 HTTP matrix.

This module owns no repository, transaction, or Asset store.  It validates the
closed v1 request/result contracts and delegates all durable authority work to
the already-established Core application services.
"""
from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from backend.plotpilot_plugin_sdk.core_api import (
    CORE_API_MATRIX,
    parse_asset_contract,
    parse_core_authority,
    parse_core_http_request_error,
    parse_publication,
)
from backend.plotpilot_plugin_sdk.errors import ContractValidationError

from ....assets import AssetReferenceError, AssetStore
from ....publication.application import PublicationApplication
from ....publication.service import PublicationService
from ....repositories.authority_application import CoreAuthorityApplication
from ....repositories.authority_application.errors import (
    AuthorityApplicationError,
    UnknownReferenceError,
)

_REQUEST_ERROR_MESSAGES: Final[dict[str, str]] = {
    "malformed_json": "Request body is not valid JSON.",
    "invalid_request": "Request does not satisfy its closed schema.",
    "invalid_query": "Query parameter syntax is invalid.",
    "range_out_of_bounds": "Asset range offset exceeds total size.",
}
_INTEGER_QUERY = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_SQLITE_MAX_OFFSET: Final = (1 << 63) - 1

_RESULT_ENTITY_KINDS: Final[dict[str, str]] = {
    "core-workspace/v1": "workspace",
    "core-document/v1": "document",
    "core-node/v1": "node",
    "core-relation/v1": "relation",
    "core-revision/v1": "revision",
    "core-workspace-page/v1": "workspace",
    "core-document-page/v1": "document",
    "core-node-page/v1": "node",
    "core-relation-page/v1": "relation",
    "core-revision-page/v1": "revision",
    "core-revision-content-page/v1": "revision",
    "asset-metadata/v1": "asset",
    "asset-read-range/v1": "asset",
}


@dataclass(frozen=True, slots=True)
class CoreRouteSpec:
    """One normalized, immutable entry from the frozen Core v1 matrix."""

    route_id: str
    method: str
    path_template: str
    request_schema: str
    result_schema: str
    success_status: int
    path_identity: tuple[str, ...]
    result_entity_kind: str | None
    failure_statuses: tuple[tuple[int, frozenset[str]], ...]

    @property
    def is_authority(self) -> bool:
        return self.request_schema.startswith("core-")

    @property
    def is_publication(self) -> bool:
        return self.request_schema == "publication-command/v1"

    @property
    def is_asset(self) -> bool:
        return self.request_schema.startswith("asset-")


def _route_spec(value: Mapping[str, Any]) -> CoreRouteSpec:
    successes = value.get("success_statuses")
    if not isinstance(successes, list) or len(successes) != 1 or not isinstance(successes[0], int):
        raise RuntimeError("frozen Core v1 route must have exactly one integer success status")
    route_id = value.get("route_id")
    method = value.get("method")
    path_template = value.get("path_template")
    request_schema = value.get("request_schema")
    result_schema = value.get("result_schema")
    path_identity = value.get("path_identity")
    if not all(isinstance(item, str) for item in (route_id, method, path_template, request_schema, result_schema)):
        raise RuntimeError("frozen Core v1 route contains a non-string identity")
    if not isinstance(path_identity, list) or not all(isinstance(item, str) for item in path_identity):
        raise RuntimeError("frozen Core v1 route path identity is invalid")
    placeholders = tuple(_PLACEHOLDER.findall(path_template))
    if tuple(path_identity) != placeholders:
        raise RuntimeError(f"frozen Core v1 route path identity drifted: {route_id}")
    failures: list[tuple[int, frozenset[str]]] = []
    for failure in value.get("failure_statuses", []):
        if not isinstance(failure, Mapping):
            raise TypeError(f"frozen Core v1 route failure entry is invalid: {route_id}")
        status = failure.get("status")
        codes = failure.get("error_codes")
        if not isinstance(status, int) or not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
            raise RuntimeError(f"frozen Core v1 route failure binding is invalid: {route_id}")
        failures.append((status, frozenset(codes)))
    result_entity_kind = value.get("result_entity_kind")
    if result_entity_kind is not None and not isinstance(result_entity_kind, str):
        raise RuntimeError(f"frozen Core v1 result entity kind is invalid: {route_id}")
    return CoreRouteSpec(
        route_id=route_id,
        method=method,
        path_template=path_template,
        request_schema=request_schema,
        result_schema=result_schema,
        success_status=successes[0],
        path_identity=tuple(path_identity),
        result_entity_kind=result_entity_kind,
        failure_statuses=tuple(failures),
    )


if CORE_API_MATRIX.get("schema") != "core-api-method-matrix/v1":
    raise RuntimeError("unexpected Core API method matrix schema")
CORE_ROUTE_SPECS: Final[tuple[CoreRouteSpec, ...]] = tuple(
    _route_spec(item) for item in CORE_API_MATRIX["routes"]
)
if len(CORE_ROUTE_SPECS) != 26 or len({spec.route_id for spec in CORE_ROUTE_SPECS}) != 26:
    raise RuntimeError("frozen Core v1 matrix must contain 26 unique routes")
AUTHORITY_ROUTE_SPECS: Final[tuple[CoreRouteSpec, ...]] = tuple(
    spec for spec in CORE_ROUTE_SPECS if spec.is_authority
)
if len(AUTHORITY_ROUTE_SPECS) != 23:
    raise RuntimeError("frozen Core v1 matrix must contain 23 authority routes")
if tuple(spec.route_id for spec in CORE_ROUTE_SPECS[23:]) != (
    "publication.accept",
    "asset.metadata",
    "asset.range",
):
    raise RuntimeError("frozen Core v1 supplemental route order drifted")
ROUTE_BY_ID: Final[dict[str, CoreRouteSpec]] = {
    spec.route_id: spec for spec in CORE_ROUTE_SPECS
}

_PAGE_ITEM_BINDINGS: Final[dict[str, tuple[str, ...]]] = {
    "workspace.list": ("workspace_id",),
    "document.list": ("workspace_id", "document_id"),
    "node.list": ("workspace_id", "node_id", "document_id", "parent_node_id"),
    "relation.list": (
        "workspace_id",
        "relation_id",
        "source_id",
        "target_id",
        "relation_type",
    ),
    "document.revision.list": ("workspace_id", "document_id", "revision_id"),
    "node.revision.list": ("workspace_id", "node_id", "revision_id"),
}
_PAGE_NULL_BINDINGS: Final[dict[str, tuple[str, ...]]] = {
    "document.revision.list": ("node_id",),
    "node.revision.list": ("document_id",),
}
_DIRECT_RESULT_BINDINGS: Final[
    dict[str, tuple[tuple[str, str], ...]]
] = {
    "workspace.get": (("workspace_id", "workspace_id"),),
    "workspace.create": (
        ("workspace_id", "workspace_id"),
        ("workspace_kind", "workspace_kind"),
        ("title", "title"),
    ),
    "workspace.update": (("workspace_id", "workspace_id"),),
    "document.get": (
        ("workspace_id", "workspace_id"),
        ("document_id", "document_id"),
    ),
    "document.create": (
        ("workspace_id", "workspace_id"),
        ("document_id", "document_id"),
        ("document_type", "document_type"),
        ("title", "title"),
    ),
    "document.update": (
        ("workspace_id", "workspace_id"),
        ("document_id", "document_id"),
        ("title", "title"),
    ),
    "node.get": (
        ("workspace_id", "workspace_id"),
        ("node_id", "node_id"),
    ),
    "node.create": (
        ("workspace_id", "workspace_id"),
        ("node_id", "node_id"),
        ("document_id", "document_id"),
        ("node_type", "node_type"),
        ("title", "title"),
        ("parent_node_id", "parent_node_id"),
        ("position", "position"),
    ),
    "node.update": (
        ("workspace_id", "workspace_id"),
        ("node_id", "node_id"),
        ("title", "title"),
        ("parent_node_id", "parent_node_id"),
        ("position", "position"),
    ),
    "relation.create": (
        ("workspace_id", "workspace_id"),
        ("relation_id", "relation_id"),
        ("relation_type", "relation_type"),
        ("source_id", "source_id"),
        ("target_id", "target_id"),
        ("revision_id", "revision_id"),
    ),
    "revision.get": (
        ("workspace_id", "workspace_id"),
        ("revision_id", "revision_id"),
    ),
    "document.revision.create": (
        ("workspace_id", "workspace_id"),
        ("revision_id", "revision_id"),
        ("document_id", "document_id"),
        ("parent_revision_id", "base_revision_id"),
        ("created_by", "created_by"),
        ("source_candidate_id", "source_candidate_id"),
        ("payload_schema", "payload_schema"),
    ),
    "node.revision.create": (
        ("workspace_id", "workspace_id"),
        ("revision_id", "revision_id"),
        ("node_id", "node_id"),
        ("parent_revision_id", "base_revision_id"),
        ("created_by", "created_by"),
        ("source_candidate_id", "source_candidate_id"),
        ("payload_schema", "payload_schema"),
    ),
}
_DELETE_ENTITY_KEYS: Final[dict[str, str]] = {
    "workspace.delete": "workspace_id",
    "node.delete": "node_id",
    "relation.delete": "relation_id",
}
_REVISION_CREATE_ROUTES: Final = frozenset(
    {"document.revision.create", "node.revision.create"}
)
_UPDATE_ROUTES: Final = frozenset(
    {"workspace.update", "document.update", "node.update"}
)
_BOUND_AUTHORITY_ROUTES = (
    set(_PAGE_ITEM_BINDINGS)
    | set(_DIRECT_RESULT_BINDINGS)
    | set(_DELETE_ENTITY_KEYS)
    | {"revision.content"}
)
if _BOUND_AUTHORITY_ROUTES != {spec.route_id for spec in AUTHORITY_ROUTE_SPECS}:
    raise RuntimeError("Core v1 Authority result-binding coverage drifted")


def route_record(spec: CoreRouteSpec) -> dict[str, Any]:
    """Return the public inventory projection used by router verification."""

    return {
        "route_id": spec.route_id,
        "method": spec.method,
        "path": spec.path_template,
        "success_status": spec.success_status,
    }


ROUTE_ALLOWLIST: Final[tuple[dict[str, Any], ...]] = tuple(
    route_record(spec) for spec in CORE_ROUTE_SPECS
)
AUTHORITY_ROUTE_ALLOWLIST: Final[tuple[dict[str, Any], ...]] = tuple(
    route_record(spec) for spec in AUTHORITY_ROUTE_SPECS
)


@dataclass(frozen=True, slots=True)
class QueryField:
    """HTTP projection type for one non-path v1 GET field."""

    name: str
    integer: bool = False
    nullable: bool = False


_QUERY_LAYOUTS: Final[dict[str, tuple[QueryField, ...]]] = {
    "core-workspace-query/v1": (
        QueryField("workspace_id", nullable=True),
        QueryField("offset", integer=True),
        QueryField("limit", integer=True),
    ),
    "core-workspace-get-query/v1": (),
    "core-document-query/v1": (
        QueryField("document_id", nullable=True),
        QueryField("offset", integer=True),
        QueryField("limit", integer=True),
    ),
    "core-document-get-query/v1": (QueryField("workspace_id"),),
    "core-node-query/v1": (
        QueryField("node_id", nullable=True),
        QueryField("document_id", nullable=True),
        QueryField("parent_node_id", nullable=True),
        QueryField("offset", integer=True),
        QueryField("limit", integer=True),
    ),
    "core-node-get-query/v1": (QueryField("workspace_id"),),
    "core-relation-query/v1": (
        QueryField("relation_id", nullable=True),
        QueryField("source_id", nullable=True),
        QueryField("target_id", nullable=True),
        QueryField("relation_type", nullable=True),
        QueryField("offset", integer=True),
        QueryField("limit", integer=True),
    ),
    "core-document-revision-query/v1": (
        QueryField("workspace_id"),
        QueryField("revision_id", nullable=True),
        QueryField("offset", integer=True),
        QueryField("limit", integer=True),
    ),
    "core-node-revision-query/v1": (
        QueryField("workspace_id"),
        QueryField("revision_id", nullable=True),
        QueryField("offset", integer=True),
        QueryField("limit", integer=True),
    ),
    "core-revision-get-query/v1": (QueryField("workspace_id"),),
    "core-revision-content-query/v1": (
        QueryField("workspace_id"),
        QueryField("offset", integer=True),
        QueryField("length", integer=True),
    ),
    "asset-query/v1": (),
    "asset-read-range-query/v1": (
        QueryField("offset", integer=True),
        QueryField("length", integer=True),
    ),
}


class QueryDecodeError(ValueError):
    """An HTTP query representation cannot be projected into a v1 request."""


def decode_query_request(
    route_id: str,
    query_items: Iterable[tuple[str, str]],
    *,
    path_identity: Mapping[str, str],
) -> dict[str, Any]:
    """Project the strict GET query string into its closed SDK request DTO.

    Presence, unknown names, duplicate names, and non-canonical integer syntax
    are transport failures.  Required values and schema-level semantic bounds
    are intentionally left for the frozen parser so they produce
    ``invalid_request`` rather than an HTTP framework 422.
    """

    spec = ROUTE_BY_ID.get(route_id)
    if spec is None or spec.method != "GET":
        raise QueryDecodeError("route is not a GET route")
    fields = _QUERY_LAYOUTS.get(spec.request_schema)
    if fields is None:
        raise RuntimeError(f"missing query projection for {spec.request_schema}")
    if set(path_identity) != set(spec.path_identity):
        raise QueryDecodeError("path identity keys do not match the route")
    projected: dict[str, Any] = {
        "schema": spec.request_schema,
        **dict(path_identity),
    }
    known = {field.name: field for field in fields}
    seen: set[str] = set()
    for name, raw_value in query_items:
        if name not in known or name in seen:
            raise QueryDecodeError("query name is unknown or repeated")
        seen.add(name)
        field = known[name]
        if field.integer:
            if not _INTEGER_QUERY.fullmatch(raw_value):
                raise QueryDecodeError("query integer syntax is invalid")
            try:
                projected[name] = int(raw_value)
            except ValueError as exc:
                raise QueryDecodeError("query integer exceeds decoder limits") from exc
        else:
            projected[name] = raw_value
    for field in fields:
        if field.nullable and field.name not in projected:
            projected[field.name] = None
    return projected


def request_error_response(error_code: str) -> tuple[int, dict[str, Any]]:
    """Build one exact, parser-validated pre-domain HTTP 400 envelope."""

    message = _REQUEST_ERROR_MESSAGES.get(error_code)
    if message is None:
        raise ValueError(f"unsupported Core HTTP request error code: {error_code}")
    payload = parse_core_http_request_error(
        {
            "schema": "core-http-request-error/v1",
            "error_code": error_code,
            "message": message,
            "retryable": False,
        }
    )
    return 400, payload


class _RequestFailure(Exception):
    """Private control-flow carrier for an already validated 400 payload."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


class CoreHttpAdapter:
    """Strict dispatch boundary over the frozen 26-route Core v1 matrix."""

    def __init__(
        self,
        authority: CoreAuthorityApplication,
        publication_service: PublicationService | None = None,
        assets: AssetStore | None = None,
    ) -> None:
        authority_service = authority.publication_service
        if authority_service is None:
            raise ValueError("Core v1 HTTP requires the existing PublicationService")
        if publication_service is not None and publication_service is not authority_service:
            raise ValueError("PublicationService must be the Authority PublicationService")
        if authority_service.repository is not authority.repository:
            raise ValueError("PublicationService must share the Core authority repository")
        authority_assets = authority_service.assets
        if assets is not None and assets is not authority_assets:
            raise ValueError("AssetStore must be the Authority PublicationService AssetStore")
        self.authority = authority
        self.publication_service = authority_service
        self.assets = authority_assets
        self.publication = PublicationApplication(authority_service)


    @staticmethod
    def request_error(error_code: str) -> tuple[int, dict[str, Any]]:
        return request_error_response(error_code)

    def handle(
        self,
        route_id: str,
        request: Mapping[str, Any],
        *,
        path_identity: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Validate and execute one frozen route without any HTTP framework."""

        spec = ROUTE_BY_ID.get(route_id)
        if spec is None:
            raise ValueError(f"route is not part of the frozen Core v1 matrix: {route_id}")
        if not isinstance(request, Mapping) or request.get("schema") != spec.request_schema:
            return self.request_error("invalid_request")
        try:
            parsed = self._parse_request(spec, request)
        except ContractValidationError:
            return self.request_error("invalid_request")
        if not self._path_identity_matches(spec, parsed, path_identity):
            return self.request_error("invalid_request")

        try:
            if spec.is_authority:
                result = self._authority_result(spec, parsed)
            elif spec.is_publication:
                result = self.publication.accept(parsed)
            elif spec.route_id == "asset.metadata":
                result = self._asset_metadata(parsed)
            elif spec.route_id == "asset.range":
                result = self._asset_range(parsed)
            else:
                raise RuntimeError(f"unhandled frozen Core v1 route: {spec.route_id}")
        except _RequestFailure as failure:
            return 400, failure.payload
        except AuthorityApplicationError as exc:
            return self._domain_error(spec, exc)

        return spec.success_status, self._validate_success(spec, parsed, result)

    def _authority_result(
        self,
        spec: CoreRouteSpec,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        offset = request.get("offset")
        if (
            spec.route_id not in _PAGE_ITEM_BINDINGS
            or not isinstance(offset, int)
            or offset <= _SQLITE_MAX_OFFSET
        ):
            return self.authority.handle(spec.route_id, request)

        bounded_request = dict(request)
        bounded_request["offset"] = _SQLITE_MAX_OFFSET
        bounded_result = self.authority.handle(spec.route_id, bounded_request)
        parsed = self._validate_success(spec, bounded_request, bounded_result)
        if parsed["items"]:
            raise ContractValidationError(
                "Core Authority returned items beyond its signed-64 pagination boundary"
            )
        parsed["offset"] = offset
        parsed["next_offset"] = None
        return parsed

    def _parse_request(self, spec: CoreRouteSpec, request: Mapping[str, Any]) -> dict[str, Any]:
        if spec.is_authority:
            return parse_core_authority(request)
        if spec.is_publication:
            return parse_publication(request)
        if spec.is_asset:
            return parse_asset_contract(request)
        raise RuntimeError(f"unhandled Core v1 request schema: {spec.request_schema}")

    @staticmethod
    def _path_identity_matches(
        spec: CoreRouteSpec,
        request: Mapping[str, Any],
        path_identity: Mapping[str, str] | None,
    ) -> bool:
        if path_identity is None:
            return True
        if set(path_identity) != set(spec.path_identity):
            return False
        for name in spec.path_identity:
            value = path_identity.get(name)
            if not isinstance(value, str) or not value or request.get(name) != value:
                return False
        return True

    def _asset_metadata(self, request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            metadata = self.assets.describe(request["asset_id"])
        except AssetReferenceError as exc:
            raise UnknownReferenceError("asset does not exist") from exc
        return {
            "schema": "asset-metadata/v1",
            "asset_id": metadata.asset_id,
            "sha256": metadata.sha256,
            "mime": metadata.mime,
            "size": metadata.size,
            "logical_role": metadata.logical_role,
            "provenance": metadata.provenance,
            "rebuildable": metadata.rebuildable,
        }

    def _asset_range(self, request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            metadata = self.assets.describe(request["asset_id"])
        except AssetReferenceError as exc:
            raise UnknownReferenceError("asset does not exist") from exc
        offset = request["offset"]
        requested_length = request["length"]
        if offset > metadata.size:
            status, payload = self.request_error("range_out_of_bounds")
            if status != 400:
                raise RuntimeError("request error policy drifted")
            # A typed request failure is not a domain exception; returning it
            # is handled directly by ``handle`` before success validation.
            raise _RequestFailure(payload)
        chunk = self.assets.read(metadata.asset_id, offset=offset, length=requested_length)
        expected_length = min(requested_length, metadata.size - offset)
        if len(chunk) != expected_length:
            raise OSError("asset range bytes are shorter than immutable metadata")
        chunk_hash = hashlib.sha256(chunk).hexdigest()
        if offset == 0 and len(chunk) == metadata.size and chunk_hash != metadata.sha256:
            raise OSError("complete asset range does not match immutable metadata hash")
        end = offset + len(chunk)
        return {
            "schema": "asset-read-range/v1",
            "asset_id": metadata.asset_id,
            "offset": offset,
            "length": len(chunk),
            "total_size": metadata.size,
            "base64_chunk": base64.b64encode(chunk).decode("ascii"),
            "next_offset": end if end < metadata.size else None,
            "content_hash": chunk_hash,
        }

    def _validate_success(
        self,
        spec: CoreRouteSpec,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(result, Mapping) or result.get("schema") != spec.result_schema:
            raise ContractValidationError("Core HTTP result schema is not bound to its route")
        if spec.is_authority:
            parsed = parse_core_authority(
                result,
                expected_workspace_id=request.get("workspace_id"),
            )
            self._validate_authority_result(spec, request, parsed)
        elif spec.is_publication:
            parsed = parse_publication(
                result,
                expected_workspace_id=request.get("workspace_id"),
                command=request,
            )
        elif spec.is_asset:
            parsed = parse_asset_contract(result)
            if parsed["asset_id"] != request["asset_id"]:
                raise ContractValidationError("Asset result identity is not bound to its request")
            if spec.route_id == "asset.range" and (
                parsed["offset"] != request["offset"]
                or parsed["length"] > request["length"]
            ):
                raise ContractValidationError("Asset range result is not bounded by its request")
        else:
            raise RuntimeError(f"unhandled Core v1 result schema: {spec.result_schema}")
        self._validate_result_entity_kind(spec, parsed)
        return parsed

    @staticmethod
    def _validate_authority_result(
        spec: CoreRouteSpec,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        route_id = spec.route_id
        if route_id in _PAGE_ITEM_BINDINGS:
            CoreHttpAdapter._validate_page_result(route_id, request, result)
            return
        if route_id in _DELETE_ENTITY_KEYS:
            entity_key = _DELETE_ENTITY_KEYS[route_id]
            expected = {
                "operation_key": request["operation_key"],
                "workspace_id": request["workspace_id"],
                "entity_id": request[entity_key],
            }
            if any(result.get(name) != value for name, value in expected.items()):
                raise ContractValidationError(
                    f"Core HTTP {route_id} delete result is not bound to its request"
                )
            previous = (
                None
                if route_id == "relation.delete"
                else request["expected_revision"]
            )
            if result.get("previous_revision") != previous:
                raise ContractValidationError(
                    f"Core HTTP {route_id} previous revision is not request-bound"
                )
            return
        if route_id == "revision.content":
            if (
                result.get("revision_id") != request["revision_id"]
                or result.get("offset") != request["offset"]
                or result.get("length", request["length"] + 1) > request["length"]
            ):
                raise ContractValidationError(
                    "Core HTTP revision.content result is not bounded by its request"
                )
            return

        bindings = _DIRECT_RESULT_BINDINGS[route_id]
        if any(
            result.get(result_name) != request[request_name]
            for result_name, request_name in bindings
        ):
            raise ContractValidationError(
                f"Core HTTP {route_id} result identity is not bound to its request"
            )
        if route_id == "workspace.update":
            for field in ("title", "status"):
                if request[field] is not None and result.get(field) != request[field]:
                    raise ContractValidationError(
                        "Core HTTP workspace.update fields are not request-bound"
                    )
        if route_id in _UPDATE_ROUTES and (
            result.get("revision") != request["expected_revision"] + 1
        ):
            raise ContractValidationError(
                f"Core HTTP {route_id} revision is not bound to expected_revision"
            )
        if route_id in _REVISION_CREATE_ROUTES:
            opposite_target = (
                "node_id"
                if route_id == "document.revision.create"
                else "document_id"
            )
            content_hash = hashlib.sha256(request["content"].encode("utf-8")).hexdigest()
            if result.get(opposite_target) is not None or result.get("content_hash") != content_hash:
                raise ContractValidationError(
                    f"Core HTTP {route_id} revision result is not request-bound"
                )

    @staticmethod
    def _validate_page_result(
        route_id: str,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        items = result["items"]
        if (
            result["offset"] != request["offset"]
            or result["limit"] != request["limit"]
            or len(items) > request["limit"]
            or (items and result["offset"] + len(items) > result["total"])
        ):
            raise ContractValidationError(
                f"Core HTTP {route_id} page is not bounded by its request"
            )
        for field in _PAGE_ITEM_BINDINGS[route_id]:
            expected = request.get(field)
            if expected is not None and any(item.get(field) != expected for item in items):
                raise ContractValidationError(
                    f"Core HTTP {route_id} page filter {field} is not request-bound"
                )
        for field in _PAGE_NULL_BINDINGS.get(route_id, ()):
            if any(item.get(field) is not None for item in items):
                raise ContractValidationError(
                    f"Core HTTP {route_id} target kind is not request-bound"
                )

    @staticmethod
    def _validate_result_entity_kind(spec: CoreRouteSpec, result: Mapping[str, Any]) -> None:
        expected = spec.result_entity_kind
        if expected is None:
            return
        schema = result.get("schema")
        actual = (
            result.get("entity_kind")
            if schema == "core-delete-result/v1"
            else _RESULT_ENTITY_KINDS.get(str(schema))
        )
        if actual != expected:
            raise ContractValidationError("Core HTTP result entity kind is not bound to its route")

    @staticmethod
    def _domain_error(
        spec: CoreRouteSpec,
        exc: AuthorityApplicationError,
    ) -> tuple[int, dict[str, Any]]:
        status = exc.status_code
        error_code = exc.error_code
        if not any(status == allowed_status and error_code in allowed_codes for allowed_status, allowed_codes in spec.failure_statuses):
            raise exc
        payload = parse_core_authority(exc.to_error())
        return status, payload


CoreHttpAdapter.dispatch = CoreHttpAdapter.handle
CoreAuthorityHttpAdapter = CoreHttpAdapter
CoreV1HttpAdapter = CoreHttpAdapter


__all__ = [
    "AUTHORITY_ROUTE_ALLOWLIST",
    "AUTHORITY_ROUTE_SPECS",
    "CORE_ROUTE_SPECS",
    "ROUTE_ALLOWLIST",
    "ROUTE_BY_ID",
    "CoreAuthorityHttpAdapter",
    "CoreHttpAdapter",
    "CoreRouteSpec",
    "CoreV1HttpAdapter",
    "QueryDecodeError",
    "decode_query_request",
    "request_error_response",
    "route_record",
]
