"""Small, real composition root for the browser-only integration baseline."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from plotpilot_plugin_sdk.fake_provider import FakeProvider
from plotpilot_plugin_sdk.ports import FakeCoreAuthorityPort, FakeExecutionPort, FakePluginRuntimePort
from plotpilot_plugin_sdk.verifier import verify_contract_inventory


@dataclass
class P0Composition:
    """Only shared seams are assembled here; no plugin business writer is added."""

    core: FakeCoreAuthorityPort
    runtime: FakePluginRuntimePort
    execution: FakeExecutionPort
    provider: FakeProvider

    def diagnostics(self) -> dict[str, Any]:
        verify_contract_inventory()
        return {
            "status": "ok",
            "contract_version": "1.2.0",
            "contract_owner": "P0",
            "provider_mode": "fake-only-for-automatic-tests",
            "writer_boundary": "core-authority-only",
            "desktop_route": "disabled",
        }


def build_composition() -> P0Composition:
    return P0Composition(
        core=FakeCoreAuthorityPort(),
        runtime=FakePluginRuntimePort(),
        execution=FakeExecutionPort(),
        provider=FakeProvider(),
    )
