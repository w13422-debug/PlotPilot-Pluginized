"""Small recursive JSON immutability helpers.

The runtime deliberately freezes contract projections rather than retaining
caller-owned dictionaries.  Only JSON values are accepted because every
frozen value is later hashed or persisted as canonical JSON.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any


def freeze_json(value: Any, *, path: str = "$") -> Any:
    """Return a recursively immutable JSON value and reject non-JSON input."""

    if value is None or type(value) in {str, int, float, bool}:
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} object keys must be strings")
            frozen[key] = freeze_json(item, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(freeze_json(item, path=f"{path}[{index}]") for index, item in enumerate(value))
    raise TypeError(f"{path} contains a non-JSON value: {type(value).__name__}")


def thaw_json(value: Any) -> Any:
    """Materialize an ordinary JSON tree from :func:`freeze_json` output."""

    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value
