from __future__ import annotations
from typing import Any, Mapping, Protocol
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode, verify_snapshot
class JobAuthorityPort(Protocol):
    def find_by_request_key(self,workspace_id:str,request_key:str)->Mapping[str,Any]|None: ...
    def create_from_verified_snapshot(self,job_id:str,snapshot:Mapping[str,Any])->Mapping[str,Any]: ...
def resolve_request(authority:JobAuthorityPort,*,job_id:str,snapshot:dict[str,Any])->tuple[Mapping[str,Any],bool]:
    verify_snapshot(snapshot)
    existing=authority.find_by_request_key(snapshot["workspace_id"],snapshot["request_key"])
    if existing is not None:
        if existing["run_intent_id"]!=snapshot["run_intent_id"] or existing["run_snapshot_hash"]!=snapshot["snapshot_hash"]:
            raise ContractError(ErrorCode.DUPLICATE_REQUEST,"request key is bound to a different RunSnapshot or intent")
        return existing,True
    return authority.create_from_verified_snapshot(job_id,snapshot),False
