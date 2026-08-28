from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "integration"))

from plotpilot_plugin_sdk.core_api import (  # noqa: E402
    parse_core_http_request_error,
    parse_core_http_request_failure_policy,
)
from verify_contracts import verify_core_http_request_failure_contract  # noqa: E402


def test_request_failure_goldens_pass_python_and_typescript() -> None:
    result = verify_core_http_request_failure_contract()
    assert result["status"] == "ok"
    assert result["http_status"] == 400
    assert result["positive_errors"] == 4
    assert len(result["python_negative_cases"]) == 13
    assert result["draft_negative_cases"] == result["python_negative_cases"]
    assert result["generic_negative_cases"] == result["python_negative_cases"]
    assert result["typescript_negative_cases"] == result["python_negative_cases"]
    assert {
        "policy-binding-reordered",
        "policy-binding-pair-drift",
        "policy-binding-scope-drift",
        "policy-binding-duplicate-source-different-tuple",
    } <= set(result["python_negative_cases"])


def test_request_failure_policy_binds_every_error_to_http_400() -> None:
    golden = json.loads(
        (ROOT / "contracts" / "golden" / "core-http-request-failure-v1" / "expected.json").read_text(
            encoding="utf-8"
        )
    )
    policy = parse_core_http_request_failure_policy(golden["policy"])
    errors = [parse_core_http_request_error(value) for value in golden["errors"]]
    assert policy["status"] == 400
    assert policy["retryable"] is False
    assert [item["error_code"] for item in errors] == [item["error_code"] for item in policy["bindings"]]
    assert all(item["retryable"] is False for item in errors)


def test_existing_core_http_v1_contract_bytes_remain_immutable() -> None:
    expected = {
        "contracts/json-schema/core-authority-command-query-v1.schema.json": "5b60a3edf630aa55c7313172c5ee20636969866e0c60cbd89adb075f0306153f",
        "contracts/json-schema/core-api-method-matrix.v1.json": "69afdda8043c3df9fac2fc1e909f741c290d6d19c1aa1a73e7d8862d5a705b4f",
        "contracts/json-schema/publication-command-result-v1.schema.json": "11d704a7c0bf72a8a6d6ccc620d1be85360262c0359ea75d0f3bb7df3a83d08c",
        "contracts/json-schema/asset-metadata-v1.schema.json": "49c49b8d3b6adf828ab0623c8d82df176a3d27a1be8fda349d2857ae8de20c6a",
        "contracts/golden/contract-publication-v1/core-http.json": "6831b14d1516fa807406e46fa911505ed32887e2db43e017947daa756b437995",
        "contracts/corpus/contract-publication-v1/negative.json": "55164bd67abd64d85f81149e9d5f2ffc2a60f69a2bb3f687979e44d8c9d3e14a",
    }
    assert {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in expected
    } == expected


def test_request_failure_family_is_content_addressed_once() -> None:
    manifest = json.loads((ROOT / "contracts" / "manifest-v1.json").read_text(encoding="utf-8"))
    families = manifest["contract_families"]
    assert families.count("core-http-request-error/v1") == 1
    assert families.count("core-http-request-failure-policy/v1") == 1
    required_paths = {
        "contracts/json-schema/core-http-request-error-v1.schema.json",
        "contracts/json-schema/core-http-request-failure-policy-v1.schema.json",
        "contracts/golden/core-http-request-failure-v1/expected.json",
        "contracts/corpus/core-http-request-failure-v1/negative.json",
    }
    records = {record["path"]: record for record in manifest["files"]}
    assert required_paths <= records.keys()
    for path in required_paths:
        raw = (ROOT / path).read_bytes()
        assert records[path]["bytes"] == len(raw)
        assert records[path]["sha256"] == hashlib.sha256(raw).hexdigest()
