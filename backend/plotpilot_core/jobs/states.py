from __future__ import annotations

ATTEMPT_EDGES = {
    "created": frozenset({"running"}),
    "running": frozenset(
        {"cancelling", "succeeded", "partial", "failed", "cancelled", "interrupted", "suspended", "fenced"}
    ),
    "cancelling": frozenset({"cancelled", "fenced"}),
}

STEP_EDGES = {
    "pending": frozenset({"running"}),
    "running": frozenset(
        {"waiting_user", "paused", "succeeded", "partial", "failed", "cancelled", "interrupted", "needs_attention"}
    ),
    "waiting_user": frozenset({"running"}),
    "paused": frozenset({"running"}),
    "partial": frozenset({"running"}),
    "failed": frozenset({"running"}),
    "interrupted": frozenset({"running"}),
    "needs_attention": frozenset({"running"}),
}

JOB_EDGES = {
    "queued": frozenset({"running"}),
    "running": frozenset(
        {"waiting_user", "paused", "cancelling", "succeeded", "partial", "failed", "needs_attention"}
    ),
    "waiting_user": frozenset({"running"}),
    "paused": frozenset({"running"}),
    "needs_attention": frozenset({"running"}),
    "cancelling": frozenset({"cancelled"}),
}

ATTEMPT_CLOSED = frozenset(
    {"succeeded", "partial", "failed", "cancelled", "interrupted", "suspended", "fenced"}
)
JOB_ACTIVE = frozenset({"queued", "running", "waiting_user", "paused", "cancelling", "needs_attention"})


def can_transition(edges: dict[str, frozenset[str]], current: str, target: str) -> bool:
    return target in edges.get(current, ())
