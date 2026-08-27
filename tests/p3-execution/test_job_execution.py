import json, sqlite3
from pathlib import Path
import pytest
from backend.plotpilot_core.jobs import ContractError, DurableOperationLedger, ErrorCode, resolve_request
from backend.plotpilot_core.jobs.states import ATTEMPT_EDGES, JOB_EDGES, STEP_EDGES, can_transition
from backend.plotpilot_plugin_sdk.rpc import decode_frame, encode_frame
MIGRATION=Path("backend/plotpilot_core/jobs/migrations/001_host_operation_ledger.sql")
class MemoryAuthority:
    """Test-only P1 port adapter; not production authority."""
    def __init__(self): self.rows={}
    def find_by_request_key(self,w,k): return self.rows.get((w,k))
    def create_from_verified_snapshot(self,j,s):
        row={"job_id":j,"run_intent_id":s["run_intent_id"],"run_snapshot_hash":s["snapshot_hash"]}; self.rows[(s["workspace_id"],s["request_key"])]=row; return row
def golden(): return json.loads(Path("contracts/golden/run-snapshot/snapshot.json").read_text(encoding="utf-8"))
def open_authority(path):
    c=sqlite3.connect(path); c.executescript(MIGRATION.read_text(encoding="utf-8")); c.commit(); return c
def frame(): return encode_frame({"jsonrpc":"2.0","id":"00000000-0000-4000-8000-000000000001","result":{"accepted":True,"job_event_seq":1}})
def test_frozen_state_edges():
    assert can_transition(ATTEMPT_EDGES,"created","running") and can_transition(ATTEMPT_EDGES,"running","suspended")
    assert not can_transition(ATTEMPT_EDGES,"suspended","running")
    assert can_transition(STEP_EDGES,"failed","running") and not can_transition(JOB_EDGES,"failed","running")
def test_golden_request_suppression_uses_m0_error_authority():
    a=MemoryAuthority(); s=golden(); first,d1=resolve_request(a,job_id="job-1",snapshot=s); second,d2=resolve_request(a,job_id="job-2",snapshot=s)
    assert not d1 and d2 and first==second
    a.rows[(s["workspace_id"],s["request_key"])]["run_intent_id"]="drift"
    with pytest.raises(ContractError) as caught: resolve_request(a,job_id="job-3",snapshot=s)
    assert caught.value.code==int(ErrorCode.DUPLICATE_REQUEST)
def test_preflight_precedes_committed_replay_and_current_replay_is_exact(tmp_path):
    db=tmp_path/"core.db"; c=open_authority(db); ledger=DurableOperationLedger(); calls={"n":0}; expected=frame(); payload={"outcome":"failed"}
    def action(_): calls["n"]+=1; return expected
    c.execute("BEGIN IMMEDIATE"); first=ledger.execute(c,context_identity="attempt-current",method="host.job.complete/v1",operation_key="op-1",payload=payload,preflight=lambda:None,action=action); c.commit()
    c.close(); c=sqlite3.connect(db); c.execute("BEGIN IMMEDIATE")
    with pytest.raises(ContractError) as stale: ledger.execute(c,context_identity="attempt-current",method="host.job.complete/v1",operation_key="op-1",payload=payload,preflight=lambda:(_ for _ in ()).throw(ContractError(ErrorCode.STALE_LEASE,"stale")),action=action)
    c.rollback(); assert stale.value.code==int(ErrorCode.STALE_LEASE)
    c.execute("BEGIN IMMEDIATE"); replay=ledger.execute(c,context_identity="attempt-current",method="host.job.complete/v1",operation_key="op-1",payload=payload,preflight=lambda:None,action=action); c.commit()
    assert replay==first==expected and decode_frame(replay)["result"]["accepted"] and calls["n"]==1
def test_payload_drift_and_non_frame_use_m0_errors(tmp_path):
    c=open_authority(tmp_path/"core.db"); ledger=DurableOperationLedger(); base=dict(context_identity="a",method="host.job.complete/v1",operation_key="op")
    c.execute("BEGIN IMMEDIATE"); ledger.execute(c,payload={"x":1},preflight=lambda:None,action=lambda _:frame(),**base); c.commit()
    c.execute("BEGIN IMMEDIATE")
    with pytest.raises(ContractError) as drift: ledger.execute(c,payload={"x":2},preflight=lambda:None,action=lambda _:frame(),**base)
    c.rollback(); assert drift.value.code==int(ErrorCode.DUPLICATE_REQUEST)
    c.execute("BEGIN IMMEDIATE")
    with pytest.raises(ContractError) as bad: ledger.execute(c,context_identity="b",method="host.job.complete/v1",operation_key="op",payload={"x":1},preflight=lambda:None,action=lambda _:b"not-a-frame")
    c.rollback(); assert bad.value.code==int(ErrorCode.ASSET_ERROR)
