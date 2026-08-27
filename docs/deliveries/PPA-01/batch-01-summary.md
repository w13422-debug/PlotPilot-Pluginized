# PPA-01 Batch 01 — Integration Ready

- Exact base: `42123d1a5126bb2bef31304b0498e2e7def9183e` (`M0-OPEN-R4`)
- Implementation head: `d6d354b6b116885dbe3e737fe9f2d973dce137bd`
- Branch: `codex/ppa-01-core`
- Donor push: `DISABLED`

本批次交付内部 Core 权威纵切：SQLite 单写事务、Workspace/Document/Node/Relation、不可变
Revision 与 current-pointer CAS、内容寻址 Asset，以及消费现有 `candidate-item/v1` 的 document
Candidate staging 和 Core-native internal Publication。Candidate staging 不改变正式正文；accept 在同一
事务内做 stale-base CAS，operation key 重试返回首次 Publication/Revision。

`PPA-01-CD-001` 当前为 **P0 adjudication in progress**。公共 Core HTTP DTO、插件可调用
Publication method、公共 Asset metadata DTO 均已停止；内部 authority、内部 migration receipt、
Asset/Candidate 和 internal Publication 不受影响。

定向验证原始结果记录于 `batch-01-integration-ready.json`。P0 应以 no-ff 方式集成；P0 合同补丁
进入集成分支后，P1 再 ff-only 同步并恢复受影响 API 切片。
