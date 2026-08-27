from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "integration"))

from plotpilot_plugin_sdk.ports import CoreAuthorityPort  # noqa: E402
from verify_contracts import (  # noqa: E402
    verify_contract_publication,
    verify_typescript_contract_publication,
)


def test_public_contract_python_gate_executes_every_scoped_negative() -> None:
    result = verify_contract_publication()
    assert result["authority_payloads"] == 35
    assert result["http_exchanges"] == 31
    assert result["http_matrix_routes"] == 26
    assert result["context_profiles"] == ["attempt", "control", "install"]
    assert result["export_items"] == 2
    assert result["plugin_publication_callable"] is False
    assert len(result["python_negative_cases"]) == 30


def test_public_contract_real_typescript_gate_matches_full_corpus() -> None:
    result = verify_typescript_contract_publication()
    corpus = json.loads(
        (ROOT / "contracts" / "corpus" / "contract-publication-v1" / "negative.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "ok"
    assert result["negative_cases"] == [case["case_id"] for case in corpus["cases"]]
    assert result["http_exchanges"] == 31
    assert result["ui_deep_frozen"] is True


def test_plugin_sdk_and_rpc_publish_no_direct_publication_callable() -> None:
    forbidden = {
        name
        for name in dir(CoreAuthorityPort)
        if "publication" in name.lower() or name.lower() in {"accept", "publish"}
    }
    rpc_matrix = json.loads(
        (ROOT / "contracts" / "json-schema" / "rpc-method-matrix.v1.json").read_text(encoding="utf-8")
    )
    methods = [*rpc_matrix["worker_methods"], *rpc_matrix["host_methods"]]
    assert forbidden == set()
    assert not any("publication" in method or "current-revisions" in method for method in methods)


def test_package_golden_is_tracked_as_exact_lf_contract_bytes() -> None:
    path = ROOT / "contracts" / "golden" / "package" / "data" / "rules.json"
    raw = path.read_bytes()
    assert len(raw) == 20
    assert raw.count(b"\n") == 1
    assert b"\r\n" not in raw
    assert hashlib.sha256(raw).hexdigest() == "54bfa55d6557dcf1a11f3e845e6492e4fe34f35a0d6751f45e0e8ae77df36e78"
    completed = subprocess.run(
        ["git", "check-attr", "text", "eol", "--", path.relative_to(ROOT).as_posix()],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "text: set" in completed.stdout
    assert "eol: lf" in completed.stdout
