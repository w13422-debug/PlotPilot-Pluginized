"""Deterministic, provenance-bound Quality Suite bundles.

The Quality Suite is deliberately a read-only domain adapter.  It accepts a
caller-supplied immutable content snapshot and its Core provenance, runs the
existing deterministic Finding scanner, and returns in-memory JSON bundles.
There is no AssetStore, Revision, Candidate, Publication, database, or file
write path in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, TypeAlias

from .rules import Finding, scan_language_style


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEVERITIES = frozenset({"info", "warning", "error"})
_UTF8_BOM = b"\xef\xbb\xbf"
_UTF8_BOM_CHAR = "\ufeff"


class ProvenanceError(ValueError):
    """Raised when a source snapshot is missing or fails provenance checks."""


class BundleValidationError(ValueError):
    """Raised when a generated bundle no longer matches its bytes or schema."""


def canonical_json_bytes(value: Mapping[str, Any] | list[Any] | tuple[Any, ...]) -> bytes:
    """Serialize JSON with the repository's deterministic UTF-8 profile.

    The function intentionally does not add a newline, timestamp, UUID, or
    host-specific value.  ``allow_nan=False`` also prevents non-portable JSON
    values from entering a bundle.
    """

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BundleValidationError(f"value is not deterministic JSON: {exc}") from exc


def sha256_hex(value: bytes) -> str:
    """Return a lowercase SHA-256 digest for ``value``."""

    if not isinstance(value, bytes):
        raise TypeError("sha256_hex expects bytes")
    return sha256(value).hexdigest()


def _required_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProvenanceError(f"{field} is required")
    return value.strip()


def _required_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ProvenanceError(f"{field} must be a lowercase SHA-256")
    return value


def _resolve_alias(primary: object, alias: object, field: str, alias_name: str) -> object:
    if primary is not None and alias is not None and primary != alias:
        raise ProvenanceError(f"{field} and {alias_name} do not match")
    return primary if primary is not None else alias


@dataclass(frozen=True, slots=True)
class FrozenSource:
    """An immutable content snapshot with the minimum Core provenance.

    ``asset_hash`` defaults to the content hash because the source Asset is
    the immutable UTF-8 content Asset.  If a caller supplies an independent
    Asset hash, it must match exactly; an Asset ID without the content hash is
    never accepted.
    """

    workspace_id: str
    revision_id: str
    asset_id: str
    content: str | bytes
    content_hash: str
    asset_hash: str | None = None

    def __post_init__(self) -> None:
        workspace_id = _required_identifier(self.workspace_id, "workspace_id")
        revision_id = _required_identifier(self.revision_id, "revision_id")
        asset_id = _required_identifier(self.asset_id, "asset_id")
        content = self.content
        if isinstance(content, bytes):
            if content.startswith(_UTF8_BOM):
                raise ProvenanceError("content must not start with a UTF-8 BOM")
            try:
                content = content.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ProvenanceError("content must be valid UTF-8") from exc
        if not isinstance(content, str):
            raise ProvenanceError("content must be text or UTF-8 bytes")
        if content.startswith(_UTF8_BOM_CHAR):
            raise ProvenanceError("content must not start with a UTF-8 BOM")

        try:
            content_bytes = content.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ProvenanceError("content must be valid UTF-8") from exc

        content_hash = _required_hash(self.content_hash, "content_hash")
        expected_hash = sha256(content_bytes).hexdigest()
        if content_hash != expected_hash:
            raise ProvenanceError("content hash does not match the frozen content")

        asset_hash = content_hash if self.asset_hash is None else _required_hash(self.asset_hash, "asset_hash")
        if asset_hash != content_hash:
            raise ProvenanceError("asset provenance hash does not match content_hash")

        object.__setattr__(self, "workspace_id", workspace_id)
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "asset_id", asset_id)
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "asset_hash", asset_hash)

    @classmethod
    def from_content(
        cls,
        content: str | bytes,
        *,
        workspace_id: str | None = None,
        revision_id: str | None = None,
        source_revision: str | None = None,
        asset_id: str | None = None,
        content_hash: str | None = None,
        asset_hash: str | None = None,
        asset_sha256: str | None = None,
    ) -> "FrozenSource":
        """Construct a source while accepting ``source_revision``/``asset_sha256`` aliases."""

        resolved_revision = _resolve_alias(revision_id, source_revision, "revision_id", "source_revision")
        resolved_asset_hash = _resolve_alias(asset_hash, asset_sha256, "asset_hash", "asset_sha256")
        return cls(
            workspace_id=_required_identifier(workspace_id, "workspace_id"),
            revision_id=_required_identifier(resolved_revision, "revision_id"),
            asset_id=_required_identifier(asset_id, "asset_id"),
            content=content,
            content_hash=_required_hash(content_hash, "content_hash"),
            asset_hash=resolved_asset_hash if resolved_asset_hash is None else _required_hash(resolved_asset_hash, "asset_hash"),
        )

    @property
    def source_revision(self) -> str:
        """Compatibility alias for the frozen Core ``revision_id``."""

        return self.revision_id

    @property
    def asset_sha256(self) -> str:
        """Compatibility alias for the source Asset's immutable hash."""

        assert self.asset_hash is not None
        return self.asset_hash

    @property
    def content_bytes(self) -> bytes:
        return self.content.encode("utf-8")

    def to_dict(self) -> dict[str, str]:
        """Return the closed, JSON-safe provenance projection."""

        return {
            "asset_hash": self.asset_sha256,
            "asset_id": self.asset_id,
            "content_hash": self.content_hash,
            "revision_id": self.revision_id,
            "workspace_id": self.workspace_id,
        }

    def source_refs(self) -> tuple[dict[str, str], dict[str, str]]:
        """Return stable revision-then-Asset references for bundle items."""

        return (
            {
                "revision_or_hash": self.content_hash,
                "source_id": self.revision_id,
                "source_type": "core.revision",
                "workspace_id": self.workspace_id,
            },
            {
                "revision_or_hash": self.asset_sha256,
                "source_id": self.asset_id,
                "source_type": "core.asset",
                "workspace_id": self.workspace_id,
            },
        )


