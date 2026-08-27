"""Tiny venv-side entrypoint loader; invoked with ``python -I`` and no shell."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import re
import sys

_ENTRYPOINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or _ENTRYPOINT.fullmatch(args[0]) is None:
        raise SystemExit("invalid immutable worker entrypoint")
    module_name, function_name = args[0].split(":", 1)
    function = getattr(importlib.import_module(module_name), function_name)
    if not callable(function):
        raise TypeError("worker entrypoint is not callable")
    result = function()
    if inspect.isawaitable(result):
        asyncio.run(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
