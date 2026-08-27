# ADR-041：PPA-01-CD-001 的最小 Core HTTP / Publication 合同

- **状态**：Accepted；P0 binding decision；实现尚未发布
- **日期**：2026-08-27
- **Owner**：P0 / PPA-00-Integration
- **Delta**：`PPA-01-CD-001`
- **机器裁决**：`coordination/PPA-00/contract-decision-PPA-01-CD-001.json`
- **正式设计**：v1.2，SHA-256 `e70f450b75cfa753148cf620d13855d0073f7d61294e7599ea9cc98f3e612b7b`

## 编号与基线

正式设计 §77 已冻结 `ADR-001` 至 `ADR-040`。当前仓库没有独立 ADR 文件，因此本仓库为该正式设计之后的第一个补充 ADR 使用 `ADR-041`，不是重新占用 `ADR-001`。

裁决基线是 `M0-OPEN-R4` peeled commit `42123d1a5126bb2bef31304b0498e2e7def9183e`。M0 的 `contracts/manifest-v1.json`（SHA-256 `2866b8d0b36ec954ee8cb5c87374e0ca0a0c4acefd1901070cee0b45979d25e7`）、RPC method matrix 和 Plugin SDK ports 已逐项回读。它们只定义插件 RPC 的 Asset/Candidate 等窄面，没有 Core HTTP wire contract；当前 P0 工作树实际 diff 只有下游登记，尚未出现 Luna 的公共合同实现。

## 背景与裁决

正式设计 §2.1/§5/§8/§10 把作品、文档、节点、关系、版本、Asset、Candidate/Publication 和 Core API 固定在 Core；P1 任务书第 4 节又把 Workspace/Document/Node/Relation command/query、Revision CAS、Asset、Candidate/Publication 端口交给 P0 合同；P4 任务书第 6 节要求 P0 发布 TS contracts 和 HTTP fixtures。因此 PPA-01-CD-001-A/B/C 是真实的公共合同缺口，不是 P1 可以私有化的实现细节。PPA-01-CD-001-D 是内部实现细节。

M0 **没有发布过 Core HTTP wire family**。因此这次是首次发布三个 additive v1 family，而不是修改任何既有 M0 `v1` 字段或方法：

1. `core-authority-command-query/v1`；
2. `publication-command-result/v1`；
3. `asset-metadata/v1`（含 bounded read-range DTO）。

它们使用现有 Core API 兼容窗口 `>=1.0 <2.0` 和当前设计合同版本 `1.2.0`。这是对“缺失物化”的最小补齐，不是把插件 RPC 改成 HTTP，也不是升级整个插件生态。发布后，这三个 family 的任何字段、类型、hash、error 或 CAS 语义变化都必须按 §84.8 另发 v2 family 与 ADR。

当前内容寻址 manifest 要在新合同实现提交时加入新增 schema、DTO/fixture 和测试证据；M0-OPEN-R4 的历史 bytes/hash 只能通过旧 tag 保留，不能移动、删除或覆盖旧 tags，也不能改写正式设计。

## 最小 Core HTTP 面

所有 DTO 是 closed Draft 2020-12 JSON，复用 §84.1 的 ID/UTC/hash/JCS 规则。不存在任意 JSON map、通用 query language、任意 JSON patch 或插件 RPC alias。写命令带 `operation_key`；revision-affecting command 带显式 expected/base revision；unknown field、未知实体、跨 Workspace、staled CAS 和同 key 不同 payload 均 fail-closed。

### Authority command/query

公开的稳定实体 DTO 只包含：

- `workspace/v1`：`workspace_id,workspace_kind,title,status,current_plan_revision_id|null,created_at,updated_at,revision`；
- `document/v1`：`document_id,workspace_id,document_type,title,current_revision_id|null,created_at,updated_at,revision`；
- `node/v1`：`node_id,workspace_id,document_id|null,node_type,title,parent_node_id|null,position,current_revision_id|null,created_at,updated_at,revision`；
- `relation/v1`：`relation_id,workspace_id,relation_type,source_id,target_id,revision_id|null,created_at`；
- `revision/v1`：`revision_id,workspace_id,document_id|null,node_id|null,parent_revision_id|null,content_hash,created_by,source_candidate_id|null,created_at,revision_number,payload_schema`。

查询固定为：

```text
GET /api/v1/core/workspaces
GET /api/v1/core/workspaces/{workspace_id}
GET /api/v1/core/workspaces/{workspace_id}/documents
GET /api/v1/core/documents/{document_id}
GET /api/v1/core/workspaces/{workspace_id}/nodes
GET /api/v1/core/workspaces/{workspace_id}/relations
GET /api/v1/core/documents/{document_id}/revisions
GET /api/v1/core/nodes/{node_id}/revisions
GET /api/v1/core/revisions/{revision_id}
GET /api/v1/core/revisions/{revision_id}/content?offset=&length=
```

