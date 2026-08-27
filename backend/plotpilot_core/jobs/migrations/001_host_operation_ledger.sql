CREATE TABLE p3_host_operation_ledger (
  context_identity TEXT NOT NULL,
  method TEXT NOT NULL,
  operation_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
  response_frame BLOB NOT NULL,
  PRIMARY KEY (context_identity, method, operation_key)
);
