from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True, slots=True)
class AssetMetadata:
    asset_id: str
    sha256: str
    mime: str
    size: int
    logical_role: str
    provenance: str
    rebuildable: bool


class AssetStore:
    """Non-replacing content-addressed storage with bounded range reads."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.metadata = self.root / "metadata"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.metadata.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _asset_id(digest: str) -> str:
        return f"asset-sha256-{digest}"

    def put(
        self,
        content: bytes | BinaryIO,
        *,
        mime: str,
        logical_role: str,
        provenance: str,
        rebuildable: bool = False,
    ) -> AssetMetadata:
        data = bytes(content) if isinstance(content, (bytes, bytearray)) else content.read()
        if not isinstance(data, bytes):
            raise TypeError("asset stream must return bytes")
        digest = hashlib.sha256(data).hexdigest()
        asset_id = self._asset_id(digest)
        meta = AssetMetadata(asset_id, digest, mime, len(data), logical_role, provenance, rebuildable)
        object_path = self.objects / digest[:2] / digest
        object_path.parent.mkdir(parents=True, exist_ok=True)
        self._publish_nonreplace(object_path, data)
        meta_bytes = json.dumps(asdict(meta), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._publish_nonreplace(self.metadata / f"{digest}.json", meta_bytes)
        if object_path.read_bytes() != data:
            raise OSError("existing asset bytes do not match content address")
        return meta

    @staticmethod
    def _publish_nonreplace(path: Path, data: bytes) -> None:
        if path.exists():
            if path.read_bytes() != data:
                raise OSError(f"content-address collision at {path}")
            return
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temp.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temp, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise OSError(f"content-address collision at {path}")
        finally:
            temp.unlink(missing_ok=True)

    def describe(self, asset_id: str) -> AssetMetadata:
        digest = self._digest(asset_id)
        raw = json.loads((self.metadata / f"{digest}.json").read_text(encoding="utf-8"))
        meta = AssetMetadata(**raw)
        if meta.asset_id != asset_id or meta.sha256 != digest:
            raise OSError("asset metadata identity mismatch")
        return meta

    def read(self, asset_id: str, *, offset: int = 0, length: int | None = None) -> bytes:
        if offset < 0 or (length is not None and length < 0):
            raise ValueError("asset range must be non-negative")
        digest = self._digest(asset_id)
        path = self.objects / digest[:2] / digest
        with path.open("rb") as stream:
            stream.seek(offset)
            result = stream.read() if length is None else stream.read(length)
        if offset == 0 and length is None and hashlib.sha256(result).hexdigest() != digest:
            raise OSError("asset content hash mismatch")
        return result

    @staticmethod
    def _digest(asset_id: str) -> str:
        prefix = "asset-sha256-"
        if not asset_id.startswith(prefix):
            raise ValueError("invalid asset id")
        digest = asset_id[len(prefix) :]
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("invalid asset id")
        return digest
