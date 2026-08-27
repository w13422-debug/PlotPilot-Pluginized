from __future__ import annotations
import hashlib, sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator
from backend.plotpilot_plugin_sdk import canonical_bytes
from .errors import duplicate_request

class DurableOperationLedger:
    """Durable ACK-loss ledger storing the first complete response frame verbatim."""
    def __init__(self, database: str | Path = ":memory:") -> None:
        self.connection=sqlite3.connect(str(database), isolation_level=None); self.connection.row_factory=sqlite3.Row
        self.connection.execute("CREATE TABLE IF NOT EXISTS host_operation_ledger(context_identity TEXT NOT NULL,method TEXT NOT NULL,operation_key TEXT NOT NULL,payload_hash TEXT NOT NULL,response_frame BLOB NOT NULL,PRIMARY KEY(context_identity,method,operation_key))")
    @contextmanager
    def transaction(self)->Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try: yield self.connection
        except BaseException: self.connection.rollback(); raise
        else: self.connection.commit()
    def execute(self,*,context_identity:str,method:str,operation_key:str,payload:object,preflight:Callable[[],None],action:Callable[[sqlite3.Connection],bytes])->bytes:
        digest=hashlib.sha256(canonical_bytes(payload)).hexdigest()
        with self.transaction() as tx:
            row=tx.execute("SELECT payload_hash,response_frame FROM host_operation_ledger WHERE context_identity=? AND method=? AND operation_key=?",(context_identity,method,operation_key)).fetchone()
            if row is not None:
                if row["payload_hash"]!=digest: raise duplicate_request()
                return bytes(row["response_frame"])
            preflight(); frame=action(tx)
            if not isinstance(frame,bytes): raise TypeError("operation action must return the complete response frame as bytes")
            tx.execute("INSERT INTO host_operation_ledger VALUES(?,?,?,?,?)",(context_identity,method,operation_key,digest,frame)); return frame
    def close(self)->None: self.connection.close()
