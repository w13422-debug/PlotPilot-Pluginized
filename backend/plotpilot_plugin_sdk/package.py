"""Frozen §13.3/§13.4 package and Skill digest implementation."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping

from .canonical import sha256_hex
from .errors import ContractError, ErrorCode


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_RESERVED = {"con", "prn", "aux", "nul", "clock$", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_FORBIDDEN_CHARS = set('<>"|?*:\x00')


def normalize_relative_path(path: str) -> str:
    """Validate the Windows package path profile without silently trimming."""
    if not isinstance(path, str) or not path:
        raise ContractError(ErrorCode.ASSET_ERROR, "path must be a non-empty string")
    if "\\" in path or "\x00" in path:
        raise ContractError(ErrorCode.ASSET_ERROR, "backslash/NUL is forbidden in package paths", path=path)
    normalized = unicodedata.normalize("NFC", path)
    if normalized.startswith("/") or normalized.startswith("//") or _DRIVE.match(normalized) or normalized.startswith("\\\\?\\"):
        raise ContractError(ErrorCode.ASSET_ERROR, "absolute, drive, UNC or extended path is forbidden", path=path)
    segments = normalized.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ContractError(ErrorCode.ASSET_ERROR, "empty/dot traversal segment is forbidden", path=path)
    if len(normalized) > 240:
        raise ContractError(ErrorCode.ASSET_ERROR, "relative path exceeds 240 Unicode scalar values", path=path)
    for segment in segments:
        if segment[-1] in ". ":
            raise ContractError(ErrorCode.ASSET_ERROR, "trailing dot/space is forbidden", path=path)
        if any(char in _FORBIDDEN_CHARS or ord(char) < 32 for char in segment):
            raise ContractError(ErrorCode.ASSET_ERROR, "forbidden Windows path character", path=path)
        stem = segment.split(".", 1)[0].casefold()
        if stem in _RESERVED:
            raise ContractError(ErrorCode.ASSET_ERROR, "Windows reserved device name is forbidden", path=path)
    return normalized


def _normalized_file_map(files: Mapping[str, bytes]) -> dict[str, bytes]:
    normalized: dict[str, bytes] = {}
    for raw_path, content in files.items():
        path = normalize_relative_path(raw_path)
        if path == "files.sha256":
            raise ContractError(ErrorCode.ASSET_ERROR, "files.sha256 is generated and cannot be an input file")
        if not isinstance(content, bytes):
            raise ContractError(ErrorCode.ASSET_ERROR, "package content must be raw bytes", path=path)
        key = path.casefold()
        if key in {existing.casefold() for existing in normalized}:
            raise ContractError(ErrorCode.ASSET_ERROR, "casefold path collision", path=path)
        normalized[path] = content
    return normalized


def build_files_sha256(files: Mapping[str, bytes]) -> bytes:
    normalized = _normalized_file_map(files)
    lines = [f"{sha256_hex(normalized[path])}  {path}\n" for path in sorted(normalized, key=lambda p: p.encode("utf-8"))]
    # The digest line uses ASCII separators/hex but the normalized path may
    # contain Unicode.  §13.4 specifies UTF-8 bytes for the complete manifest,
    # not an ASCII-only path profile.
    result = "".join(lines).encode("utf-8")
    if result.startswith(b"\xef\xbb\xbf") or not result.endswith(b"\n"):
        raise ContractError(ErrorCode.ASSET_ERROR, "files.sha256 must be UTF-8 LF with a final newline")
    return result


@dataclass(frozen=True)
class PackageDigest:
    files_sha256: bytes
    package_hash: str
    release_id: str


def _release_id(domain: str, identity: str, version: str, package_digest_value: str) -> str:
    return sha256_hex(f"{domain}\n{identity}\n{version}\n{package_digest_value}\n".encode("ascii"))


def package_hash(files: Mapping[str, bytes]) -> str:
    return sha256_hex(b"plotpilot-package/v1\n" + build_files_sha256(files))


def release_id(plugin_id: str, version: str, digest: str) -> str:
    if not _HEX64.fullmatch(digest):
        raise ContractError(ErrorCode.ASSET_ERROR, "package hash must be lowercase SHA-256")
    return _release_id("plotpilot-release/v1", plugin_id, version, digest)


def digest_package(files: Mapping[str, bytes], plugin_id: str, version: str) -> PackageDigest:
    files_sha256 = build_files_sha256(files)
    digest = sha256_hex(b"plotpilot-package/v1\n" + files_sha256)
    return PackageDigest(files_sha256, digest, release_id(plugin_id, version, digest))


def skill_package_hash(files: Mapping[str, bytes]) -> str:
    return sha256_hex(b"plotpilot-skill-package/v1\n" + build_files_sha256(files))


def skill_release_id(skill_id: str, version: str, digest: str) -> str:
    if not _HEX64.fullmatch(digest):
        raise ContractError(ErrorCode.ASSET_ERROR, "Skill package hash must be lowercase SHA-256")
    return _release_id("plotpilot-skill-release/v1", skill_id, version, digest)
