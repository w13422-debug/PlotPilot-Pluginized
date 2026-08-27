# PPA-01 Batch 01 — Integration Ready

- Exact base: `42123d1a5126bb2bef31304b0498e2e7def9183e` (`M0-OPEN-R4`)
- Reviewed implementation head: `93a34093e25603c3e2553ff72bfd6469e033de4a`
- Branch: `codex/ppa-01-core`
- Donor push: `DISABLED`

本批次交付内部 Core 权威纵切：SQLite 单写事务、Workspace/Document/Node/Relation、不可变
Revision 与 current-pointer CAS、内容寻址 Asset，以及消费现有 `candidate-item/v1` 的 document
Candidate staging 和 Core-native internal Publication。Candidate staging 不改变正式正文；accept 在同一
事务内做 stale-base CAS，operation key 重试返回首次 Publication/Revision。

`PPA-01-CD-001` 已由 P0 以 `accepted_with_scoped_public_contract` 作出 binding decision。
`core-authority-command-query/v1`、`publication-command-result/v1`、`asset-metadata/v1` 三个
public family 尚未发布，因此公共 Core HTTP、公共 Publication adapter、公共 Asset metadata DTO
切片继续停止；内部 authority、deterministic internal migration receipt、Asset/Candidate 和 internal
Publication 不受影响。

本批次冻结并枚举以下四个提交：`752e2b6b`、`d6d354b6`、`82dab14c`、`93a34093`。

定向验证原始结果记录于 `batch-01-integration-ready.json`。P0 应以 no-ff 方式集成；P0 合同补丁
三个 public family 经 P0 发布并形成新集成 HEAD 后，P1 再 ff-only 同步并恢复受影响 API 切片。