SourceProvenance = FrozenSource
QualitySource = FrozenSource
AssetProvenance = FrozenSource


def freeze_source(
    content: str | bytes,
    *,
    workspace_id: str | None = None,
    revision_id: str | None = None,
    source_revision: str | None = None,
    asset_id: str | None = None,
    content_hash: str | None = None,
    asset_hash: str | None = None,
    asset_sha256: str | None = None,
    asset_provenance: Mapping[str, object] | None = None,
) -> FrozenSource:
    """Freeze and validate a content snapshot before any Finding is scanned.

    The explicit hash is required; callers must not let this helper silently
    turn mutable text into an apparently immutable source.  ``asset_provenance``
    may carry the same fields returned by a Core Asset metadata port and is
    checked for cross-field drift.
    """

    nested: Mapping[str, object] = {} if asset_provenance is None else asset_provenance
    if not isinstance(nested, Mapping):
        raise ProvenanceError("asset_provenance must be a mapping")

    nested_workspace = nested.get("workspace_id")
    if nested_workspace is not None:
        if not isinstance(nested_workspace, str):
            raise ProvenanceError("asset provenance workspace_id must be a string")
        if workspace_id is not None and nested_workspace != workspace_id:
            raise ProvenanceError("asset provenance workspace_id does not match source")
        if workspace_id is None:
            workspace_id = nested_workspace

    nested_asset_id = nested.get("asset_id")
    if nested_asset_id is not None:
        if not isinstance(nested_asset_id, str):
            raise ProvenanceError("asset provenance asset_id must be a string")
        if asset_id is not None and nested_asset_id != asset_id:
            raise ProvenanceError("asset provenance asset_id does not match source")
        if asset_id is None:
            asset_id = nested_asset_id

    nested_hashes: list[tuple[str, str]] = []
    for key in ("asset_hash", "asset_sha256", "sha256", "content_hash"):
        value = nested.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ProvenanceError(f"asset provenance {key} must be a string")
            nested_hashes.append((key, value))
    if nested_hashes:
        nested_values = {value for _, value in nested_hashes}
        if len(nested_values) != 1:
            raise ProvenanceError("asset provenance hash fields do not match")
        nested_asset_hash = nested_hashes[0][1]
        if asset_hash is not None and nested_asset_hash != asset_hash:
            raise ProvenanceError("asset provenance hash fields do not match")
        if asset_sha256 is not None and nested_asset_hash != asset_sha256:
            raise ProvenanceError("asset provenance hash fields do not match")
        if asset_hash is None:
            asset_hash = nested_asset_hash

    nested_schema = nested.get("schema")
    if nested_schema is not None and nested_schema != "asset-metadata/v1":
        raise ProvenanceError("asset provenance schema is not asset-metadata/v1")

    resolved_revision = _resolve_alias(revision_id, source_revision, "revision_id", "source_revision")
    resolved_asset_hash = _resolve_alias(asset_hash, asset_sha256, "asset_hash", "asset_sha256")
    frozen = FrozenSource.from_content(
        content,
        workspace_id=workspace_id,
        revision_id=resolved_revision if isinstance(resolved_revision, str) else None,
        asset_id=asset_id,
        content_hash=content_hash,
        asset_hash=resolved_asset_hash if isinstance(resolved_asset_hash, str) else None,
    )

    for key in ("size", "total_size"):
        value = nested.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value != len(frozen.content_bytes)):
            raise ProvenanceError(f"asset provenance {key} does not match content size")
    encodings = [(key, nested.get(key)) for key in ("encoding", "charset") if nested.get(key) is not None]
    if encodings and any(value != "utf-8" for _, value in encodings):
        raise ProvenanceError("asset provenance encoding must be utf-8")
    if encodings and len({value for _, value in encodings}) != 1:
        raise ProvenanceError("asset provenance encoding fields do not match")
    return frozen


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True, init=False)
class QualityBundle:
    """An immutable in-memory bundle with canonical bytes and its digest."""

    payload: Mapping[str, Any]
    json_bytes: bytes
    sha256: str

    def __init__(self, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise BundleValidationError("bundle payload must be a mapping")
        plain = _thaw(_freeze(payload))
        raw = canonical_json_bytes(plain)
        object.__setattr__(self, "payload", _freeze(plain))
        object.__setattr__(self, "json_bytes", raw)
        object.__setattr__(self, "sha256", sha256_hex(raw))

    @property
    def bytes(self) -> bytes:
        """Deterministic JSON bytes (alias for ``json_bytes``)."""

        return self.json_bytes

    @property
    def raw_bytes(self) -> bytes:
        return self.json_bytes

    @property
    def digest(self) -> str:
        return self.sha256

    @property
    def data(self) -> dict[str, Any]:
        return self.to_dict()

    @property
    def schema(self) -> str:
        return str(self.payload["schema"])

    @property
    def contract_id(self) -> str:
        return str(self.payload["contract_id"])

    @property
    def bundle_id(self) -> str:
        return str(self.payload["bundle_id"])

    @property
    def bundle_type(self) -> str:
        return str(self.payload["bundle_type"])

    @property
    def status(self) -> str:
        return str(self.payload["status"])

    @property
    def provenance(self) -> Mapping[str, Any]:
        return self.payload["provenance"]

    @property
    def items(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self.payload["items"])

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self.payload)

    as_dict = to_dict

    def to_bytes(self) -> bytes:
        return self.json_bytes

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def __contains__(self, key: object) -> bool:
        return key in self.payload


