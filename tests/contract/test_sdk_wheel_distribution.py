"""Regression coverage for the SDK wheel's verifier schema payload."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import venv
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SDK_SOURCE = ROOT / "backend" / "plotpilot_plugin_sdk"
AUTHORITATIVE_SCHEMA_DIR = ROOT / "contracts" / "json-schema"
WHEEL_SCHEMA_PREFIX = "plotpilot_plugin_sdk-0.1.2.data/data/Lib/contracts/json-schema/"


def _temporary_root() -> Path:
    """Keep Windows wheel paths short while retaining one disposable root."""
    if os.name == "nt":
        system_drive = (os.environ.get("SystemDrive") or "C:").rstrip("\\/") + "\\"
        return Path(tempfile.mkdtemp(prefix="pp-sdk-wheel-", dir=system_drive))
    return Path(tempfile.mkdtemp(prefix="pp-sdk-wheel-"))


def _subprocess_env(temp_root: Path) -> dict[str, str]:
    temp_dir = temp_root / "subprocess-tmp"
    temp_dir.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PIP_CACHE_DIR": str(temp_root / "pip-cache"),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONUTF8": "1",
            "TEMP": str(temp_dir),
            "TMP": str(temp_dir),
            "TMPDIR": str(temp_dir),
        }
    )
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
    )
    assert result.returncode == 0, (
        f"command failed ({result.returncode}): {' '.join(command)}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return result


def _pep517_builder(*, env: dict[str, str], cwd: Path) -> str:
    """Return an interpreter with the local setuptools/wheel backend available."""
    candidate = os.environ.get("PLOTPILOT_PEP517_PYTHON", sys.executable)
    result = subprocess.run(
        [candidate, "-c", "import setuptools, wheel"],
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
    )
    if result.returncode:
        pytest.fail(
            "PEP 517 builder lacks setuptools/wheel; set "
            "PLOTPILOT_PEP517_PYTHON to the configured bundled Python.\n"
            f"stderr:\n{result.stderr}"
        )
    return candidate


def _materialize_source(temp_root: Path) -> Path:
    source_root = temp_root / "source"
    package_root = source_root / "backend" / "plotpilot_plugin_sdk"
    shutil.copytree(
        SDK_SOURCE,
        package_root,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "build", "dist", "*.egg-info"),
    )
    shutil.copytree(AUTHORITATIVE_SCHEMA_DIR, source_root / "contracts" / "json-schema")
    return package_root


def _venv_python(venv_root: Path) -> Path:
    return venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _probe_script(
    expected_schema_names: list[str],
    macro_positive: dict[str, object],
    integer_vectors: dict[str, object],
    integer_vector_source_sha256: str,
    model_provider_golden: dict[str, object],
) -> str:
    expected = json.dumps(expected_schema_names)
    macro = json.dumps(macro_positive, separators=(",", ":"))
    integers = json.dumps(integer_vectors, separators=(",", ":"))
    provider = json.dumps(model_provider_golden, separators=(",", ":"))
    return textwrap.dedent(
        f"""\
        import copy
        import hashlib
        import json
        import sys
        from pathlib import Path

        import plotpilot_plugin_sdk
        from plotpilot_plugin_sdk import (
            MODEL_PROVIDER_INVOKE_METHOD_V1,
            MODEL_PROVIDER_RESERVED_METHOD_IDS,
            get_model_provider_rpc_method_matrix_v2,
            model_provider_operation_digest_v2,
            parse_macro_planning,
            parse_model_provider_invoke_request_v2,
            parse_model_provider_invoke_result_v2,
            parse_model_profile_revision_v1,
            parse_project_planner_model_output_v1,
            parse_project_planner_runtime_input_v2,
            validate_model_provider_rpc_success_v2,
        )
        from plotpilot_plugin_sdk.errors import ContractError, ContractValidationError
        from plotpilot_plugin_sdk.m4_m5_http_v2 import validate_http_exchange
        from plotpilot_plugin_sdk.macro_planning_v2 import (
            JSON_MAX_SAFE_INTEGER,
            WIRE_WHITESPACE_CODEPOINTS,
            contains_wire_whitespace,
            has_non_wire_whitespace_character,
            is_wire_whitespace_character,
            model_profile_revision_hash,
            planner_runtime_input_hash,
        )
        from plotpilot_plugin_sdk.rpc import build_meta, build_request
        from plotpilot_plugin_sdk.verifier import SCHEMA_DIR, assert_valid, validate_rpc_request, validate_rpc_response

        expected = {expected}
        macro = json.loads({macro!r})
        integer_vectors = json.loads({integers!r})
        integer_vector_source_sha256 = {integer_vector_source_sha256!r}
        model_provider = json.loads({provider!r})
        schema_dir = Path(sys.prefix) / "Lib" / "contracts" / "json-schema"
        assert SCHEMA_DIR == schema_dir, (SCHEMA_DIR, schema_dir)
        actual = sorted(path.name for path in schema_dir.glob("*.json"))
        assert actual == expected, (actual, expected)

        release_id = "a" * 64
        request = build_request(
            "runtime.handshake",
            {{
                "host_protocol": "1",
                "generation_id": "generation-wheel",
                "plugin_release_id": release_id,
                "data_generation_id": "data-generation-wheel",
            }},
            build_meta(
                "control",
                generation_id="generation-wheel",
                plugin_release_id=release_id,
                deadline_at="2030-01-02T03:04:05Z",
                operation_id="wheel-schema-probe",
            ),
            request_id="123e4567-e89b-42d3-a456-426614174000",
        )
        validate_rpc_request(request)
        response = {{
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {{"code": 1001, "message": "incompatible", "data": None}},
        }}
        validate_rpc_response(response, request=request)

        def _expect_rejected(action):
            try:
                action()
            except ContractError:
                pass
            else:
                raise AssertionError("invalid RPC success response was accepted")

        assert MODEL_PROVIDER_INVOKE_METHOD_V1 == "model.provider.invoke/v1"
        assert MODEL_PROVIDER_RESERVED_METHOD_IDS == frozenset(
            {{"model.provider.invoke/v1"}}
        )
        provider_matrix = get_model_provider_rpc_method_matrix_v2()
        assert provider_matrix["reserved_method_ids"] == [
            "model.provider.invoke/v1"
        ]
        provider_matrix["reserved_method_ids"].append("forged.method/v1")
        assert get_model_provider_rpc_method_matrix_v2()["reserved_method_ids"] == [
            "model.provider.invoke/v1"
        ]
        provider_request = parse_model_provider_invoke_request_v2(
            model_provider["request"]
        )
        assert (
            model_provider_operation_digest_v2(provider_request)
            == model_provider["operation_digest"]
        )

        provider_resource_names = [
            "model-provider-rpc-method-matrix.v2.json",
            "model-provider-invoke-request-v2.schema.json",
            "model-provider-invoke-result-v2.schema.json",
            "model-provider-invoke-success-v2.schema.json",
        ]
        resource_dir = Path(plotpilot_plugin_sdk.__file__).resolve().parent / "resources"
        for name in provider_resource_names:
            assert (resource_dir / name).read_bytes() == (schema_dir / name).read_bytes()

        provider_terminal_states = []
        for state in ("receipted", "failed", "cancelled", "uncertain"):
            result = parse_model_provider_invoke_result_v2(
                model_provider["terminal_results"][state]
            )
            parsed = validate_model_provider_rpc_success_v2(
                model_provider["terminal_successes"][state],
                request=provider_request,
                canonical_receipt=model_provider["canonical_receipts"][state],
            )
            assert parsed == result
            provider_terminal_states.append(parsed["provider_terminal_state"])
        copied_provider_result = parse_model_provider_invoke_result_v2(
            model_provider["result"]
        )
        copied_provider_result["model_receipt_anchor"]["job_id"] = "mutated"
        assert model_provider["result"]["model_receipt_anchor"]["job_id"] == "job-1"

        def _job_request(method, request_id):
            params = {{
                "capability_id": "shared.worker.run/v1",
                "run_snapshot_asset_id": "run-snapshot-asset",
                "checkpoint_asset_id": None,
                "secrets": [],
            }}
            if method == "job.resume":
                params.update({{
                    "resume_of_attempt_id": "attempt-previous",
                    "resume_intent_id": "resume-intent-1",
                    "resume_reason": "retry",
                }})
            return build_request(
                method,
                params,
                build_meta(
                    "attempt",
                    generation_id="generation-wheel",
                    plugin_release_id=release_id,
                    deadline_at="2030-01-02T03:04:05Z",
                    operation_id=method + "-operation",
                    job_id="job-1",
                    step_id="step-1",
                    attempt_id="attempt-1",
                    lease_epoch=1,
                ),
                request_id=request_id,
            )

        validated_job_methods = []
        for method, request_id in (
            ("job.start", "123e4567-e89b-42d3-a456-426614174001"),
            ("job.resume", "123e4567-e89b-42d3-a456-426614174002"),
        ):
            job_request = _job_request(method, request_id)
            validate_rpc_request(job_request)
            job_response = {{
                "jsonrpc": "2.0",
                "id": job_request["id"],
                "result": {{
                    "accepted": True,
                    "worker_run_id": "worker-run-1",
                    "provenance_receipt_id": "receipt-1",
                    "output_streams": [],
                }},
            }}
            validate_rpc_response(job_response, request=job_request)
            validated_job_methods.append(method)

            _expect_rejected(lambda: validate_rpc_response(job_response))
            other_method = "job.resume" if method == "job.start" else "job.start"
            _expect_rejected(
                lambda: validate_rpc_response(job_response, other_method, request=job_request)
            )

            wrong_fields = dict(job_response)
            wrong_fields["result"] = dict(job_response["result"])
            wrong_fields["result"]["unexpected"] = "value"
            _expect_rejected(lambda: validate_rpc_response(wrong_fields, request=job_request))

            malformed_id = dict(job_response)
            malformed_id["id"] = "not-a-uuid"
            _expect_rejected(lambda: validate_rpc_response(malformed_id, request=job_request))

        try:
            assert_valid("unknown-contract/v1", {{}})
        except ContractValidationError:
            pass
        else:
            raise AssertionError("unknown contract ids must fail closed")

        macro_fixtures = macro["fixtures"]
        for value in macro_fixtures.values():
            parse_macro_planning(value)
        parsed_profile = parse_model_profile_revision_v1(
            macro_fixtures["model_profile_revision"]
        )
        parsed_profile["provider"]["model_name"] = "defensive-copy-probe"
        assert (
            macro_fixtures["model_profile_revision"]["provider"]["model_name"]
            == "planner-model-1"
        )
        parsed_runtime = parse_project_planner_runtime_input_v2(
            macro_fixtures["project_planner_runtime_input"]
        )
        parsed_runtime["targets"]["setting"]["document_id"] = "defensive-copy-probe"
        assert (
            macro_fixtures["project_planner_runtime_input"]["targets"]["setting"]["document_id"]
            == "document-setting"
        )
        parse_project_planner_model_output_v1(
            macro_fixtures["project_planner_model_output"]
        )

        assert JSON_MAX_SAFE_INTEGER == 9_007_199_254_740_991
        maximum_profile = copy.deepcopy(macro_fixtures["model_profile_revision"])
        maximum_profile["revision_number"] = JSON_MAX_SAFE_INTEGER
        maximum_profile["parent_revision_id"] = "revision-model-profile-parent-max-safe"
        maximum_profile["revision_hash"] = model_profile_revision_hash(maximum_profile)
        parse_model_profile_revision_v1(maximum_profile)
        unsafe_profile = copy.deepcopy(maximum_profile)
        unsafe_profile["revision_number"] = JSON_MAX_SAFE_INTEGER + 1
        _expect_rejected(lambda: model_profile_revision_hash(unsafe_profile))

        maximum_runtime = copy.deepcopy(macro_fixtures["project_planner_runtime_input"])
        maximum_runtime["writer_epoch"] = JSON_MAX_SAFE_INTEGER
        maximum_runtime["input_hash"] = planner_runtime_input_hash(maximum_runtime)
        parse_project_planner_runtime_input_v2(maximum_runtime)
        unsafe_runtime = copy.deepcopy(maximum_runtime)
        unsafe_runtime["writer_epoch"] = JSON_MAX_SAFE_INTEGER + 1
        _expect_rejected(lambda: planner_runtime_input_hash(unsafe_runtime))

        def _set_path(value, path, replacement):
            target = value
            for token in path[:-1]:
                target = target[token]
            target[path[-1]] = replacement

        def _value_at_path(value, path):
            current = value
            for token in path:
                current = current[token]
            return current

        assert integer_vectors["schema"] == "macro-planning-integer-representations/v1"
        assert integer_vectors["field_count"] == 8
        assert integer_vectors["vector_count"] == 34
        assert integer_vectors["accepted_count"] == 19
        assert integer_vectors["rejected_count"] == 15
        integer_results = []
        for vector in integer_vectors["vectors"]:
            value = copy.deepcopy(macro_fixtures[vector["fixture"]])
            _set_path(value, vector["path"], json.loads(vector["raw_token"]))
            accepted = False
            normalized = None
            canonical_hash = None
            try:
                if vector["fixture"] == "model_profile_revision":
                    before_hash = copy.deepcopy(value)
                    canonical_hash = model_profile_revision_hash(value)
                    assert value == before_hash
                    value["revision_hash"] = canonical_hash
                elif vector["fixture"] == "model_profile_revise_result":
                    before_hash = copy.deepcopy(value["revision"])
                    canonical_hash = model_profile_revision_hash(value["revision"])
                    assert value["revision"] == before_hash
                    value["revision"]["revision_hash"] = canonical_hash
                elif vector["fixture"] == "project_planner_runtime_input":
                    before_hash = copy.deepcopy(value)
                    canonical_hash = planner_runtime_input_hash(value)
                    assert value == before_hash
                    value["input_hash"] = canonical_hash
                before_parse = copy.deepcopy(value)
                parsed = parse_macro_planning(value)
                assert value == before_parse
                normalized = _value_at_path(parsed, vector["path"])
                accepted = True
            except ContractError:
                pass
            assert accepted is (vector["expected"] == "accept"), vector["case_id"]
            if accepted:
                assert type(normalized) is int, vector["case_id"]
                assert normalized == vector["normalized"], vector["case_id"]
            integer_results.append(
                {{
                    "case_id": vector["case_id"],
                    "field_id": vector["field_id"],
                    "raw_token": vector["raw_token"],
                    "accepted": accepted,
                    "normalized": normalized,
                    "canonical_hash": canonical_hash,
                }}
            )
        integer_vector_result_digest = hashlib.sha256(
            json.dumps(
                integer_results,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        assert integer_vector_result_digest == "d4cfe7ec27c052badd2f67723fa5a4123becd60b6c1be11ce37b07b58f00f13d"

        expected_wire_whitespace = tuple(
            [*range(0x0009, 0x000E)]
            + [*range(0x001C, 0x0021)]
            + [
                0x0085,
                0x00A0,
                0x1680,
                *range(0x2000, 0x200B),
                0x2028,
                0x2029,
                0x202F,
                0x205F,
                0x3000,
                0xFEFF,
            ]
        )
        assert WIRE_WHITESPACE_CODEPOINTS == expected_wire_whitespace
        assert len(expected_wire_whitespace) == 30
        for codepoint in expected_wire_whitespace:
            character = chr(codepoint)
            assert is_wire_whitespace_character(character)
            assert contains_wire_whitespace("left" + character + "right")
            assert not has_non_wire_whitespace_character(character)

        for index, codepoint in enumerate((0x0085, 0xFEFF, 0x00A0)):
            character = chr(codepoint)
            endpoint = copy.deepcopy(macro_fixtures["model_profile_revise_command"])
            endpoint["provider"]["endpoint"] += character + "suffix"
            _expect_rejected(lambda endpoint=endpoint: parse_macro_planning(endpoint))
            model_name = copy.deepcopy(macro_fixtures["model_profile_revise_command"])
            model_name["provider"]["model_name"] = character + "planner-model-1"
            _expect_rejected(lambda model_name=model_name: parse_macro_planning(model_name))
            error = copy.deepcopy(macro_fixtures["model_profile_error"])
            error["message"] = character
            _expect_rejected(lambda error=error: parse_macro_planning(error))
            output = copy.deepcopy(macro_fixtures["project_planner_model_output"])
            output[("setting", "bible", "outline")[index]] = character
            _expect_rejected(lambda output=output: parse_project_planner_model_output_v1(output))

        for exchange in macro["exchanges"]:
            validate_http_exchange(
                exchange["route_id"],
                exchange["request"],
                exchange["status"],
                exchange["response"],
                path_params=exchange["path_params"],
            )

        print(
            json.dumps(
                {{
                    "module_file": str(Path(plotpilot_plugin_sdk.__file__).resolve()),
                    "schema_dir": str(schema_dir),
                    "schema_names": actual,
                    "validated_job_methods": validated_job_methods,
                    "macro_fixture_count": len(macro_fixtures),
                    "macro_routes": [item["route_id"] for item in macro["exchanges"]],
                    "macro_json_max_safe_integer": JSON_MAX_SAFE_INTEGER,
                    "macro_integer_vector_count": len(integer_results),
                    "macro_integer_vector_source_sha256": integer_vector_source_sha256,
                    "macro_integer_vector_result_digest": integer_vector_result_digest,
                    "macro_wire_whitespace_codepoints": len(WIRE_WHITESPACE_CODEPOINTS),
                    "model_provider_method": MODEL_PROVIDER_INVOKE_METHOD_V1,
                    "model_provider_terminal_states": provider_terminal_states,
                    "model_provider_operation_digest": model_provider_operation_digest_v2(
                        provider_request
                    ),
                    "model_provider_resources": provider_resource_names,
                }},
                sort_keys=True,
            )
        )
        """
    )


def test_real_sdk_wheel_installs_authoritative_verifier_schemas() -> None:
    """Build, inspect, install, and execute the actual 0.1.2 SDK distribution."""
    expected_schema_names = sorted(path.name for path in AUTHORITATIVE_SCHEMA_DIR.glob("*.json"))
    assert expected_schema_names
    macro_positive = json.loads(
        (ROOT / "contracts" / "golden" / "macro-planning-host-v1" / "positive.json").read_text(
            encoding="utf-8"
        )
    )
    integer_vector_path = (
        ROOT
        / "contracts"
        / "corpus"
        / "macro-planning-host-v1"
        / "integer-representations.json"
    )
    integer_vector_bytes = integer_vector_path.read_bytes()
    integer_vectors = json.loads(integer_vector_bytes)
    integer_vector_source_sha256 = hashlib.sha256(integer_vector_bytes).hexdigest()
    assert integer_vector_source_sha256 == (
        "2d9c72efdf8f593deb399567557229f6bcbccad1c95e961d01e819809bbbf88b"
    )
    model_provider_golden = json.loads(
        (
            ROOT
            / "contracts"
            / "golden"
            / "model-provider-rpc-v2"
            / "invoke.json"
        ).read_text(encoding="utf-8")
    )

    temp_root = _temporary_root()
    try:
        env = _subprocess_env(temp_root)
        package_root = _materialize_source(temp_root)
        wheel_dir = temp_root / "wheel"
        wheel_dir.mkdir()
        builder = _pep517_builder(env=env, cwd=package_root)
        _run(
            [
                builder,
                "-m",
                "pip",
                "wheel",
                "--use-pep517",
                "--no-build-isolation",
                "--no-deps",
                "--no-index",
                "--wheel-dir",
                str(wheel_dir),
                ".",
            ],
            cwd=package_root,
            env=env,
        )

        wheels = sorted(wheel_dir.glob("plotpilot_plugin_sdk-0.1.2-*.whl"))
        assert len(wheels) == 1
        wheel = wheels[0]
        with zipfile.ZipFile(wheel) as archive:
            wheel_schema_names = sorted(
                member.removeprefix(WHEEL_SCHEMA_PREFIX)
                for member in archive.namelist()
                if member.startswith(WHEEL_SCHEMA_PREFIX)
            )
            assert wheel_schema_names == expected_schema_names
            for schema_name in expected_schema_names:
                assert archive.read(WHEEL_SCHEMA_PREFIX + schema_name) == (
                    AUTHORITATIVE_SCHEMA_DIR / schema_name
                ).read_bytes()

        venv_root = temp_root / "venv"
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(venv_root)
        venv_python = _venv_python(venv_root)
        _run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-index",
                str(wheel),
            ],
            cwd=temp_root,
            env=env,
        )

        probe = temp_root / "installed_probe.py"
        probe.write_text(
            _probe_script(
                expected_schema_names,
                macro_positive,
                integer_vectors,
                integer_vector_source_sha256,
                model_provider_golden,
            ),
            encoding="utf-8",
        )
        result = _run([str(venv_python), str(probe)], cwd=temp_root, env=env)
        proof = json.loads(result.stdout)
        assert Path(proof["schema_dir"]).resolve() == (
            venv_root / "Lib" / "contracts" / "json-schema"
        ).resolve()
        assert proof["schema_names"] == expected_schema_names
        assert Path(proof["module_file"]).resolve().is_relative_to(venv_root.resolve())
        assert proof["validated_job_methods"] == ["job.start", "job.resume"]
        assert proof["macro_fixture_count"] == 17
        assert proof["macro_json_max_safe_integer"] == 9_007_199_254_740_991
        assert proof["macro_integer_vector_count"] == 34
        assert proof["macro_integer_vector_source_sha256"] == (
            "2d9c72efdf8f593deb399567557229f6bcbccad1c95e961d01e819809bbbf88b"
        )
        assert proof["macro_integer_vector_result_digest"] == (
            "d4cfe7ec27c052badd2f67723fa5a4123becd60b6c1be11ce37b07b58f00f13d"
        )
        assert proof["macro_wire_whitespace_codepoints"] == 30
        assert proof["model_provider_method"] == "model.provider.invoke/v1"
        assert proof["model_provider_terminal_states"] == [
            "receipted",
            "failed",
            "cancelled",
            "uncertain",
        ]
        assert proof["model_provider_operation_digest"] == model_provider_golden[
            "operation_digest"
        ]
        assert proof["model_provider_resources"] == [
            "model-provider-rpc-method-matrix.v2.json",
            "model-provider-invoke-request-v2.schema.json",
            "model-provider-invoke-result-v2.schema.json",
            "model-provider-invoke-success-v2.schema.json",
        ]
        assert proof["macro_routes"] == [
            "model-secret.put",
            "model-profile.revise",
            "workspace-plan.select",
            "project-planning.get",
            "project-planning.start",
        ]
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