命令固定为：

```text
POST   /api/v1/core/workspaces
PATCH  /api/v1/core/workspaces/{workspace_id}
DELETE /api/v1/core/workspaces/{workspace_id}
POST   /api/v1/core/workspaces/{workspace_id}/documents
PATCH  /api/v1/core/documents/{document_id}
POST   /api/v1/core/workspaces/{workspace_id}/nodes
PATCH  /api/v1/core/nodes/{node_id}
DELETE /api/v1/core/nodes/{node_id}
POST   /api/v1/core/workspaces/{workspace_id}/relations
DELETE /api/v1/core/relations/{relation_id}
POST   /api/v1/core/documents/{document_id}/revisions
POST   /api/v1/core/nodes/{node_id}/revisions
```

每个命令/结果必须有自己的 closed request/result schema；不能以 `payload: object` 逃避字段冻结。实体 DTO 不内联无界正文，正文通过 bounded revision-content page 读取。

### Core-native Publication

唯一公共 accept command 是：

```text
POST /api/v1/core/publications/accept
```

request 为 `publication-command/v1`：`publication_operation_key,workspace_id,candidate_id,accepted_by`；result 为 `publication-result/v1`：`publication_id,candidate_id,workspace_id,entity_kind,entity_id,resulting_revision,idempotent`。

Core 必须从 durable Candidate 重装配并复核 payload hash、RunSnapshot/provenance/release identity、target/mutation/base/write-set、parent closure 和 publication eligibility；同 key 同 payload 返回第一次 Publication，CAS 与 Revision/Event/receipt 在 Core 同一事务完成。普通 `partial/failed/skipped` 不可发布，只有设计 §8.8 的 Core-generated `incomplete_stream` 例外可由作者显式接受。

Publication 不加入 `backend/plotpilot_plugin_sdk/ports.py`、`rpc-method-matrix.v1.json` 或 Plugin UI Bundle。Plugin UI 只能通过已安装 tree 的 `open_core_operation`/preview intent 请求 Core 原生控件；worker 不得直连 Core HTTP，Core 原生控件和服务端才是最终门禁。

### Asset metadata / range

```text
GET /api/v1/core/assets/{asset_id}
GET /api/v1/core/assets/{asset_id}/range?offset=&length=
```

`asset-metadata/v1` 必须返回 `asset_id,sha256,mime,size,logical_role,provenance,rebuildable`；range DTO 必须返回 `asset_id,offset,length,total_size,base64_chunk,next_offset|null,content_hash`，并验证非负边界、返回字节数和完整 SHA-256。Delta 只新增 HTTP 读取面，不新增公开 Asset upload/create；现有 `host.asset.read/v1`、`host.asset.create/v1` 不变。

## Migration 边界

P1 的 Core `schema_migration` receipt/DB record 是 P1 自己写集里的 deterministic internal evidence，不进入 P0 公共 manifest、Plugin SDK、Plugin UI 或 RPC matrix。现有 Plugin `migration.apply/v1` 的 install lease、`applied_schema` 和 `receipt_hash` 保持原语义；二者不能互换、不能把 Core receipt 伪装成插件 RPC response。

## 实施与验收门

当前没有 Luna 草案可验收，故公共合同 release verdict 为 **BLOCKED / not implemented**。Luna 只能在 P0 写集内实现本 ADR 规定的 schema、Python/TypeScript DTO/validator、HTTP positive/negative fixtures、P1/P4 typed fakes 和 manifest/compatibility 记录；不能替 P1/P4 写业务、不能改 P0 既有 v1 RPC、不能修改正式设计或下游 worktree。

Sol 的接受门必须读取实际 diff，并运行原始输出可追溯的：Python verifier、真实 `frontend/src/contracts/verifier.ts`、cross-language fixtures、HTTP fixture 正负例和 schema/manifest checks；证明插件 SDK/RPC 没有 Publication callable，证明 stale/cross-workspace/unknown-field/idempotency/partial-publication/Asset-range negative 全部 fail-closed。实现缺一项都不进入 P0 发布基线。

## 同步顺序

1. P1 先提交不依赖新公共合同的 internal authority 批次；公共 API/Publication slice 继续停止。
2. P0 仅对真实 `integration-ready/v1` submission 校验 base、真实 diff、P1 写集和证据，然后 `git merge --no-ff`。
3. P0 发布本 ADR 对应的三组 v1 contract，并经 Sol 聚焦复核。
4. P1/P4 在 clean worktree 上对新的 P0 integration head 使用 `git merge --ff-only`，之后才能恢复 public Core API/Publication 消费。
5. 任一实现若需要改变已冻结 v1 语义，停止切片并重新提交 Contract Delta；不得在业务代码中临时兼容。