DiagnosticBundle = QualityBundle
FindingBundle = QualityBundle
CandidateBundle = QualityBundle


@dataclass(frozen=True, slots=True)
class QualityBundleSet:
    """The three deterministic projections produced from one source freeze."""

    diagnostic: QualityBundle
    finding: QualityBundle
    candidate: QualityBundle

    @property
    def diagnostic_bundle(self) -> QualityBundle:
        return self.diagnostic

    @property
    def finding_bundle(self) -> QualityBundle:
        return self.finding

    @property
    def candidate_bundle(self) -> QualityBundle:
        return self.candidate

    def __iter__(self):
        yield self.diagnostic
        yield self.finding
        yield self.candidate


BundleInput: TypeAlias = FrozenSource | Mapping[str, object]


def _coerce_source(source: BundleInput) -> FrozenSource:
    if isinstance(source, FrozenSource):
        return source
    if not isinstance(source, Mapping):
        raise ProvenanceError("bundle input must be a FrozenSource or provenance mapping")

    root = source
    if "provenance" in root:
        nested_value = root["provenance"]
    else:
        nested_value = root.get("source")
    if nested_value is None:
        nested: Mapping[str, object] = {}
    elif isinstance(nested_value, Mapping):
        nested = nested_value
    else:
        raise ProvenanceError("bundle provenance must be a mapping")

    if "asset" in root:
        asset_value = root["asset"]
    else:
        asset_value = root.get("asset_provenance")
    if asset_value is None:
        asset: Mapping[str, object] = {}
    elif isinstance(asset_value, Mapping):
        asset = asset_value
    else:
        raise ProvenanceError("bundle asset provenance must be a mapping")

    def pick(*keys: str) -> object:
        for mapping in (root, nested, asset):
            for key in keys:
                if key in mapping and mapping[key] is not None:
                    return mapping[key]
        return None

    content = pick("content", "text")
    return freeze_source(
        content,  # type: ignore[arg-type]
        workspace_id=pick("workspace_id"),  # type: ignore[arg-type]
        revision_id=pick("revision_id"),  # type: ignore[arg-type]
        source_revision=pick("source_revision"),  # type: ignore[arg-type]
        asset_id=pick("asset_id"),  # type: ignore[arg-type]
        content_hash=pick("content_hash"),  # type: ignore[arg-type]
        asset_hash=pick("asset_hash"),  # type: ignore[arg-type]
        asset_sha256=pick("asset_sha256", "sha256"),  # type: ignore[arg-type]
        asset_provenance=asset if asset else None,
    )


