# §20 / §84.3 RPC method matrix

协议参数与结果均是第二阶段 closed validation；表格由 `rpc-method-matrix.v1.json` 生成，避免文档与机器清单漂移。

## Worker methods

`runtime.handshake`, `runtime.health`, `runtime.heartbeat`, `capability.describe`, `settings.validate`,
`migration.plan`, `migration.apply`, `migration.verify`, `job.start`, `job.resume`, `job.pause`,
`job.cancel`, `runtime.shutdown`。

## Host methods

`host.asset.read/v1`, `host.asset.create/v1`, `host.asset.upload.status/v1`, `host.model.invoke/v1`,
`host.capability.invoke/v1`, `host.capability.poll/v1`, `host.capability.cancel/v1`, `host.candidate.stage/v1`,
`host.checkpoint.commit/v1`, `host.stream.commit/v1`, `host.job.event/v1`, `host.job.await_user/v1`,
`host.job.complete/v1`, `host.log/v1`, `host.migration.lease.renew/v1`, `host.migration.lease.release/v1`。

## Closed field matrix

| method | meta profile | params fields | result fields |
|---|---|---|---|
| `capability.describe` | `control` | `capability_id` | `descriptor` |
| `host.asset.create/v1` | `install/attempt` | `operation_key`, `upload_id`, `offset`, `mime`, `total_size`, `expected_hash`, `chunk_hash`, `base64_chunk`, `final` | `upload_id`, `accepted_bytes`, `completed`, `asset_id` |
| `host.asset.read/v1` | `control/install/attempt` | `asset_id`, `offset`, `length` | `base64_chunk`, `next_offset`, `content_hash` |
| `host.asset.upload.status/v1` | `install/attempt` | `upload_id`, `expected_hash` | `accepted_bytes`, `completed`, `asset_id` |
| `host.candidate.stage/v1` | `attempt` | `operation_key`, `result_bundle_asset_id`, `input_snapshot_hash` | `accepted`, `staged_items`, `job_event_seq` |
| `host.capability.cancel/v1` | `attempt` | `operation_key`, `child_job_id`, `reason` | `accepted`, `terminal_known`, `child_state`, `child_job_event_seq` |
| `host.capability.invoke/v1` | `attempt` | `operation_key`, `binding_id`, `input_asset_id`, `parameters_asset_id`, `expected_result_contract`, `propagate_cancel` | `accepted`, `child_job_id`, `child_step_id`, `child_run_snapshot_asset_id`, `child_run_snapshot_hash`, `child_result_contract`, `child_job_event_seq` |
| `host.capability.poll/v1` | `attempt` | `child_job_id`, `after_job_event_seq` | `job_snapshot_asset_id`, `job_event_page_asset_id`, `next_job_event_seq`, `terminal`, `result_bundle_asset_id`, `provenance_receipt_id` |
| `host.checkpoint.commit/v1` | `attempt` | `operation_key`, `checkpoint_asset_id` | `accepted`, `checkpoint_id`, `completed_units`, `total_units`, `job_event_seq` |
| `host.job.await_user/v1` | `attempt` | `operation_key`, `worker_run_id`, `checkpoint_asset_id`, `prompt_asset_id`, `reason` | `accepted`, `attempt_state`, `step_state`, `job_state`, `job_event_seq` |
| `host.job.complete/v1` | `attempt` | `operation_key`, `worker_run_id`, `outcome`, `result_bundle_asset_id`, `candidate_stage_operation_key`, `terminal_detail_asset_id`, `local_seq` | `accepted`, `attempt_state`, `step_state`, `job_state`, `provenance_receipt_id`, `job_event_seq`, `core_event_high_water` |
| `host.job.event/v1` | `attempt` | `operation_key`, `event_type`, `payload_asset_id`, `local_seq` | `accepted`, `job_event_seq` |
| `host.log/v1` | `control/install/attempt` | `level`, `message`, `fields_asset_id`, `local_seq` | `accepted`, `dropped` |
| `host.migration.lease.release/v1` | `install` | `operation_key`, `db_lease_id`, `db_lease_epoch`, `owner_instance_id`, `reason` | `accepted`, `state` |
| `host.migration.lease.renew/v1` | `install` | `operation_key`, `db_lease_id`, `db_lease_epoch`, `owner_instance_id`, `requested_expires_at` | `accepted`, `db_lease_epoch`, `expires_at` |
| `host.model.invoke/v1` | `attempt` | `operation_key`, `invocation_id`, `invocation_key`, `model_profile_revision_id`, `request_asset_id`, `replay_policy` | `state`, `response_asset_id`, `receipt_id`, `uncertainty` |
| `host.stream.commit/v1` | `attempt` | `operation_key`, `stream_prefix_asset_id` | `accepted`, `stream_id`, `acked_prefix_seq`, `acked_bytes`, `acked_prefix_hash`, `job_event_seq` |
| `job.cancel` | `attempt` | `worker_run_id`, `reason` | `accepted`, `terminal_known`, `attempt_state` |
| `job.pause` | `attempt` | `worker_run_id`, `reason` | `accepted`, `checkpoint_asset_id` |
| `job.resume` | `attempt` | `capability_id`, `run_snapshot_asset_id`, `resume_of_attempt_id`, `checkpoint_asset_id`, `resume_intent_id`, `resume_reason`, `secrets` | `accepted`, `worker_run_id`, `provenance_receipt_id`, `output_streams` |
| `job.start` | `attempt` | `capability_id`, `run_snapshot_asset_id`, `checkpoint_asset_id`, `secrets` | `accepted`, `worker_run_id`, `provenance_receipt_id`, `output_streams` |
| `migration.apply` | `install` | `plan_id`, `db_lease_id`, `db_lease_epoch`, `owner_instance_id` | `applied_schema`, `receipt_hash` |
| `migration.plan` | `install` | `from_schema`, `to_schema`, `migration_manifest_hash` | `plan_id`, `steps`, `backward_compatible`, `requires_verified_backup` |
| `migration.verify` | `install` | `db_lease_id`, `db_lease_epoch`, `owner_instance_id`, `expected_schema` | `valid`, `schema_hash`, `errors` |
| `runtime.handshake` | `control/install` | `host_protocol`, `generation_id`, `plugin_release_id`, `data_generation_id` | `plugin_protocol`, `plugin_id`, `release_id`, `capabilities`, `worker_instance_id` |
| `runtime.health` | `control/install` | `probe_id`, `db_lease_id`, `db_lease_epoch`, `owner_instance_id` | `status`, `details_asset_id`, `checked_at` |
| `runtime.heartbeat` | `attempt` | `worker_instance_id`, `observed_at`, `local_seq` | notification / 无 response |
| `runtime.shutdown` | `control/install/attempt` | `reason`, `deadline_at` | `accepted` |
| `settings.validate` | `control/install` | `settings_revision_id`, `plugin_release_id`, `schema_hash`, `payload_asset_id` | `valid`, `evaluated_payload_hash`, `details_asset_id`, `errors` |

## Profile and transaction gates

- `control`: `protocol_version,generation_id,plugin_release_id,deadline_at,context="control",operation_id`。
- `install`: control common fields plus `install_operation_id,install_lease_epoch`。
- `attempt`: control common fields plus `job_id,step_id,attempt_id,lease_epoch`。
- 所有 install/attempt mutation 先 fencing，再查 operation ledger；Core mutation 与 operation row 同事务提交。
- `host.job.complete/v1` 只提交 Attempt 终态；Step/Job 由 Core 聚合。
- `host.log/v1` 可丢弃但仍先 fencing；`runtime.heartbeat` 不携带业务结果。
