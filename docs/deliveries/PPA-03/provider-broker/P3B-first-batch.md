> **Generation 2 note:** This file is retained as source-seed history only. G2 identity, closure matrix and evidence are authoritative in `P3B-generation-2.md`.

# P3B Provider / Model / Broker 第一批纵切

## 交付身份

- 项目：PPA-03B-Provider-Broker（P3B）
- 分支：`codex/ppa-03b-provider-broker`
- 精确 base：`1b352be671e70a2ce443b10499060982e93d64b7`
- donor-local push：`DISABLED`
- 交付类型：source candidate；实现、测试与证据同一 source-only commit，不创建 evidence-only HEAD。

本批只使用 P3B 独占写集：Model、Broker、API v1 `models/**`、OpenAI-compatible first-party plugin、P3B 定向测试及本目录的交付/协调证据。

## 第一批结果

### Model Profile / Provider 配置

- `ModelProfileRevision`、`ProviderConfig`、`ProviderIdentity`、`Endpoint`、`ModelName`、`SecretRef` 为不可变值对象。
- revision 使用 SDK canonical/JCS hash；外部 `revision_hash` 与内容不一致时拒绝。
- provider 配置只保留 `api_key_ref`，递归拒绝 secret-bearing/raw-key 字段；endpoint 禁止 userinfo、query、fragment。
- `InMemoryModelProfileRepository` 与注入式 `SQLiteModelProfileRepository` 提供 append-only history；SQLite update/delete trigger fail-closed。
- API DTO 只投影 secret reference，不引入 FastAPI/Pydantic 或修改公共合同。

### Capability Broker

- `BrokerInvocationEnvelope`、`BrokerChildRecord`、invoke/poll/cancel DTO 均为 closed、frozen、hash/contract-bound 值。
- Broker 只通过注入的 Core Asset、Attempt、Runtime、Descriptor、Child Factory、Execution、Projection、Ledger、Receipt ports 编排；不导入或直接修改 Job/Event/Candidate/Publication authority。
- invoke 在 child factory/attestation 前置条件不足时拒绝；同 operation key 重放复用原 child；ledger/child 任一侧缺失时 fail-closed，禁止重复创建。
- poll 使用 durable projection 并绑定 snapshot/result/receipt；cancel 使用冻结 binding 与 `child-cancel/v1` key，传播至多一次；required/optional aggregation 与 receipt propagation 不直接终结 parent Job。
- port 兼容调用在签名绑定后只调用一次，不捕获业务 `TypeError` 重试外部副作用。

### OpenAI-compatible first-party Provider

- `OpenAICompatibleProvider` 强制注入 transport；默认不创建 HTTP client、不打开真实网络。
- `DeterministicTransport`/`TestTransport` 提供离线 stream、非 stream、cancel、error、post-send uncertain 与 query recovery 场景。
- ProviderInvocation/ModelReceipt 递归冻结输入并严格重算 `request_hash`/`receipt_hash`；外部 hash drift 拒绝。
- API key 只来自一次性 invoke 参数或 SecretProvider reference lookup；headers/repr/error/receipt 安全视图 scrub secret。
- 发送前失败、发送后不确定窗口、query-capable 同 key recovery、stream cancellation、response/receipt persistence failure 均返回明确 provider-owned result/receipt；不自动切换 Provider/Model，不写 Core receipt/Job/Candidate/Publication。

## 越界修正与边界证据

派发前工作树为 clean；本批观察到的根 `README.md` 替换来自本批现场，已仅撤销该根路径的批内越界修改，并把相同 Provider README 内容放在
`first-party-plugins/provider-openai-compatible/README.md`。根 README 当前 hash 与 base `HEAD:README.md` 相同；最终审计要求 `ROOT_README_IN_DIFF=0`。

`backend/plotpilot_core/api/v1/` 当前只有授权的 `models/**`（`dto.py` 与 `__init__.py`），未写入其他 v1 API 子树。最终审计要求 `API_V1_UNAUTHORIZED=[]`。

## 验证证据

原始命令输出存放在同目录 `evidence/raw/`：

1. `targeted-pytest.stdout.txt`：`python -B -m pytest -p no:cacheprovider tests/p3-execution/provider-broker -q`
2. `compileall.stdout.txt`：`python -m compileall -q backend/plotpilot_core/model backend/plotpilot_core/broker first-party-plugins/provider-openai-compatible/backend/src`
3. `manifest-validation.stdout.txt`：SDK `verify_manifest` 校验 plugin manifest。
4. `offline-smoke.stdout.txt`：注入 `DeterministicTransport` 的 stream、secret redaction 与 no-real-network smoke。
5. `git-diff-check.stdout.txt`：最终 staged diff whitespace 检查。
6. `write-set-audit.stdout.txt`：staged changed paths、root README、API v1 及 out-of-set 审计。

当前定向测试覆盖 26 项；最终原始结果以 `evidence/raw/targeted-pytest.stdout.txt` 为准。

## 状态与剩余依赖

这是实现交付证据，不是中央验收结论。独立 fresh Sol Max review、P0 `no-ff` 集成及其资格判断留给中央控制器；本交付不宣称 central PASS、Finding closure 或 merge eligibility。

第一批当时未记录代码 Delta；受限整改已明确记录生产 reservation/atomic-child composition adapter Delta，详见 `coordination/PPA-03/provider-broker/remaining-delta.md`。该 Delta 不授权 P3B 修改未拥有的 Jobs/Events/contracts/SDK 路径。

## 2026-08-27 restricted remediation addendum

The first source candidate was reviewed at blocked HEAD `8ccfb47214ee5f720afdec01acc03923bae68b79`. The single permitted remediation round addresses only P3B-SOL-F-001 through F-007; F-008 was withdrawn by the same reviewer and is untouched. Detailed counter-evidence is in `P3B-remediation-F001-F007.md`.

The remediation suite now contains 33 P3B tests; the parent P3 regression command contains 37 tests. Exact raw outputs and the final changed-path/write-set audit supersede the first-batch counts in the earlier section above.
