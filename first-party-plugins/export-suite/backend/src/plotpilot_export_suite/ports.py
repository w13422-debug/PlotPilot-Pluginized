"""Injection-only ports used by the Export integration.

The public SDK port shape is deliberately small: a reader returns immutable
Asset bytes and a creator returns the new Asset identity (or its metadata).
The adapter below is the only place where the P1 ``AssetStore.read/put``
shape is translated to that port.  No Core repository, HTTP client, SQL
handle, Revision writer, Candidate writer, or Publication method belongs in
this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


AssetBytes = bytes | bytearray | memoryview


@runtime_checkable
class AssetReadPort(Protocol):
    """SDK-style read seam for an immutable P1 Asset."""

    def read_asset(self, asset_id: str) -> AssetBytes: ...


@runtime_checkable
class AssetCreatePort(Protocol):
    """SDK-style create seam for an immutable P1 Asset."""

    def create_asset(self, content: bytes, *, mime: str) -> object: ...


@runtime_checkable
class AssetMetadataReadPort(Protocol):
    """Optional metadata read seam used for an additional identity check."""

    def describe_asset(self, asset_id: str) -> object: ...


class AssetPort(AssetReadPort, AssetCreatePort, Protocol):
    """Composite SDK-style port accepted by :func:`export_from_ports`."""


@dataclass(frozen=True, slots=True)
class AssetReadResult:
    """Optional rich return value for a fake or host adapter.

    A plain ``read_asset(asset_id) -> bytes`` implementation remains the
    preferred and fully supported shape.  When metadata is available from a
    read operation, it is checked by the integration before use; it is never
    trusted merely because it is attached to the bytes.
    """

    content: bytes
    metadata: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, (bytes, bytearray, memoryview)):
            raise TypeError("AssetReadResult.content must be bytes-like")
        object.__setattr__(self, "content", bytes(self.content))


@dataclass(frozen=True, slots=True)
class P1AssetStoreAdapter:
    """Explicitly adapt the existing P1 ``AssetStore`` to SDK-style ports.

    ``AssetStore`` exposes ``read`` and ``put`` rather than the SDK-style
    ``read_asset`` and ``create_asset`` names.  Keeping this translation
    explicit prevents the Export integration from acquiring a hidden P1
    implementation dependency.  ``provenance`` is stable by default so the
    adapter itself is deterministic; detailed source provenance is returned
    in Export's immutable receipt.
    """

    store: object
    logical_role: str = "export_output"
    provenance: str = "plotpilot:export-suite"
    rebuildable: bool = True

    def read_asset(self, asset_id: str) -> AssetBytes:
        reader = getattr(self.store, "read", None)
        if not callable(reader):
            raise TypeError("P1 AssetStore adapter requires store.read(asset_id)")
        return reader(asset_id)

    def describe_asset(self, asset_id: str) -> object:
        describer = getattr(self.store, "describe", None)
        if not callable(describer):
            raise TypeError("P1 AssetStore adapter requires store.describe(asset_id)")
        return describer(asset_id)

    def create_asset(self, content: bytes, *, mime: str) -> object:
        creator = getattr(self.store, "put", None)
        if not callable(creator):
            raise TypeError("P1 AssetStore adapter requires store.put(...)" )
        return creator(
            bytes(content),
            mime=mime,
            logical_role=self.logical_role,
            provenance=self.provenance,
            rebuildable=self.rebuildable,
        )


# A descriptive alias for callers that prefer the port terminology.
P1AssetStorePort = P1AssetStoreAdapter


__all__ = [
    "AssetBytes",
    "AssetCreatePort",
    "AssetMetadataReadPort",
    "AssetPort",
    "AssetReadPort",
    "AssetReadResult",
    "P1AssetStoreAdapter",
    "P1AssetStorePort",
]
