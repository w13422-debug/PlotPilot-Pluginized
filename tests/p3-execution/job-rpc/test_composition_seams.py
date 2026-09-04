from __future__ import annotations

import inspect
from pathlib import Path

from backend.plotpilot_core.jobs.http_rpc import PreparedHostRpcResult
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_core.supervisor.supervisor import PluginProcessSupervisor
from backend.plotpilot_plugin_sdk.rpc import METHOD_MATRIX


def test_terminal_receipt_requires_an_unpublished_internal_authority_seam():
    wire_fields = set(
        METHOD_MATRIX["methods"]["host.job.complete/v1"]["params"]["fields"]
    )
    authority_parameters = inspect.signature(
        ExecutionAuthority.complete_attempt
    ).parameters

    assert "provenance_receipt" not in wire_fields
    assert "provenance_receipt" in authority_parameters
    assert authority_parameters["provenance_receipt"].default is inspect.Parameter.empty


def test_authority_remains_internal_and_supervisor_transport_seams_are_published():
    for name in (
        "append_job_event",
        "await_user",
        "cancel_job",
        "pause_job",
        "resume_job",
    ):
        assert not hasattr(ExecutionAuthority, name)
    assert not hasattr(PluginProcessSupervisor, "request_worker")
    assert hasattr(PluginProcessSupervisor, "send_worker_request")
    assert hasattr(PluginProcessSupervisor, "take_worker_response")
    assert hasattr(PluginProcessSupervisor, "respond_host_error")
    assert not hasattr(PluginProcessSupervisor, "peek_host_events")
    assert not hasattr(PluginProcessSupervisor, "dispose_host_event")
    assert inspect.signature(ExecutionAuthority.start_attempt).return_annotation in {
        None,
        "None",
    }


def test_owned_adapters_do_not_create_a_second_job_authority():
    root = Path("backend/plotpilot_core")
    production = [
        root / "api/v1/jobs/rpc/command_query.py",
        root / "api/v2/jobs/rpc/command_query.py",
        root / "jobs/http_rpc/chapter_handlers.py",
        root / "jobs/http_rpc/composition.py",
        root / "jobs/http_rpc/dispatcher.py",
    ]
    forbidden = (
        "sqlite3.connect",
        "CREATE TABLE",
        "INSERT INTO execution_",
        "UPDATE execution_",
        "DELETE FROM execution_",
        "repository._connection",
        "PublicationService",
    )
    for path in production:
        source = path.read_text(encoding="utf-8")
        assert not any(value in source for value in forbidden), path
    dispatcher = production[1].read_text(encoding="utf-8")
    assert ".drain_events(" not in dispatcher


def test_prepared_host_result_has_a_validation_failure_release_port():
    parameters = inspect.signature(PreparedHostRpcResult).parameters

    assert "abort" in parameters
    assert parameters["abort"].default is not inspect.Parameter.empty


def test_delta_records_attempt_binding_secrets_disposition_and_reconciliation():
    delta = Path(
        "coordination/PPA-03/job-rpc/NW-P3-JOB-RPC-02-composition-delta-v1.json"
    ).read_text(encoding="utf-8")
    for required in (
        "AttemptStartBinding",
        "one-shot secrets",
        "peek_host_events",
        "dispose_host_event",
        "canonical payload hash",
        "atomic created/replayed disposition",
    ):
        assert required in delta
