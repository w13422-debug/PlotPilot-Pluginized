from __future__ import annotations
import hashlib
from typing import Callable, Mapping, Protocol, Any
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode, canonical_bytes
from backend.plotpilot_plugin_sdk.rpc import decode_frame

class AuthoritativeConnection(Protocol):
    """P1-owned connection already enclosed by its authoritative transaction."""
    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> Any: ...

class DurableOperationLedger:
    """Stateless P3 ledger logic; connection, transaction and migration ownership stay in P1."""
    def execute(self, connection: AuthoritativeConnection, *, context_identity: str, method: str,
                operation_key: str, payload: Mapping[str, Any], preflight: Callable[[], None],
                action: Callable[[AuthoritativeConnection], bytes]) -> bytes:
        # Frozen audit rule: fencing/context validation always precedes replay lookup.
        preflight()
        digest = hashlib.sha256(canonical_bytes(dict(payload))).hexdigest()
        row = connection.execute(
            "SELECT payload_hash,response_frame FROM p3_host_operation_ledger WHERE context_identity=? AND method=? AND operation_key=?",
            (context_identity, method, operation_key),
        ).fetchone()
        if row is not None:
            if row[0] != digest:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "operation key reused with a different payload")
            frame = bytes(row[1]); decode_frame(frame); return frame
        frame = action(connection)
        if not isinstance(frame, bytes):
            raise ContractError(ErrorCode.ASSET_ERROR, "operation action must return a complete response frame")
        decode_frame(frame)
        connection.execute(
            "INSERT INTO p3_host_operation_ledger(context_identity,method,operation_key,payload_hash,response_frame) VALUES(?,?,?,?,?)",
            (context_identity, method, operation_key, digest, frame),
        )
        return frame
