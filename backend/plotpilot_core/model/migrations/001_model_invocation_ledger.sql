CREATE TABLE model_invocation (
  context_identity TEXT NOT NULL CHECK(length(context_identity)=64 AND context_identity NOT GLOB '*[^0-9a-f]*'),
  method TEXT NOT NULL CHECK(method='host.model.invoke/v1'),
  operation_key TEXT NOT NULL,
  workspace_id TEXT NOT NULL REFERENCES workspace(workspace_id),
  job_id TEXT NOT NULL REFERENCES execution_job(job_id),
  step_id TEXT NOT NULL REFERENCES execution_step(step_id),
  attempt_id TEXT NOT NULL REFERENCES execution_attempt(attempt_id),
  lease_epoch INTEGER NOT NULL CHECK(lease_epoch BETWEEN 1 AND 9007199254740991),
  caller_plugin_id TEXT NOT NULL,
  caller_plugin_release_id TEXT NOT NULL CHECK(length(caller_plugin_release_id)=64 AND caller_plugin_release_id NOT GLOB '*[^0-9a-f]*'),
  caller_plugin_package_hash TEXT NOT NULL CHECK(length(caller_plugin_package_hash)=64 AND caller_plugin_package_hash NOT GLOB '*[^0-9a-f]*'),
  generation_id TEXT NOT NULL REFERENCES p2_plugin_generation(generation_id),
  run_snapshot_id TEXT NOT NULL,
  run_snapshot_asset_id TEXT NOT NULL,
  run_snapshot_hash TEXT NOT NULL CHECK(length(run_snapshot_hash)=64 AND run_snapshot_hash NOT GLOB '*[^0-9a-f]*'),
  plan_revision_id TEXT NOT NULL,
  plan_revision_hash TEXT NOT NULL CHECK(length(plan_revision_hash)=64 AND plan_revision_hash NOT GLOB '*[^0-9a-f]*'),
  model_profile_revision_id TEXT NOT NULL REFERENCES p1_model_profile_revision(revision_id),
  model_profile_revision_hash TEXT NOT NULL CHECK(length(model_profile_revision_hash)=64 AND model_profile_revision_hash NOT GLOB '*[^0-9a-f]*'),
  provider_plugin_id TEXT NOT NULL,
  provider_release_id TEXT NOT NULL CHECK(length(provider_release_id)=64 AND provider_release_id NOT GLOB '*[^0-9a-f]*'),
  invocation_id TEXT NOT NULL,
  invocation_key TEXT NOT NULL,
  replay_policy TEXT NOT NULL CHECK(replay_policy IN ('idempotent_auto','manual_if_unknown','never_replay')),
  host_request_hash TEXT NOT NULL CHECK(length(host_request_hash)=64 AND host_request_hash NOT GLOB '*[^0-9a-f]*'),
  host_request_json TEXT NOT NULL CHECK(json_valid(host_request_json)),
  source_request_asset_id TEXT NOT NULL,
  source_request_asset_hash TEXT NOT NULL CHECK(length(source_request_asset_hash)=64 AND source_request_asset_hash NOT GLOB '*[^0-9a-f]*'),
  state TEXT NOT NULL CHECK(state IN ('reserved','dispatching','received','failed','uncertain')),
  provider_request_json TEXT CHECK(provider_request_json IS NULL OR json_valid(provider_request_json)),
  provider_success_json TEXT CHECK(provider_success_json IS NULL OR json_valid(provider_success_json)),
  provider_transport_request_hash TEXT CHECK(provider_transport_request_hash IS NULL OR (length(provider_transport_request_hash)=64 AND provider_transport_request_hash NOT GLOB '*[^0-9a-f]*')),
  provider_transport_response_hash TEXT CHECK(provider_transport_response_hash IS NULL OR (length(provider_transport_response_hash)=64 AND provider_transport_response_hash NOT GLOB '*[^0-9a-f]*')),
  response_asset_id TEXT,
  response_asset_hash TEXT CHECK(response_asset_hash IS NULL OR (length(response_asset_hash)=64 AND response_asset_hash NOT GLOB '*[^0-9a-f]*')),
  model_receipt_id TEXT,
  receipt_asset_id TEXT,
  receipt_asset_hash TEXT CHECK(receipt_asset_hash IS NULL OR (length(receipt_asset_hash)=64 AND receipt_asset_hash NOT GLOB '*[^0-9a-f]*')),
  receipt_hash TEXT CHECK(receipt_hash IS NULL OR (length(receipt_hash)=64 AND receipt_hash NOT GLOB '*[^0-9a-f]*')),
  host_result_json TEXT CHECK(host_result_json IS NULL OR json_valid(host_result_json)),
  rpc_error_json TEXT CHECK(rpc_error_json IS NULL OR json_valid(rpc_error_json)),
  uncertainty_json TEXT CHECK(uncertainty_json IS NULL OR json_valid(uncertainty_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(context_identity,method,operation_key),
  FOREIGN KEY(job_id,step_id) REFERENCES execution_step(job_id,step_id),
  FOREIGN KEY(generation_id,plan_revision_id) REFERENCES p1_plan_revision_reference(generation_id,plan_revision_id),
  CHECK((response_asset_id IS NULL)=(response_asset_hash IS NULL)),
  CHECK(
    (state='reserved' AND provider_request_json IS NULL AND provider_success_json IS NULL
      AND provider_transport_request_hash IS NULL AND provider_transport_response_hash IS NULL
      AND response_asset_id IS NULL AND model_receipt_id IS NULL AND receipt_asset_id IS NULL
      AND receipt_asset_hash IS NULL AND receipt_hash IS NULL AND host_result_json IS NULL
      AND rpc_error_json IS NULL AND uncertainty_json IS NULL)
    OR
    (state='dispatching' AND provider_request_json IS NOT NULL AND provider_success_json IS NULL
      AND provider_transport_request_hash IS NULL AND provider_transport_response_hash IS NULL
      AND response_asset_id IS NULL AND model_receipt_id IS NULL AND receipt_asset_id IS NULL
      AND receipt_asset_hash IS NULL AND receipt_hash IS NULL AND host_result_json IS NULL
      AND rpc_error_json IS NULL AND uncertainty_json IS NULL)
    OR
    (state IN ('received','failed') AND provider_request_json IS NOT NULL
      AND provider_success_json IS NOT NULL AND provider_transport_request_hash IS NOT NULL
      AND model_receipt_id IS NOT NULL AND receipt_asset_id IS NOT NULL
      AND receipt_asset_hash IS NOT NULL AND receipt_hash IS NOT NULL
      AND host_result_json IS NOT NULL AND rpc_error_json IS NULL AND uncertainty_json IS NULL
      AND (state<>'received' OR response_asset_id IS NOT NULL))
    OR
    (state='uncertain' AND provider_request_json IS NOT NULL AND uncertainty_json IS NOT NULL AND (
      (provider_success_json IS NOT NULL AND provider_transport_request_hash IS NOT NULL
        AND model_receipt_id IS NOT NULL AND receipt_asset_id IS NOT NULL
        AND receipt_asset_hash IS NOT NULL AND receipt_hash IS NOT NULL
        AND host_result_json IS NOT NULL AND rpc_error_json IS NULL)
      OR
      (provider_success_json IS NULL AND provider_transport_request_hash IS NULL
        AND provider_transport_response_hash IS NULL AND response_asset_id IS NULL
        AND model_receipt_id IS NULL AND receipt_asset_id IS NULL
        AND receipt_asset_hash IS NULL AND receipt_hash IS NULL
        AND host_result_json IS NULL AND rpc_error_json IS NOT NULL)
    ))
  )
);
CREATE UNIQUE INDEX model_invocation_workspace_invocation_id
  ON model_invocation(workspace_id,invocation_id);
CREATE UNIQUE INDEX model_invocation_workspace_invocation_key
  ON model_invocation(workspace_id,invocation_key);
CREATE INDEX model_invocation_state
  ON model_invocation(state,workspace_id,updated_at);
CREATE TRIGGER model_invocation_transition_guard
BEFORE UPDATE ON model_invocation
WHEN
  NEW.context_identity<>OLD.context_identity OR NEW.method<>OLD.method
  OR NEW.operation_key<>OLD.operation_key OR NEW.workspace_id<>OLD.workspace_id
  OR NEW.job_id<>OLD.job_id OR NEW.step_id<>OLD.step_id
  OR NEW.attempt_id<>OLD.attempt_id OR NEW.lease_epoch<>OLD.lease_epoch
  OR NEW.caller_plugin_id<>OLD.caller_plugin_id
  OR NEW.caller_plugin_release_id<>OLD.caller_plugin_release_id
  OR NEW.caller_plugin_package_hash<>OLD.caller_plugin_package_hash
  OR NEW.generation_id<>OLD.generation_id OR NEW.run_snapshot_id<>OLD.run_snapshot_id
  OR NEW.run_snapshot_asset_id<>OLD.run_snapshot_asset_id
  OR NEW.run_snapshot_hash<>OLD.run_snapshot_hash
  OR NEW.plan_revision_id<>OLD.plan_revision_id OR NEW.plan_revision_hash<>OLD.plan_revision_hash
  OR NEW.model_profile_revision_id<>OLD.model_profile_revision_id
  OR NEW.model_profile_revision_hash<>OLD.model_profile_revision_hash
  OR NEW.provider_plugin_id<>OLD.provider_plugin_id
  OR NEW.provider_release_id<>OLD.provider_release_id
  OR NEW.invocation_id<>OLD.invocation_id OR NEW.invocation_key<>OLD.invocation_key
  OR NEW.replay_policy<>OLD.replay_policy OR NEW.host_request_hash<>OLD.host_request_hash
  OR NEW.host_request_json<>OLD.host_request_json
  OR (OLD.state='dispatching' AND NEW.provider_request_json IS NOT OLD.provider_request_json)
  OR NEW.source_request_asset_id<>OLD.source_request_asset_id
  OR NEW.source_request_asset_hash<>OLD.source_request_asset_hash
  OR NEW.created_at<>OLD.created_at
  OR NOT (
    (OLD.state='reserved' AND NEW.state='dispatching')
    OR (OLD.state='dispatching' AND NEW.state IN ('received','failed','uncertain'))
  )
BEGIN
  SELECT RAISE(ABORT,'model invocation identity is immutable or transition is invalid');
END;
CREATE TRIGGER model_invocation_no_delete
BEFORE DELETE ON model_invocation
BEGIN
  SELECT RAISE(ABORT,'model invocation deletion is forbidden');
END;
