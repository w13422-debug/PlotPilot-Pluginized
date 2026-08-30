from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable, Mapping

from backend.plotpilot_core.events import EventRecoveryService, RecoveryPlan, SnapshotProvider
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode, canonical_bytes


@dataclass(frozen=True, slots=True)
class StreamCursor:
    stream_kind: str
    aggregate_id: str | None
    sequence: int

    def encode(self) -> str:
        if self.stream_kind == "core_event":
            return f"core/{self.sequence}"
        return f"job/{self.aggregate_id}/{self.sequence}"

    @classmethod
    def parse(
        cls,
        value: str | None,
        *,
        stream_kind: str,
        aggregate_id: str | None,
    ) -> StreamCursor:
        if value is None or value == "":
            return cls(stream_kind, aggregate_id, 0)
        if "\r" in value or "\n" in value:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "Last-Event-ID contains a line break")
        try:
            if stream_kind == "core_event":
                prefix, raw_sequence = value.split("/", 1)
                parsed_aggregate = None
            else:
                prefix, remainder = value.split("/", 1)
                parsed_aggregate, raw_sequence = remainder.rsplit("/", 1)
            if re.fullmatch(r"(?:0|[1-9][0-9]*)", raw_sequence) is None:
                raise ValueError("non-canonical sequence")
            sequence = int(raw_sequence)
        except (TypeError, ValueError) as exc:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "Last-Event-ID is not a typed event cursor") from exc
        expected_prefix = "core" if stream_kind == "core_event" else "job"
        if prefix != expected_prefix or parsed_aggregate != aggregate_id or sequence < 0:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "Last-Event-ID belongs to another event cursor domain")
        return cls(stream_kind, aggregate_id, sequence)


def _field(name: str, value: str) -> bytes:
    if "\r" in value or "\n" in value:
        raise ValueError(f"SSE {name} contains a line break")
    return f"{name}: {value}\n".encode("utf-8")


def encode_sse_event(
    data: Mapping[str, Any],
    *,
    event: str,
    cursor: StreamCursor | None = None,
    retry_ms: int | None = None,
) -> bytes:
    output = bytearray()
    if cursor is not None:
        output.extend(_field("id", cursor.encode()))
    output.extend(_field("event", event))
    if retry_ms is not None:
        if retry_ms < 0:
            raise ValueError("SSE retry must be non-negative")
        output.extend(_field("retry", str(retry_ms)))
    for line in canonical_bytes(data).decode("utf-8").splitlines() or [""]:
        output.extend(_field("data", line))
    output.extend(b"\n")
    return bytes(output)


def encode_cursor_advance(cursor: StreamCursor) -> bytes:
    return _field("id", cursor.encode()) + b": durable high-water\n\n"


@dataclass(frozen=True, slots=True)
class SSEReplayResponse:
    plan: RecoveryPlan
    frames: tuple[bytes, ...]
    next_cursor: StreamCursor

    @property
    def status_code(self) -> int:
        return 409 if self.plan.recovery["gap"] else 200

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }

    def body(self) -> Iterable[bytes]:
        return iter(self.frames)

    def as_response(self):
        """Create the exact Starlette 200-stream or 409-recovery response lazily."""
        from starlette.responses import JSONResponse, StreamingResponse

        if self.status_code == 409:
            return JSONResponse(
                dict(self.plan.recovery),
                status_code=409,
                headers={"Cache-Control": "no-cache, no-store"},
            )
        return StreamingResponse(
            self.body(), media_type="text/event-stream", headers=self.headers
        )

    def as_streaming_response(self):
        """Backward-friendly adapter name; a retention gap returns JSON 409."""
        return self.as_response()


class JobSSEAdapter:
    """Refresh-safe finite replay adapter for Core and per-Job event streams.

    Cursor IDs are domain-qualified.  A browser refresh can feed its exact
    Last-Event-ID back without allowing a Core cursor to be mistaken for a Job
    cursor.  A final cursor-only SSE field advances Last-Event-ID even when a
    filtered page contains no data events.
    """

    def __init__(self, recovery: EventRecoveryService, *, retry_ms: int = 2000) -> None:
        if retry_ms < 0:
            raise ValueError("SSE retry must be non-negative")
        self.recovery = recovery
        self.retry_ms = retry_ms

    def core_replay(
        self,
        *,
        last_event_id: str | None,
        after_seq: int | None = None,
        workspace_id: str | None = None,
        event_types: tuple[str, ...] = (),
        limit: int = 1000,
        snapshot_provider: SnapshotProvider | None = None,
    ) -> SSEReplayResponse:
        cursor = self._resolve_cursor(last_event_id, after_seq, "core_event", None)
        plan = self.recovery.core(
            cursor.sequence,
            workspace_id=workspace_id,
            event_types=event_types,
            limit=limit,
            snapshot_provider=snapshot_provider,
        )
        return self._response(plan)

    def job_replay(
        self,
        job_id: str,
        *,
        last_event_id: str | None,
        after_seq: int | None = None,
        limit: int = 1000,
        snapshot_provider: SnapshotProvider | None = None,
    ) -> SSEReplayResponse:
        cursor = self._resolve_cursor(last_event_id, after_seq, "job_event", job_id)
        plan = self.recovery.job(
            job_id,
            cursor.sequence,
            limit=limit,
            snapshot_provider=snapshot_provider,
        )
        return self._response(plan)

    @staticmethod
    def _resolve_cursor(
        last_event_id: str | None,
        after_seq: int | None,
        stream_kind: str,
        aggregate_id: str | None,
    ) -> StreamCursor:
        header = StreamCursor.parse(
            last_event_id, stream_kind=stream_kind, aggregate_id=aggregate_id
        )
        if after_seq is None:
            return header
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "after_seq must be a non-negative integer")
        if last_event_id not in {None, ""} and header.sequence != after_seq:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "after_seq conflicts with Last-Event-ID")
        return StreamCursor(stream_kind, aggregate_id, after_seq)

    def _response(self, plan: RecoveryPlan) -> SSEReplayResponse:
        kind = str(plan.recovery["stream_kind"])
        aggregate_id = plan.recovery["aggregate_id"]
        frames = [
            encode_sse_event(plan.recovery, event="recovery", retry_ms=self.retry_ms)
        ]
        if plan.snapshot is not None:
            frames.append(
                encode_sse_event(
                    plan.snapshot.value,
                    event="snapshot",
                    cursor=StreamCursor(kind, aggregate_id, plan.snapshot.high_water_seq),
                )
            )
        sequence_field = "core_event_seq" if kind == "core_event" else "job_event_seq"
        for value in plan.events:
            sequence = value.get(sequence_field)
            if isinstance(sequence, bool) or not isinstance(sequence, int):
                raise ContractError(ErrorCode.ASSET_ERROR, "stored event has no durable sequence")
            frames.append(
                encode_sse_event(
                    value,
                    event=kind,
                    cursor=StreamCursor(kind, aggregate_id, sequence),
                )
            )
        next_cursor = StreamCursor(kind, aggregate_id, plan.next_after_seq)
        frames.append(encode_cursor_advance(next_cursor))
        return SSEReplayResponse(plan, tuple(frames), next_cursor)


__all__ = [
    "JobSSEAdapter",
    "SSEReplayResponse",
    "StreamCursor",
    "encode_cursor_advance",
    "encode_sse_event",
]
