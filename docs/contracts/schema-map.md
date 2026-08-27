# 53 closed schema 映射

`contract_id`、路径和 `$id` 直接从当前 schema 文件读取；section 映射只标注正式设计 §84 的规范来源，不新增字段语义。

| contract_id | normative section | file | `$id` |
|---|---|---|---|
| `_common-v1` | `84.1` | `contracts/json-schema/_common-v1.schema.json` | `https://plotpilot.local/contracts/common-v1` |
| `artifact-item-v1` | `84.5` | `contracts/json-schema/artifact-item-v1.schema.json` | `https://plotpilot.local/contracts/artifact-item-v1` |
| `asset-metadata-v1` | `2.1 / 5 / 84.8` | `contracts/json-schema/asset-metadata-v1.schema.json` | `https://plotpilot.local/contracts/asset-metadata-v1` |
| `backup-bundle-v1` | `84.9` | `contracts/json-schema/backup-bundle-v1.schema.json` | `https://plotpilot.local/contracts/backup-bundle-v1` |
| `broker-child-record-v1` | `84.12` | `contracts/json-schema/broker-child-record-v1.schema.json` | `https://plotpilot.local/contracts/broker-child-record-v1` |
| `broker-invocation-v1` | `84.4 / 84.12` | `contracts/json-schema/broker-invocation-v1.schema.json` | `https://plotpilot.local/contracts/broker-invocation-v1` |
| `candidate-item-v1` | `84.5` | `contracts/json-schema/candidate-item-v1.schema.json` | `https://plotpilot.local/contracts/candidate-item-v1` |
| `capability-provider-v1` | `84.2` | `contracts/json-schema/capability-provider-v1.schema.json` | `https://plotpilot.local/contracts/capability-provider-v1` |
| `checkpoint-v1` | `84.6` | `contracts/json-schema/checkpoint-v1.schema.json` | `https://plotpilot.local/contracts/checkpoint-v1` |
| `compatibility-v1` | `84.8` | `contracts/json-schema/compatibility-v1.schema.json` | `https://plotpilot.local/contracts/compatibility-v1` |
| `core-authority-command-query-v1` | `2.1 / 5 / 84.8` | `contracts/json-schema/core-authority-command-query-v1.schema.json` | `https://plotpilot.local/contracts/core-authority-command-query-v1` |
| `core-event-v1` | `84.6` | `contracts/json-schema/core-event-v1.schema.json` | `https://plotpilot.local/contracts/core-event-v1` |
| `core-snapshot-v1` | `84.6` | `contracts/json-schema/core-snapshot-v1.schema.json` | `https://plotpilot.local/contracts/core-snapshot-v1` |
| `diagnostic-item-v1` | `84.5` | `contracts/json-schema/diagnostic-item-v1.schema.json` | `https://plotpilot.local/contracts/diagnostic-item-v1` |
| `export-current-revisions-v1` | `2.1 / 5 / 71 / 84.8` | `contracts/json-schema/export-current-revisions-v1.schema.json` | `https://plotpilot.local/contracts/export-current-revisions-v1` |
| `job-event-page-v1` | `84.6 / 84.12` | `contracts/json-schema/job-event-page-v1.schema.json` | `https://plotpilot.local/contracts/job-event-page-v1` |
| `job-snapshot-v1` | `84.6 / 84.12` | `contracts/json-schema/job-snapshot-v1.schema.json` | `https://plotpilot.local/contracts/job-snapshot-v1` |
| `operation-context-identity-v1` | `20.5 / 84.8` | `contracts/json-schema/operation-context-identity-v1.schema.json` | `https://plotpilot.local/contracts/operation-context-identity-v1` |
| `plugin-data-bundle-v1` | `84.2` | `contracts/json-schema/plugin-data-bundle-v1.schema.json` | `https://plotpilot.local/contracts/plugin-data-bundle-v1` |
| `plugin-generation-v1` | `84.7` | `contracts/json-schema/plugin-generation-v1.schema.json` | `https://plotpilot.local/contracts/plugin-generation-v1` |
| `plugin-job-event-v1` | `84.6` | `contracts/json-schema/plugin-job-event-v1.schema.json` | `https://plotpilot.local/contracts/plugin-job-event-v1` |
| `plugin-lifecycle-transition-v1` | `84.7` | `contracts/json-schema/plugin-lifecycle-transition-v1.schema.json` | `https://plotpilot.local/contracts/plugin-lifecycle-transition-v1` |
| `plugin-manifest-v1` | `84.2` | `contracts/json-schema/plugin-manifest-v1.schema.json` | `https://plotpilot.local/contracts/plugin-manifest-v1` |
| `plugin-plan-v1` | `84.7` | `contracts/json-schema/plugin-plan-v1.schema.json` | `https://plotpilot.local/contracts/plugin-plan-v1` |
| `plugin-ui-ack-v1` | `84.10` | `contracts/json-schema/plugin-ui-ack-v1.schema.json` | `https://plotpilot.local/contracts/plugin-ui-ack-v1` |
| `plugin-ui-dispose-v1` | `84.10` | `contracts/json-schema/plugin-ui-dispose-v1.schema.json` | `https://plotpilot.local/contracts/plugin-ui-dispose-v1` |
| `plugin-ui-error-v1` | `84.10` | `contracts/json-schema/plugin-ui-error-v1.schema.json` | `https://plotpilot.local/contracts/plugin-ui-error-v1` |
| `plugin-ui-event-v1` | `84.10` | `contracts/json-schema/plugin-ui-event-v1.schema.json` | `https://plotpilot.local/contracts/plugin-ui-event-v1` |
| `plugin-ui-init-v1` | `84.10` | `contracts/json-schema/plugin-ui-init-v1.schema.json` | `https://plotpilot.local/contracts/plugin-ui-init-v1` |
| `plugin-ui-intent-v1` | `84.10` | `contracts/json-schema/plugin-ui-intent-v1.schema.json` | `https://plotpilot.local/contracts/plugin-ui-intent-v1` |
| `plugin-ui-message-v1` | `84.10` | `contracts/json-schema/plugin-ui-message-v1.schema.json` | `https://plotpilot.local/contracts/plugin-ui-message-v1` |
| `plugin-ui-tree-v1` | `84.10` | `contracts/json-schema/plugin-ui-tree-v1.schema.json` | `https://plotpilot.local/contracts/plugin-ui-tree-v1` |
| `provenance-receipt-v1` | `84.5` | `contracts/json-schema/provenance-receipt-v1.schema.json` | `https://plotpilot.local/contracts/provenance-receipt-v1` |
| `publication-command-result-v1` | `8 / 10 / 84.8` | `contracts/json-schema/publication-command-result-v1.schema.json` | `https://plotpilot.local/contracts/publication-command-result-v1` |
| `release-pin-v1` | `84.7` | `contracts/json-schema/release-pin-v1.schema.json` | `https://plotpilot.local/contracts/release-pin-v1` |
| `release-retirement-v1` | `84.7` | `contracts/json-schema/release-retirement-v1.schema.json` | `https://plotpilot.local/contracts/release-retirement-v1` |
| `restore-report-v1` | `84.9` | `contracts/json-schema/restore-report-v1.schema.json` | `https://plotpilot.local/contracts/restore-report-v1` |
| `result-bundle-v1` | `84.5` | `contracts/json-schema/result-bundle-v1.schema.json` | `https://plotpilot.local/contracts/result-bundle-v1` |
| `rpc-envelope-v1` | `84.3` | `contracts/json-schema/rpc-envelope-v1.schema.json` | `https://plotpilot.local/contracts/rpc-envelope-v1` |
| `rpc-error-v1` | `84.3` | `contracts/json-schema/rpc-error-v1.schema.json` | `https://plotpilot.local/contracts/rpc-error-v1` |
| `rpc-notification-v1` | `84.3` | `contracts/json-schema/rpc-notification-v1.schema.json` | `https://plotpilot.local/contracts/rpc-notification-v1` |
| `rpc-request-v1` | `84.3` | `contracts/json-schema/rpc-request-v1.schema.json` | `https://plotpilot.local/contracts/rpc-request-v1` |
| `rpc-success-v1` | `84.3` | `contracts/json-schema/rpc-success-v1.schema.json` | `https://plotpilot.local/contracts/rpc-success-v1` |
| `run-snapshot-v1` | `84.4` | `contracts/json-schema/run-snapshot-v1.schema.json` | `https://plotpilot.local/contracts/run-snapshot-v1` |
| `settings-migration-manifest-v1` | `84.7` | `contracts/json-schema/settings-migration-manifest-v1.schema.json` | `https://plotpilot.local/contracts/settings-migration-manifest-v1` |
| `settings-revision-v1` | `84.7` | `contracts/json-schema/settings-revision-v1.schema.json` | `https://plotpilot.local/contracts/settings-revision-v1` |
| `settings-validation-receipt-v1` | `84.7` | `contracts/json-schema/settings-validation-receipt-v1.schema.json` | `https://plotpilot.local/contracts/settings-validation-receipt-v1` |
| `skill-chain-ref-v1` | `84.11` | `contracts/json-schema/skill-chain-ref-v1.schema.json` | `https://plotpilot.local/contracts/skill-chain-ref-v1` |
| `skill-chain-result-v1` | `84.11` | `contracts/json-schema/skill-chain-result-v1.schema.json` | `https://plotpilot.local/contracts/skill-chain-result-v1` |
| `skill-manifest-v1` | `84.11` | `contracts/json-schema/skill-manifest-v1.schema.json` | `https://plotpilot.local/contracts/skill-manifest-v1` |
| `skill-run-receipt-v1` | `84.11` | `contracts/json-schema/skill-run-receipt-v1.schema.json` | `https://plotpilot.local/contracts/skill-run-receipt-v1` |
| `sse-recovery-v1` | `84.6` | `contracts/json-schema/sse-recovery-v1.schema.json` | `https://plotpilot.local/contracts/sse-recovery-v1` |
| `stream-prefix-v1` | `84.6` | `contracts/json-schema/stream-prefix-v1.schema.json` | `https://plotpilot.local/contracts/stream-prefix-v1` |

## 非 schema 机器清单

| 文件 | 作用 | normative source |
|---|---|---|
| `contracts/json-schema/rpc-method-matrix.v1.json` | 29 个 worker/host 方法、profile、参数/结果字段及 14 错误码 | §20 / §84.3 |
| `contracts/json-schema/compatibility-matrix.v1.json` | Core API、plugin RPC、UI host、Python 兼容窗口 | §84.8 |
| `contracts/json-schema/core-api-method-matrix.v1.json` | P1/P4 Core HTTP route、request/result schema 与 success status 绑定 | §2.1 / §5 / §8 / §10 / §84.8 |

以上三个 JSON 不是 `*.schema.json`，但与 53 个 closed schema 一起进入 `contracts/manifest-v1.json` 的内容清单。
