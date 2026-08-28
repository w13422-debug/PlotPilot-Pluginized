from __future__ import annotations

import inspect
from pathlib import Path

from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_core.supervisor.supervisor import PluginProcessSupervisor
from backend.plotpilot_plugin_sdk.rpc import METHOD_MATRIX


def test_terminal_receipt_requires_an_unpublished_internal_authority_seam():
    wire_fields = set(METHOD_MATRIX["methods"]["host.job.complete/v1"]["params"]["fields"])
    authority_parameters = inspect.signature(ExecutionAuthority.complete_attempt).parameters

    assert "provenance_receipt" not in wire_fields
    assert "provenance_receipt" in authority_parameters
    assert authority_parameters["provenance_receipt"].default is inspect.Parameter.empty


def test_missing_authority_and_supervisor_surfaces_remain_explicit_delta_gates():
    for name in ("append_job_event", "await_user", "cancel_job", "pause_job", "resume_job"):
        assert not hasattr(ExecutionAuthority, name)
    assert not hasattr(PluginProcessSupervisor, "request_worker")
    assert not hasattr(PluginProcessSupervisor, "respond_host_error")


def test_owned_adapters_do_not_create_a_second_job_authority():
    root = Path("backend/plotpilot_core")
    production = [
        root / "api/v1/jobs/rpc/command_query.py",
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
