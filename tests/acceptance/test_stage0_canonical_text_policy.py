from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ROOT_RULES = (
    "* text=auto eol=lf",
    "*.bat text eol=crlf",
    "*.cmd text eol=crlf",
)
REAL_REPRESENTATIVES = (
    "docs/contracts/finding-closure-v1.json",
    "tools/integration/generate_m0_delivery.py",
    "frontend/src/contracts/canonical.ts",
    "docs/deliveries/PPA-00/m0-open-manifest.json",
    "frontend/src-tauri/bin/backend-sidecar.bat",
    "coordination/integration-queue/WAVE-D-20260830-R2/evidence/closures/01-finding-manifest.json",
)
EXPECTED_MATERIALIZATION = {
    "docs/contracts/finding-closure-v1.json": {"text": "set", "eol": "lf"},
    "tools/integration/generate_m0_delivery.py": {"text": "set", "eol": "lf"},
    "frontend/src/contracts/canonical.ts": {"text": "set", "eol": "lf"},
    "docs/deliveries/PPA-00/m0-open-manifest.json": {"text": "set", "eol": "lf"},
    "frontend/src-tauri/bin/backend-sidecar.bat": {"text": "set", "eol": "crlf"},
    "tools/__stage0_canonical_text_policy_probe__.cmd": {"text": "set", "eol": "crlf"},
    "coordination/integration-queue/WAVE-D-20260830-R2/evidence/closures/01-finding-manifest.json": {
        "text": "unset",
        "eol": "unspecified",
    },
}


def _git_check_attr(paths: tuple[str, ...]) -> dict[str, dict[str, str]]:
    command = ("git", "check-attr", "-z", "text", "eol", "--", *paths)
    env = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    env.update({"GIT_ATTR_NOSYSTEM": "1", "LC_ALL": "C", "LANG": "C"})
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, (
        f"git check-attr failed with exit {completed.returncode}\n"
        f"command: {command!r}\n"
        f"stdout: {completed.stdout.decode('utf-8', errors='replace')}\n"
        f"stderr: {completed.stderr.decode('utf-8', errors='replace')}"
    )

    fields = completed.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    assert len(fields) % 3 == 0, f"malformed git check-attr -z output: {completed.stdout!r}"

    attributes: dict[str, dict[str, str]] = {}
    for offset in range(0, len(fields), 3):
        path, attribute, value = (
            field.decode("utf-8", errors="surrogateescape")
            for field in fields[offset : offset + 3]
        )
        attributes.setdefault(path, {})[attribute] = value

    expected_paths = set(paths)
    assert set(attributes) == expected_paths, (
        f"git check-attr returned unexpected paths; expected={sorted(expected_paths)!r}, "
        f"actual={sorted(attributes)!r}, raw={completed.stdout!r}"
    )
    for path, values in attributes.items():
        assert set(values) == {"text", "eol"}, (
            f"git check-attr returned incomplete attributes for {path!r}: {values!r}"
        )
    return attributes


def _materialization_state(raw: dict[str, str]) -> dict[str, str]:
    text = raw["text"]
    if text == "unset":
        # Git ignores eol when a nested rule explicitly disables text conversion.
        return {"text": "unset", "eol": "unspecified"}
    if text in {"auto", "set"}:
        return {"text": "set", "eol": raw["eol"]}
    return {"text": text, "eol": raw["eol"]}


def test_stage0_canonical_text_materialization_policy() -> None:
    policy_bytes = (ROOT / ".gitattributes").read_bytes()
    assert b"\r" not in policy_bytes, "root .gitattributes must itself use LF bytes"
    policy_rules = tuple(
        line.strip()
        for line in policy_bytes.decode("utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    assert policy_rules == ROOT_RULES, (
        f"unexpected root .gitattributes rules: expected={ROOT_RULES!r}, actual={policy_rules!r}"
    )

    missing = [path for path in REAL_REPRESENTATIVES if not (ROOT / path).is_file()]
    assert not missing, f"canonical text policy representatives are missing: {missing!r}"

    paths = tuple(EXPECTED_MATERIALIZATION)
    raw = _git_check_attr(paths)
    actual = {path: _materialization_state(raw[path]) for path in paths}
    assert actual == EXPECTED_MATERIALIZATION, (
        "canonical text materialization policy mismatch\n"
        f"expected={EXPECTED_MATERIALIZATION!r}\n"
        f"effective={actual!r}\n"
        f"raw_git_check_attr={raw!r}"
    )
