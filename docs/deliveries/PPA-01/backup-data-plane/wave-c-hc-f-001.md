# WAVE-C-HC-F-001 Generation 2：集成 Core Workspace 投影

## 身份与范围

- Generation 2 branch：`codex/nw-p1-backup-data-wave-c-g2`
- exact parent：`f5e7feaee17ed7c331eeb63a5fe185df7e2e8b2f`
- parent tree：`86d74c2e84c590306751da8a8db19e2c7d12f1f8`
- 旧 source `7948da473f3e5066b13d3fe6239561572bce86b9` 标记为 `superseded-at-integration`，未改写旧分支或提交。

本提交只整改 `WAVE-C-HC-F-001`。Workspace mode 在同一注入 `BackupBarrier` 下，先使用 `sqlite3.Connection.backup` 在线冻结完整 Core 图，再从只读冻结图生成新的单 Workspace SQLite；投影完成即删除完整临时图。Full/data 继续保存完整在线 SQLite snapshot。

## 生产 authority 薄适配

- canonical schema 与 migration ledger 直接由 `CoreAuthorityRepository` 创建，因而与生产顺序 `CORE_MIGRATIONS` → `P3_JOB_MIGRATIONS` → `EXECUTION_MIGRATIONS` 一致；Backup 内没有复制 DDL。
- 接受并精确验证 30 个公开 schema objects、21 张分类表及 5 条 migration：`0001-core-authority`、`0002-candidate-publication`、`p3-jobs-001`、`0003-execution-authority`、`0004-execution-remediation`。未知 table/view/trigger/index/column/migration 均 fail closed。
- 基础 Workspace/document/node/revision/relation/candidate/publication closure 复用 `CandidateService` canonical identity。
- P3/ETX closure 按 `execution_job.workspace_id`、正式 Attempt context identity、生产 `_broker_context_identity`、Candidate/publication binding 和 broker child/operation/host-ledger identity 归属；跨 Workspace、孤立或多归属行 fail closed。
- `execution_core_event` 按 `workspace_id` 过滤，同时把源 `sqlite_sequence` 的全局 high-water 写入目标，避免后续 Core Event 序号回退。
- 投影发布前再次执行 canonical schema/ledger 校验、完整 soft-identity closure、`foreign_key_check` 与 `integrity_check`。
- Workspace CoreSnapshot state Asset 包含选定 Workspace 的基础 + P3/ETX 全闭包；SQLite BLOB 使用确定性 hex JSON 表示。Asset scanner/manifest/receipt 继续从投影 DB 与 state Asset 重算内容哈希闭包。

## 决定性覆盖

- 真实 `CoreAuthorityRepository` + `ExecutionAuthority` 双 Workspace fixture：两边均有 parent/child job、step、attempt、receipt、Job/Core event、outcome、Candidate/publication binding、broker operation/child record/child creation 与 host ledger。
- `ws-1` Workspace backup 成功；投影、新 root restore 与 CoreSnapshot state 仅含 `ws-1` 全闭包，`ws-2` sentinel、Asset、plugin DB/package 均排除。
- 5 条 migration ledger 顺序、30 schema objects、FK/integrity、源 Core Event sequence high-water 与恢复后 repository reopen/no-migration-drift 均精确断言。
- orphan broker operation、orphan host ledger、cross-Workspace broker child、未知 table/view/trigger/column/migration 均 fail closed 且不发布 destination/stage。
- 同一双 Workspace 源库的 data/full 模式仍保留完整 authority；既有 plugin contributor、nonreplace、内容哈希、Asset closure 测试继续通过。

## Execution receipt lineage follow-up

- follow-up exact parent：`77be6c1d7463559dcdfda5359fae3e25724afc74`（tree `9d290e7ff2deedd71c7286641ba4c8abc7cf7576`）。
- Receipt 第一遍继续验证 provenance receipt 自身、job/step/attempt identity，并收集完整 parsed receipt 与 `receipt_owner`。
- 清晰的第二遍逐一解析 `receipt_json.parent_receipt_ids`：每个 parent 必须对应现有 `execution_receipt` authority row，且 parent owner 必须与 referring receipt owner 完全相同；缺失或跨 Workspace lineage 在 destination 发布前 fail closed，不会把外 Workspace parent 偷偷带入投影。
- 决定性负例证明 orphan parent 与 `ws-1 -> ws-2` parent 均不发布 destination、清理 staging 且不改变源 authority；正例证明同 Workspace parent receipt 在 projection、独立新-root restore 与 `CoreAuthorityRepository` reopen 后完整保留，migration ledger 与 FK 不漂移。
- reviewer 输入证据：`C:\Users\Administrator\Desktop\Novel-Agent- (2)\novel-agent\cases\plotpilot-pluginized-seven-project-construction-20260826\evidence\reviews\wave-c-h-c-f001-g2-sol-block-v1.json`，SHA-256 `f5cdd2c8cd1fe836378a05f1d93c9614cd22bec54e84224028d85253f57d95b0`。

## 复用决策与 composition

选择“当前生产 authority + Python stdlib 薄适配”。现有 `CoreAuthorityRepository`、`ExecutionAuthority`、`CandidateService`、broker value objects/verifier、`SqliteCoreSnapshotAdapter`、`AssetStore` 与 backup verifier 已提供精确能力；外部代码没有额外复用收益且会引入未授权依赖。

真实 runtime composition 仍依赖 P0 注入 durable BackupBarrier/Core event high-water、P2 generation/release/package authority，以及 P3 quiesced plugin data roots；本提交未增加第二 authority，也未改公共合同/SDK/P0 路由。

## 状态

`review_pending=true`；`self_closed=false`；未发起 reviewer，未声明 Finding closed、中央 PASS 或 merge eligibility。
