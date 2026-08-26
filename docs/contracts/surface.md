# v1.2 合同固定语义（§13.4 / §20 / §84）

## §13.4 Package digest

解包只接受普通文件；相对路径统一 `/`、Unicode NFC，拒绝空段、`.`、`..`、绝对路径、NUL、反斜杠与 casefold 重名。
每个文件按原始 bytes 计算 lowercase SHA-256，`files.sha256` 自身除外；按规范化路径 UTF-8 bytes 升序；每行精确为
`<64hex><两个 ASCII 空格><path>\n`，UTF-8、LF、末尾换行、无 BOM。

```text
package_hash = SHA256(UTF8("plotpilot-package/v1\n") || exact_files_sha256_bytes)
release_id   = SHA256(UTF8("plotpilot-release/v1\n" + plugin_id + "\n" + version + "\n" + package_hash + "\n"))
```

M0 package golden：`package_hash=987e80013fe0cddd463eb8976fd75b62dbabef4f8e0e321ae6ad82a54f09b068`，
`release_id=00d365d816a45108cac08b5dc26eae015a44401c04aed0e9d512ed8fb24d88dd`。
Skill 使用独立域前缀 `plotpilot-skill-package/v1\n` 与 `plotpilot-skill-release/v1\n`，golden 为
`skill_package_hash=5cf3df6acea3c792ee36fa3c0c5757f87c8219aba88cfc568bd4a67738983b7f`、
`skill_release_id=abe35d45644a2ebac3b3c914876c613342b8d5a3e2421334241a06ec887436b7`。

## §20 Stdio Framed JSON-RPC

- JSON-RPC 2.0、stdio、UTF-8、`Content-Length` framing；stdout 只允许协议帧，stderr 只作日志。
- header 精确为 ASCII `Content-Length: <decimal>\r\nContent-Type: application/json; charset=utf-8\r\n\r\n`；header 上限 8 KiB、body 上限 8 MiB。
- 禁止 batch、未知/重复 header、非 UTF-8、尾随 bytes；request ID 必须为 UUID。
- meta 只有 `control`、`install`、`attempt` 三个 closed profile；install/attempt 使用当前 lease epoch，旧 epoch 先返回 `1002 stale_lease`。
- `runtime.heartbeat` 是唯一 notification；持久 mutation 必须 Core durable commit 后 ACK。
- 同 `(context identity, method, operation_key)` 同 payload 返回 byte-equivalent response；不同 payload 返回 `1008 duplicate_request`。
- Asset upload 的首包必须带客户端生成的 `upload_id`；chunk 重试只接受完全一致的 offset/bytes/hash；final 同时校验总大小与整体 hash。

完整 29 个方法、字段闭合集和 profile 见 `method-matrix.md` 与 `contracts/json-schema/rpc-method-matrix.v1.json`。

## §84 family 规则

### 84.1 common/JCS

ID 为 1–128 ASCII namespaced ID/UUID；hash 为 lowercase 64 hex；UTC 只允许冻结的零毫秒或固定三位毫秒格式；
set-like 数组在 hash 前按规范键排序，Plan/Data/Skill/Bundle 等 ordered 数组保留业务顺序；所有 hashed JSON 使用 RFC 8785 JCS。

### 84.2–84.7 identity、RPC、snapshot、result、runtime

Manifest、Capability、Data Bundle、RPC envelope、RunSnapshot、Result/Candidate/Provenance、Event/SSE/Checkpoint、
Plan/Generation/Settings/Lifecycle 的 closed 字段与 hash/transaction 关系逐项由 schema、SDK verifier 和 fixture 共同固定。
插件不得直写 Core SQL、Publication、任意文件根或直连通道；Candidate 只能通过 Core staging/Publication 生命周期进入正式状态。

### 84.8–84.12 compatibility、backup、UI、Skill、Job

v1 reader append-only 保留历史 raw bytes/hash；future v2 只创建新 revision/staging，不原地迁移历史 evidence。
备份按 mode/epoch/Asset closure 校验，workspace backup 标记 plugin projection rebuild；UI 仅接受 whitelist tree/intent/ACK；
Skill receipt 具备四级独立归因；Job 的 Attempt/Step/Job terminal 与 Broker parent/child 由 Core 聚合。

### 84.13–84.14 negative 与 finding closure

`negative-golden.md` 对应 14 组断言；每组至少一个真实负例且由 Python verifier 执行。Finding closure ID 以正式设计 §84.14 为准，
不能用“页面存在”或单一正例替代合同负例。

## Error codes

| code | name | code | name |
|---:|---|---:|---|
|1001|incompatible_generation|1008|duplicate_request|
|1002|stale_lease|1009|uncertain_external_effect|
|1003|cancelled|1010|invalid_transition|
|1004|deadline_exceeded|1011|result_contract_mismatch|
|1005|asset_error|1012|data_interpreter_unavailable|
|1006|settings_invalid|1013|release_retiring|
|1007|migration_failed|1014|checkpoint_invalid|
