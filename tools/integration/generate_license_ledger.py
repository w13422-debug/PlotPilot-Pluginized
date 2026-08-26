"""Create the P0 dependency/license ledger from checked-in lock metadata."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _node_version() -> str:
    try:
        return subprocess.run(["node", "--version"], check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def _npm_dependencies() -> dict[str, Any]:
    package_path = ROOT / "frontend" / "package.json"
    lock_path = ROOT / "frontend" / "package-lock.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    direct = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    all_entries: list[dict[str, Any]] = []
    for lock_path_key, entry in sorted(lock.get("packages", {}).items()):
        if not lock_path_key or not isinstance(entry, dict) or "version" not in entry:
            continue
        all_entries.append(
            {
                "lock_path": lock_path_key,
                "version": entry["version"],
                "license": entry.get("license", "metadata-not-present-in-package-lock"),
                "dev": bool(entry.get("dev", False)),
                "optional": bool(entry.get("optional", False)),
                "resolved": entry.get("resolved"),
                "integrity": entry.get("integrity"),
            }
        )
    direct_entries = []
    for name, specifier in sorted(direct.items()):
        entry = lock.get("packages", {}).get(f"node_modules/{name}", {})
        direct_entries.append(
            {
                "name": name,
                "specifier": specifier,
                "version": entry.get("version"),
                "license": entry.get("license", "metadata-not-present-in-package-lock"),
                "resolved": entry.get("resolved"),
                "integrity": entry.get("integrity"),
            }
        )
    return {
        "lockfile": str(lock_path),
        "lockfile_sha256": _sha256(lock_path),
        "lockfile_version": lock.get("lockfileVersion"),
        "direct": direct_entries,
        "resolved_package_entries": all_entries,
    }


def _python_packages() -> list[dict[str, Any]]:
    names = ["jsonschema", "rfc8785", "pytest", "fastapi", "uvicorn", "pydantic", "httpx", "starlette"]
    result: list[dict[str, Any]] = []
    for name in names:
        try:
            metadata = importlib.metadata.metadata(name)
            license_value = metadata.get("License-Expression") or metadata.get("License")
            if not license_value:
                classifiers = metadata.get_all("Classifier") or []
                if any(item.endswith("Apache Software License") for item in classifiers):
                    license_value = "Apache-2.0"
                elif any(item.endswith("MIT License") for item in classifiers):
                    license_value = "MIT"
                elif any(item.endswith("BSD License") for item in classifiers):
                    license_value = "BSD-3-Clause"
            result.append(
                {
                    "name": name,
                    "version": importlib.metadata.version(name),
                    "license": license_value or "metadata-license-undetermined",
                    "home_page": metadata.get("Home-page"),
                    "source_urls": [value for value in metadata.get_all("Project-URL") or [] if value.startswith("Source,") or value.startswith("Homepage,")],
                }
            )
        except importlib.metadata.PackageNotFoundError:
            result.append({"name": name, "status": "not-installed-in-verification-runtime"})
    return result


def generate() -> dict[str, Any]:
    return {
        "schema": "plotpilot-license-ledger/v1",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "policy": {
            "source_code": "PlotPilot baseline remains under the existing LICENSE; P0 adds no copied third-party source.",
            "npm": "All direct and lock-resolved frontend packages are listed with lock path, license metadata, resolved URL and integrity when present.",
            "python": "M0 verification runtime packages are listed from importlib.metadata; the checked-in requirements files remain the dependency declaration.",
            "review": "Any package with missing license metadata blocks release until its upstream notice is reviewed.",
        },
        "toolchain": {
            "python": platform.python_version(),
            "node": _node_version(),
            "platform": platform.platform(),
        },
        "python_packages": _python_packages(),
        "npm": _npm_dependencies(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "deliveries" / "PPA-00" / "license-ledger.json")
    args = parser.parse_args()
    value = json.dumps(generate(), ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(value, encoding="utf-8", newline="\n")
    print(value, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
