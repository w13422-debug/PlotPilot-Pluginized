# ADR-042：M0 后 UI ingress、operation context 与 Export 输入的结构性收口

- **状态**：Accepted；P0 binding decision；随 `PPA-M1-CONTRACT-PUBLICATION-01` 物化
- **日期**：2026-08-27
- **Owner**：P0 / PPA-00-Integration
- **Deltas**：`PPA-04-CD-001`、`P3-CD-CONTEXT-IDENTITY-001`、`P6-CD-EXPORT-CURRENT-REVISIONS-001`
- **正式设计**：v1.2，SHA-256 `e70f450b75cfa753148cf620d13855d0073f7d61294e7599ea9cc98f3e612b7b`

## 决策动机与版本路线

仓库已有 `ADR-041`，因此下一号为 `ADR-042`。三项 Delta 都源于正式设计已经冻结但 M0 未提供可被下游直接消费的最后一段可执行口径。为避免把同一路线继续膨胀成多个 API，P0 用一份 scoped ADR 固定三条边界：补齐既有 UI v1 的 unknown-ingress 执行器；把 §20.5 的 opaque `context identity` 收敛成唯一派生规则；把 Export 所需 current revisions 物化为 Core-created immutable Asset，而不是修改已冻结的 Host RPC 清单。

本 ADR 不移动或改写任何 `M0-OPEN*` tag，不删除/改名既有 v1 字段，不改变 framing、error、lease、state 或 cursor。新增的 `operation-context-identity/v1` 与 `export-current-revisions/v1` 是正式设计缺失物化的首次 family；既有 `plugin-ui-*/v1` wire schema 不升 v2，只补齐与正式设计 §84.10 一致的 Python/TypeScript executable enforcement。发布后这些新 family 的字段变化遵循 §84.8，必须另发 v2。

## PPA-04-CD-001：existing v1 executable ingress

Delta 是真实的 SDK/verifier 完整性缺口，不是新的 wire schema 缺口。P0 保留既有：

- `job-snapshot/v1`；
- `plugin-ui-tree/v1`；
- `plugin-ui-intent/v1`；
- `plugin-ui-ack/v1`。

P0 TypeScript 必须从 `unknown` 开始解析，拒绝非 plain object、prototype-backed object、getter/setter、缺失/额外 own property、错误类型/pattern/enum、重复数组值和错误递归 discriminator；成功时返回 deep-cloned、deep-frozen 的 typed DTO。Tree 还执行正式设计 §84.10 的 component event allowlist：无事件组件只能是空数组，其他组件只能声明表中对应事件。

`open_core_operation` 只是请求 Core 原生控制路径，不授予 Plugin UI Bundle Publication 权限。P4 不得保留 private wire DTO 或绕过 P0 parser。

## P3-CD-CONTEXT-IDENTITY-001：唯一 canonical projection

Delta 是真实的跨项目语义缺口。RPC envelope 与 method matrix 不新增 `context_identity` 字段；P3 只能调用 P0 SDK 的派生函数。

closed projection 为 `operation-context-identity/v1`：

```text
common  = schema,protocol_version="1",context,generation_id,plugin_release_id
control = common
install = common + install_operation_id
attempt = common + job_id,step_id,attempt_id
```

唯一公式为：

```text
SHA256(UTF8("plotpilot-operation-context/v1\n") || JCS(projection))
```

`deadline_at`、request `operation_id`、`install_lease_epoch`、`lease_epoch` 不进入 projection。前两者是请求级可变值；epoch 是 fencing 值。install/attempt 必须先把当前 expected epoch 与 meta 比较，旧 epoch 在查询 operation ledger 之前返回 `1002 stale_lease`。把 epoch 放入 identity 会让旧 lease 对同一 operation key 建立第二条幂等记录，因此明确禁止。

## P6-CD-EXPORT-CURRENT-REVISIONS-001：拒绝新增 Host RPC

底层需求是真缺口，但 Delta 提议的 `host.current-revisions.read/v1` 不接受。正式设计 §20.2 的 Host 方法清单已在 M0 冻结；增加一个查询方法会修改既有 plugin RPC v1 method matrix。结构性简化为首次发布 `export-current-revisions/v1`：

```text
schema,workspace_id,core_snapshot_revision,generated_at,
ordered_revisions[
  ordinal,document_id,document_type,title,revision_id,
  content_asset_id,content_hash,mime="text/plain",encoding="utf-8"
]
```

Core 在冻结 Export RunSnapshot 前，从唯一权威 current pointers 生成该 closed JSON Asset。`ordinal` 从 0 连续，document/revision/content Asset identity 唯一；每项必须与同一 Workspace 的 RunSnapshot `input_revisions` 一致。该 Asset 作为 `parameters_asset_id` 并以精确 SHA-256 出现在 `asset_hashes`；正文 bytes 只通过既有 `host.asset.read/v1` 读取。插件不能提交 raw-bytes 私有 DTO、不能直连 Core HTTP、不能写 Revision 或调用 Publication。

P1 Asset/Core snapshot builder 与 P2/P3 framed runtime 尚未集成前，P6 Export runtime 继续停止；本 ADR 只发布 read input contract，不伪造可运行导出链。

## 验收与同步门

发布门必须同时通过：generator-backed closed schemas、Python verifier、真实 TypeScript parser、cross-language context vectors、Core HTTP/Publication/Asset positive+negative fixtures、P4 prototype/getter/recursive tree probes、Export RunSnapshot/Asset binding probes、manifest 内容寻址和 fresh checkout 黄金 LF/hash。

P4/P3/P6 只能在本批次经独立 Sol 聚焦复核且 P0 提交后，对新的 P0 integration head 执行 `git merge --ff-only`（或按中央单写者流程同步）并删除自己的替代 DTO/identity 拼接。P3 的已接受 first-slice HEAD 可排队合并，但 durable Host mutation composition 在同步本 ADR 前仍停止；P6 runtime 还必须等待 P1/P2/P3 真实依赖。
