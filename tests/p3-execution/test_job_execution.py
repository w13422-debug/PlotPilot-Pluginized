import json
from pathlib import Path
import pytest
from backend.plotpilot_core.jobs import DurableOperationLedger, ExecutionError, resolve_request
from backend.plotpilot_core.jobs.states import ATTEMPT_EDGES, JOB_EDGES, STEP_EDGES, can_transition
class MemoryAuthority:
    """Test-only adapter; never used as production authority."""
    def __init__(self): self.rows={}
    def find_by_request_key(self,w,k): return self.rows.get((w,k))
    def create_from_verified_snapshot(self,j,s):
        row={"job_id":j,"run_intent_id":s["run_intent_id"],"run_snapshot_hash":s["snapshot_hash"]}; self.rows[(s["workspace_id"],s["request_key"])]=row; return row
def golden(): return json.loads(Path("contracts/golden/run-snapshot/snapshot.json").read_text(encoding="utf-8"))
def test_frozen_state_edges():
    assert can_transition(ATTEMPT_EDGES,"created","running") and can_transition(ATTEMPT_EDGES,"running","suspended")
    assert not can_transition(ATTEMPT_EDGES,"suspended","running")
    assert can_transition(STEP_EDGES,"failed","running") and not can_transition(JOB_EDGES,"failed","running")
def test_golden_request_suppression_and_binding_drift():
    a=MemoryAuthority(); s=golden(); first,d1=resolve_request(a,job_id="job-1",snapshot=s); second,d2=resolve_request(a,job_id="job-2",snapshot=s)
    assert not d1 and d2 and first==second
    a.rows[(s["workspace_id"],s["request_key"])]["run_intent_id"]="drift"
    with pytest.raises(ExecutionError) as caught: resolve_request(a,job_id="job-3",snapshot=s)
    assert caught.value.code==1008
def test_ledger_replays_exact_frame_after_expiry():
    ledger=DurableOperationLedger(); state={"live":True,"calls":0}; frame=b'{"jsonrpc":"2.0","id":"same","result":{"accepted":true}}\n'
    def fence():
        if not state["live"]: raise ExecutionError(1002,"stale_lease","expired")
    def action(_): state["calls"]+=1; return frame
    kwargs=dict(context_identity="attempt-1:epoch-1",method="host.job.complete/v1",operation_key="complete-1",payload={"outcome":"failed"},preflight=fence,action=action)
    assert ledger.execute(**kwargs)==frame; state["live"]=False; assert ledger.execute(**kwargs)==frame; assert state["calls"]==1
def test_ledger_drift_and_new_epoch_are_fenced():
    ledger=DurableOperationLedger(); base=dict(method="host.job.complete/v1",operation_key="complete-1")
    ledger.execute(context_identity="a:e1",payload={"x":1},preflight=lambda:None,action=lambda _:b"first",**base)
    with pytest.raises(ExecutionError) as drift: ledger.execute(context_identity="a:e1",payload={"x":2},preflight=lambda:None,action=lambda _:b"other",**base)
    assert drift.value.code==1008
    def stale(): raise ExecutionError(1002,"stale_lease","old")
    with pytest.raises(ExecutionError) as caught: ledger.execute(context_identity="a:e2",payload={"x":1},preflight=stale,action=lambda _:b"never",**base)
    assert caught.value.code==1002
