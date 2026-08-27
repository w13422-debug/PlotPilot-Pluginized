"""Explicit integration gates for the future real P1/P3 adapters.

No fake implementation is provided here.  The runtime can be tested entirely
with frozen IDs and local bytes; a Core integration must satisfy this manifest
before it can stage Candidates or create Jobs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class PortGate:
    owner: str
    contract: str
    method: str
    purpose: str
    status: str = "pending-real-adapter"

    def as_dict(self) -> dict[str, str]:
        return {
            "owner": self.owner,
            "contract": self.contract,
            "method": self.method,
            "purpose": self.purpose,
            "status": self.status,
        }


_PORT_GATES = (
    PortGate("P1/CoreAuthority", "asset/v1", "read_asset", "read frozen input/replacement Assets"),
    PortGate("P1/CoreAuthority", "asset/v1", "create_asset", "persist immutable output/replay Assets"),
    PortGate("P1/CoreAuthority", "candidate/v1", "stage_candidate", "stage Skill output; never publish directly"),
    PortGate("P3/Execution", "job/v1", "start", "create a real Job/Attempt from frozen RunSnapshot"),
    PortGate("P3/Execution", "job/v1", "poll", "read durable Job events/results"),
    PortGate("P3/Execution", "job/v1", "cancel", "propagate cancellation through Core"),
)


def required_port_gates() -> tuple[PortGate, ...]:
    return _PORT_GATES


def port_gate_manifest() -> dict[str, Any]:
    return {
        "schema": "prompt-skill-runtime-port-gates/v1",
        "runtime": "com.plotpilot.prompt-skill-runtime",
        "status": "blocked-until-real-p1-p3-adapters",
        "gates": [gate.as_dict() for gate in _PORT_GATES],
        "fake_ports": False,
    }


def verify_port_gates(ports: Mapping[str, object] | object | None = None) -> tuple[PortGate, ...]:
    """Return gates, or fail closed when a supplied adapter misses a method."""

    if ports is None:
        return _PORT_GATES
    missing: list[str] = []
    for gate in _PORT_GATES:
        target: object | None
        if isinstance(ports, Mapping):
            target = ports.get(gate.owner)
        else:
            target = ports
        if target is None or not callable(getattr(target, gate.method, None)):
            missing.append(f"{gate.owner}.{gate.method}")
    if missing:
        raise RuntimeError("missing real P1/P3 port gates: " + ", ".join(missing))
    return _PORT_GATES