def _coerce_finding(value: Finding | Mapping[str, object]) -> Finding:
    if isinstance(value, Finding):
        return value
    if not isinstance(value, Mapping):
        raise BundleValidationError("findings must contain Finding values or mappings")
    try:
        return Finding(
            str(value["rule_id"]),
            str(value["severity"]),
            str(value["message"]),
            int(value["start"]),
            int(value["end"]),
            str(value["excerpt"]),
            str(value["source_hash"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BundleValidationError(f"invalid Finding mapping: {exc}") from exc


def _ordered_findings(source: FrozenSource, findings: Iterable[Finding] | None) -> tuple[tuple[str, Finding], ...]:
    values = tuple(scan_language_style(source.content) if findings is None else (_coerce_finding(item) for item in findings))
    for finding in values:
        if finding.source_hash != source.content_hash:
            raise ProvenanceError("Finding source_hash does not match the frozen content hash")
        if finding.severity not in _SEVERITIES:
            raise BundleValidationError(f"unsupported Finding severity: {finding.severity}")
        if finding.start < 0 or finding.end < finding.start or finding.end > len(source.content):
            raise BundleValidationError("Finding offsets are outside the frozen content")
        if finding.excerpt != source.content[finding.start : finding.end]:
            raise ProvenanceError("Finding excerpt does not match the frozen content")

    ordered = sorted(
        values,
        key=lambda item: (
            item.start,
            item.end,
            item.rule_id,
            item.severity,
            item.message,
            item.excerpt,
            item.source_hash,
        ),
    )
    occurrences: dict[tuple[object, ...], int] = {}
    result: list[tuple[str, Finding]] = []
    source_dict = source.to_dict()
    for finding in ordered:
        finding_dict = {
            "end": finding.end,
            "excerpt": finding.excerpt,
            "message": finding.message,
            "rule_id": finding.rule_id,
            "severity": finding.severity,
            "source_hash": finding.source_hash,
            "start": finding.start,
        }
        identity = tuple(finding_dict.values())
        occurrence = occurrences.get(identity, 0)
        occurrences[identity] = occurrence + 1
        item_hash = sha256_hex(canonical_json_bytes({"finding": finding_dict, "occurrence": occurrence, "source": source_dict}))
        result.append((f"finding-{item_hash}", finding))
    return tuple(result)


def _finding_item_id(item_id: str, prefix: str) -> str:
    return f"{prefix}-{item_id.removeprefix('finding-')}"


def _finding_dict(finding: Finding) -> dict[str, object]:
    return {
        "end": finding.end,
        "excerpt": finding.excerpt,
        "message": finding.message,
        "rule_id": finding.rule_id,
        "severity": finding.severity,
        "source_hash": finding.source_hash,
        "start": finding.start,
    }


def _source_refs(source: FrozenSource) -> list[dict[str, str]]:
    return [dict(ref) for ref in source.source_refs()]


def _diagnostic_items(source: FrozenSource, findings: tuple[tuple[str, Finding], ...]) -> list[dict[str, object]]:
    return [
        {
            "code": finding.rule_id,
            "details_asset_id": None,
            "details_hash": None,
            "item_id": _finding_item_id(item_id, "diagnostic"),
            "message": finding.message,
            "schema": "diagnostic-item/v1",
            "severity": finding.severity,
            "source_refs": _source_refs(source),
            "status": "complete",
        }
        for item_id, finding in findings
    ]


def _finding_items(source: FrozenSource, findings: tuple[tuple[str, Finding], ...]) -> list[dict[str, object]]:
    return [
        {
            **_finding_dict(finding),
            "item_id": item_id,
            "schema": "finding-item/v1",
            "source_refs": _source_refs(source),
            "status": "complete",
        }
        for item_id, finding in findings
    ]


def _candidate_items(
    source: FrozenSource,
    findings: tuple[tuple[str, Finding], ...],
    suggestions: Mapping[str, str] | None,
) -> list[dict[str, object]]:
    suggestions = suggestions or {}
    for key, value in suggestions.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value.strip():
            raise BundleValidationError("candidate suggestions must map non-blank strings to non-blank strings")

    items: list[dict[str, object]] = []
    for item_id, finding in findings:
        suggestion = suggestions.get(item_id) or suggestions.get(finding.rule_id)
        if suggestion is None:
            suggestion = (
                f"Review {finding.rule_id} at the reported range; any text change must be an explicit "
                "user-reviewed Candidate/Publication operation."
            )
        items.append(
            {
                "action": "review",
                "candidate_id": _finding_item_id(item_id, "candidate"),
                "item_id": _finding_item_id(item_id, "candidate-item"),
                "publication_eligibility": "none",
                "schema": "quality-candidate-item/v1",
                "source_finding_id": item_id,
                "source_refs": _source_refs(source),
                "status": "proposed",
                "suggestion": suggestion,
                "target": {
                    "asset_id": source.asset_id,
                    "content_hash": source.content_hash,
                    "revision_id": source.revision_id,
                    "workspace_id": source.workspace_id,
                },
            }
        )
    return items


def _make_bundle(
    bundle_type: str,
    source: FrozenSource,
    findings: tuple[tuple[str, Finding], ...],
    suggestions: Mapping[str, str] | None = None,
) -> QualityBundle:
    if bundle_type == "diagnostic":
        items = _diagnostic_items(source, findings)
        status = "complete"
    elif bundle_type == "finding":
        items = _finding_items(source, findings)
        status = "complete"
    elif bundle_type == "candidate":
        items = _candidate_items(source, findings, suggestions)
        status = "proposed"
    else:
        raise BundleValidationError(f"unsupported Quality bundle type: {bundle_type}")

    contract_id = f"{bundle_type}-bundle/v1"
    payload: dict[str, object] = {
        "bundle_id": "",
        "bundle_type": bundle_type,
        "contract_id": contract_id,
        "items": items,
        "provenance": source.to_dict(),
        "schema": "quality-bundle/v1",
        "status": status,
        "workspace_id": source.workspace_id,
    }
    if bundle_type == "candidate":
        # This is deliberately a non-Core proposal projection.  It carries no
        # mutation/payload Asset and has no method that can publish it.
        payload["publication_eligibility"] = "none"

    identity = dict(payload)
    identity.pop("bundle_id")
    bundle_hash = sha256_hex(canonical_json_bytes(identity))
    payload["bundle_id"] = f"quality-{bundle_type}-{bundle_hash}"
    return QualityBundle(payload)


def build_diagnostic_bundle(source: BundleInput, findings: Iterable[Finding] | None = None) -> QualityBundle:
    """Build a deterministic diagnostic bundle from a frozen source."""

    frozen = _coerce_source(source)
    return _make_bundle("diagnostic", frozen, _ordered_findings(frozen, findings))


def build_finding_bundle(source: BundleInput, findings: Iterable[Finding] | None = None) -> QualityBundle:
    """Build a deterministic Finding bundle, including offsets and excerpts."""

    frozen = _coerce_source(source)
    return _make_bundle("finding", frozen, _ordered_findings(frozen, findings))


def build_candidate_bundle(
    source: BundleInput,
    findings: Iterable[Finding] | None = None,
    *,
    suggestions: Mapping[str, str] | None = None,
) -> QualityBundle:
    """Build proposed, review-only suggestions without a mutation or publish path."""

    frozen = _coerce_source(source)
    return _make_bundle("candidate", frozen, _ordered_findings(frozen, findings), suggestions)


def build_quality_bundles(
    source: BundleInput,
    findings: Iterable[Finding] | None = None,
    *,
    suggestions: Mapping[str, str] | None = None,
) -> QualityBundleSet:
    """Build diagnostic, Finding, and proposed-candidate projections once."""

    frozen = _coerce_source(source)
    ordered = _ordered_findings(frozen, findings)
    return QualityBundleSet(
        diagnostic=_make_bundle("diagnostic", frozen, ordered),
        finding=_make_bundle("finding", frozen, ordered),
        candidate=_make_bundle("candidate", frozen, ordered, suggestions),
    )


generate_quality_bundles = build_quality_bundles


def bundle_sha256(bundle: QualityBundle | bytes) -> str:
    """Recompute the digest of a bundle or canonical bytes."""

    return sha256_hex(bundle.json_bytes if isinstance(bundle, QualityBundle) else bundle)


def validate_bundle(bundle: QualityBundle) -> None:
    """Validate deterministic bytes, digest, provenance, and candidate fencing."""

    if not isinstance(bundle, QualityBundle):
        raise BundleValidationError("expected a QualityBundle")
    raw = canonical_json_bytes(bundle.to_dict())
    if raw != bundle.json_bytes:
        raise BundleValidationError("bundle JSON bytes are not canonical")
    if sha256_hex(raw) != bundle.sha256:
        raise BundleValidationError("bundle sha256 does not match JSON bytes")
    if bundle.schema != "quality-bundle/v1":
        raise BundleValidationError("bundle schema is not the private quality-bundle/v1 projection")
    if bundle.bundle_type not in {"diagnostic", "finding", "candidate"}:
        raise BundleValidationError("unsupported Quality bundle type")
    if bundle.contract_id != f"{bundle.bundle_type}-bundle/v1":
        raise BundleValidationError("bundle contract_id does not match bundle_type")
    identity = bundle.to_dict()
    bundle_id = identity.pop("bundle_id", None)
    expected_bundle_id = f"quality-{bundle.bundle_type}-{sha256_hex(canonical_json_bytes(identity))}"
    if bundle_id != expected_bundle_id:
        raise BundleValidationError("bundle_id does not match the canonical bundle payload")
    provenance = bundle.provenance
    required = {"asset_hash", "asset_id", "content_hash", "revision_id", "workspace_id"}
    if set(provenance) != required:
        raise BundleValidationError("bundle provenance fields are not closed")
    if bundle["workspace_id"] != provenance["workspace_id"]:
        raise BundleValidationError("bundle workspace does not match provenance")
    if not all(isinstance(provenance[field], str) and provenance[field] for field in required):
        raise BundleValidationError("bundle provenance values must be non-empty strings")
    if not _SHA256_RE.fullmatch(provenance["content_hash"]) or not _SHA256_RE.fullmatch(provenance["asset_hash"]):
        raise BundleValidationError("bundle provenance hashes are not lowercase SHA-256")
    if provenance["asset_hash"] != provenance["content_hash"]:
        raise BundleValidationError("bundle asset/content provenance drift")
    expected_status = "proposed" if bundle.bundle_type == "candidate" else "complete"
    if bundle.status != expected_status:
        raise BundleValidationError(f"{bundle.bundle_type} bundle has an invalid status")
    if bundle.bundle_type == "candidate":
        if bundle["publication_eligibility"] != "none":
            raise BundleValidationError("candidate bundle cannot be publication-eligible")
        if any(item.get("status") != "proposed" for item in bundle.items):
            raise BundleValidationError("candidate items must remain proposed")
    elif "publication_eligibility" in bundle:
        raise BundleValidationError("non-candidate bundle cannot carry publication eligibility")


__all__ = [
    "AssetProvenance",
    "BundleInput",
    "BundleValidationError",
    "CandidateBundle",
    "DiagnosticBundle",
    "FindingBundle",
    "FrozenSource",
    "ProvenanceError",
    "QualityBundle",
    "QualityBundleSet",
    "QualitySource",
    "SourceProvenance",
    "build_candidate_bundle",
    "build_diagnostic_bundle",
    "build_finding_bundle",
    "build_quality_bundles",
    "bundle_sha256",
    "canonical_json_bytes",
    "freeze_source",
    "generate_quality_bundles",
    "sha256_hex",
    "validate_bundle",
]
