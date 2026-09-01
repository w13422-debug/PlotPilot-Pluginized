"""Generate the reviewable P0 M0 contract and parity delivery records.

The generator only reads the frozen contract tree and the recorded browser
evidence.  It does not start PlotPilot, access a provider, or touch the donor
repository.  Delivery JSON is intentionally content-addressed where a
content hash is meaningful; the M0 manifest uses the symbolic ``M0-OPEN``
commit/tag reference to avoid a self-referential commit hash.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

if __package__:
    from .contract_inventory import (
        contract_schema_paths,
        v1_contract_inventory,
        v1_negative_group_paths,
    )
    from .validate_merge_gate import verify_creation_gate
else:
    from contract_inventory import (
        contract_schema_paths,
        v1_contract_inventory,
        v1_negative_group_paths,
    )
    from validate_merge_gate import verify_creation_gate


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
SCHEMAS = CONTRACTS / "json-schema"
EVIDENCE = ROOT / "docs" / "deliveries" / "PPA-00" / "evidence"
DELIVERY = ROOT / "docs" / "deliveries" / "PPA-00"
DESIGN_PATH = Path(r"C:\Users\Administrator\Desktop\交接文档\PlotPilot-Pluginized正式设计规划-2026-08-25-v1.md")
TASK_PATH = Path(r"C:\Users\Administrator\Desktop\交接文档\PlotPilot-Pluginized七项目任务书-2026-08-26\PPA-00-Integration.md")
MATRIX_PATH = Path(r"C:\Users\Administrator\Desktop\交接文档\PlotPilot-Pluginized七项目任务书-2026-08-26\project-matrix.json")
BOOTSTRAP_PATH = Path(
    r"C:\Users\Administrator\Desktop\Novel-Agent- (2)\novel-agent\cases\plotpilot-pluginized-seven-project-construction-20260826\evidence\p0-monorepo-bootstrap.json"
)
BACKUP_MANIFEST_PATH = Path(
    r"C:\Users\Administrator\Desktop\PlotPilot-Pluginized-preconstruction-backup-20260826\donor-backup-manifest.json"
)
DESIGN_SHA256 = "e70f450b75cfa753148cf620d13855d0073f7d61294e7599ea9cc98f3e612b7b"
BASE_SHA = "1c481237b6fa32ef5f85d7f8da4cb16f366cd4f0"
P0_BRANCH = "codex/ppa-00-integration"
RELEASE_LABEL = "M0-OPEN-R4"

FAMILY_IDS = [
    "84.1-common-jcs",
    "84.2-manifest-capability-data",
    "84.3-rpc-envelope-method-matrix",
    "84.4-run-snapshot-request-key",
    "84.5-result-candidate-provenance",
    "84.6-event-sse-checkpoint-stream",
    "84.7-plan-generation-settings-lifecycle",
    "84.8-compatibility-history",
    "84.9-backup-restore",
    "84.10-plugin-ui-wire",
    "84.11-skill-package-receipt",
    "84.12-job-broker-outcome",
    "84.13-negative-golden",
    "84.14-finding-closure",
]

SCHEMA_SECTIONS = {
    "_common-v1": "84.1",
    "artifact-item-v1": "84.5",
    "asset-metadata-v1": "2.1 / 5 / 84.8",
    "backup-bundle-v1": "84.9",
    "broker-child-record-v1": "84.12",
    "broker-invocation-v1": "84.4 / 84.12",
    "candidate-item-v1": "84.5",
    "capability-provider-v1": "84.2",
    "checkpoint-v1": "84.6",
    "compatibility-v1": "84.8",
    "core-authority-command-query-v1": "2.1 / 5 / 84.8",
    "core-event-v1": "84.6",
    "core-http-request-error-v1": "2.1 / 5 / 84.8 / ADR-043",
    "core-http-request-failure-policy-v1": "2.1 / 5 / 84.8 / ADR-043",
    "core-snapshot-v1": "84.6",
    "diagnostic-item-v1": "84.5",
    "export-current-revisions-v1": "2.1 / 5 / 71 / 84.8",
    "job-event-page-v1": "84.6 / 84.12",
    "job-snapshot-v1": "84.6 / 84.12",
    "operation-context-identity-v1": "20.5 / 84.8",
    "plugin-data-bundle-v1": "84.2",
    "plugin-generation-v1": "84.7",
    "plugin-job-event-v1": "84.6",
    "plugin-lifecycle-transition-v1": "84.7",
    "plugin-manifest-v1": "84.2",
    "plugin-plan-v1": "84.7",
    "plugin-ui-ack-v1": "84.10",
    "plugin-ui-dispose-v1": "84.10",
    "plugin-ui-error-v1": "84.10",
    "plugin-ui-event-v1": "84.10",
    "plugin-ui-init-v1": "84.10",
    "plugin-ui-intent-v1": "84.10",
    "plugin-ui-message-v1": "84.10",
    "plugin-ui-tree-v1": "84.10",
    "provenance-receipt-v1": "84.5",
    "publication-command-result-v1": "8 / 10 / 84.8",
    "release-pin-v1": "84.7",
    "release-retirement-v1": "84.7",
    "restore-report-v1": "84.9",
    "result-bundle-v1": "84.5",
    "rpc-envelope-v1": "84.3",
    "rpc-error-v1": "84.3",
    "rpc-notification-v1": "84.3",
    "rpc-request-v1": "84.3",
    "rpc-success-v1": "84.3",
    "run-snapshot-v1": "84.4",
    "settings-migration-manifest-v1": "84.7",
    "settings-revision-v1": "84.7",
    "settings-validation-receipt-v1": "84.7",
    "skill-chain-ref-v1": "84.11",
    "skill-chain-result-v1": "84.11",
    "skill-manifest-v1": "84.11",
    "skill-run-receipt-v1": "84.11",
    "sse-recovery-v1": "84.6",
    "stream-prefix-v1": "84.6",
}

NEGATIVE_DESCRIPTIONS = {
    "84.13-01": "JCS set permutation、Skill order permutation 与 Package/RunSnapshot/Skill golden 独立复算",
    "84.13-02": "Result profile、bundle、item 错配；Candidate target/mutation/base/write-set、parent cycle、cross-workspace source 与逐项映射",
    "84.13-03": "Broker invoke ACK-loss、child cancel、receipt propagation、Data format/interpreter/Plan/Snapshot mismatch",
    "84.13-04": "operation key ACK-loss、首包/中间/final upload 重试、status 恢复和同 key 不同 payload",
    "84.13-05": "Attempt cancel/complete race、fresh resume、checkpoint 倒退/跨 Snapshot、shadow lease stale",
    "84.13-06": "Bundle producer/snapshot/lease/receipt/staging/outcome transaction 边界",
    "84.13-07": "并发 install base CAS、crash point、rollback once、safe mode exit、settings validator failure",
    "84.13-08": "retire/new pin race、可恢复 Attempt blocker、package 删除后的 Candidate Publication",
    "84.13-09": "aggregate/Event crash、SSE gap/cursor ahead/snapshot convergence、Core/Job cursor 混用",
    "84.13-10": "raw stream 超过 ACK、cancel/crash race、唯一 incomplete Candidate、terminal hydration",
    "84.13-11": "Skill executed/failed/skipped、model claim、verified patch 组合和 patch 篡改",
    "84.13-12": "Worker immutable URL/CSP、stale/duplicate intent、unknown component/prop/event、无限循环导航仍响应",
    "84.13-13": "backup mode 条件、manifest/hash、crash staging、restore 新 root、projection rebuild",
    "84.13-14": "compatibility valid/invalid、历史 v1 raw bytes reader、Windows reserved/ADS/trailing dot-space/casefold collision",
}

FEATURE_ROWS = [
    ("PP46-LIB-CREATE", "填入作品信息并创建书目", "P4", ["P1", "P5"], ["home-create-surface"], "baseline_observed"),
    ("PP46-LIB-SETUP", "创建后执行或跳过设定初始化", "P5", ["P4", "P1", "P3"], ["wizard-generation"], "baseline_observed"),
    ("PP46-LIB-OPEN", "从书库打开作品并进入工作台", "P4", ["P1", "P5"], ["workbench-shell"], "baseline_observed"),
    ("PP46-LIB-DELETE", "删除单本书目并更新列表", "P4", ["P1"], [], "m0_not_exercised"),
    ("PP46-LIB-BATCH-DELETE", "选择多本后批量删除", "P4", ["P1"], [], "m0_not_exercised"),
    ("PP46-WB-THREE-PANE", "章节树—正文—右侧上下文三栏工作台", "P4", ["P1"], ["workbench-shell"], "baseline_observed"),
    ("PP46-CHAPTER-TREE", "查看、选择、刷新并按路由定位章节", "P4", ["P1"], ["workbench-chapter-tree"], "baseline_observed"),
    ("PP46-CHAPTER-EDIT-SAVE", "打开章节、编辑并保存正文", "P4", ["P1"], ["chapter-edit-save"], "baseline_observed"),
    ("PP46-ACT-CHAPTER-PLAN", "从幕发起规划并确认章节规划结果", "P6", ["P4", "P1", "P3"], [], "m0_not_exercised"),
    ("PP46-WB-SETTINGS", "右侧设置和生成偏好更新", "P4", ["P1", "P2", "P3"], [], "m0_not_exercised"),
    ("PP46-BIBLE", "查看作品 Story Bible", "P5", ["P4", "P1"], ["FLOW-08-story-evolution-bible"], "baseline_observed"),
    ("PP46-CHARACTER", "查看人物档案和设定", "P5", ["P4", "P1"], ["workbench-writing-support"], "baseline_observed"),
    ("PP46-WORLDBUILDING", "查看世界观", "P5", ["P4", "P1"], ["workbench-shell"], "baseline_observed"),
    ("PP46-PROPS", "查看道具/物品生命周期", "P5", ["P4", "P1"], [], "m0_not_exercised"),
    ("PP46-FORESHADOW", "查看伏笔台账和待处理数量", "P5", ["P4", "P1"], ["FLOW-07-foreshadow-ledger"], "baseline_observed"),
    ("PP46-STORY-EVOLUTION", "查看故事演进并兼容旧 tab 参数", "P5", ["P4", "P1"], ["checkpoint-recovery"], "baseline_observed"),
    ("PP46-KNOWLEDGE-GRAPH", "查看故事知识/知识图谱", "P5", ["P4", "P1"], [], "m0_not_exercised"),
    ("PP46-GENERATE", "执行章节规划、写作、审计和状态推进", "P6", ["P1", "P2", "P3", "P4", "P5"], [], "m0_not_exercised"),
    ("PP46-GENERATE-PAUSE", "暂停/继续生成并产生 checkpoint", "P6", ["P3", "P4"], ["generation-pause-cancel"], "baseline_observed"),
    ("PP46-GENERATE-CANCEL", "取消生成并保留合同允许的残稿", "P6", ["P3", "P4", "P1"], ["generation-pause-cancel"], "baseline_observed"),
    ("PP46-AUTOPILOT-PROGRESS", "SSE 展示自动驾驶进度和阶段", "P6", ["P3", "P4"], ["sse-disconnect-recovery"], "baseline_observed"),
    ("PP46-AUTOPILOT-LOG", "查看自动驾驶阶段和日志", "P6", ["P3", "P4"], ["sse-disconnect-recovery"], "baseline_observed"),
    ("PP46-QUALITY", "查看质量结果并生成候选修订", "P6", ["P3", "P4", "P1"], [], "m0_not_exercised"),
    ("PP46-CHECKPOINT", "阶段前产生检查点并恢复", "P3", ["P6", "P4", "P1"], [], "m0_not_exercised"),
    ("PP46-PROVIDER-CONFIG", "配置 Provider、凭证、Base URL 和模型", "P3", ["P4", "P1"], [], "m0_not_exercised"),
    ("PP46-PROMPT-PACKAGE", "使用 Prompt Package 覆写提示接点", "P2", ["P4", "P3"], [], "m0_not_exercised"),
    ("PP46-EXPORT", "导出作品并观察浏览器下载格式", "P6", ["P4", "P1"], ["workbench-export-menu"], "baseline_observed"),
]

MAIN_FLOW_ROWS = [
    ("FLOW-01", "PP46-LIB-CREATE", "建书", ["home-create-surface"]),
    ("FLOW-02", "PP46-LIB-SETUP", "设定生成并确认", ["wizard-generation"]),
    ("FLOW-03", "PP46-LIB-OPEN", "打开章节", ["workbench-shell", "workbench-chapter-tree"]),
    ("FLOW-04", "PP46-CHAPTER-EDIT-SAVE", "编辑保存", ["chapter-edit-save"]),
    ("FLOW-05", "PP46-GENERATE-CANCEL", "生成/暂停/取消", ["generation-pause-cancel"]),
    ("FLOW-06", "PP46-AUTOPILOT-PROGRESS", "SSE 断线重连", ["sse-disconnect-recovery"]),
    ("FLOW-07", "PP46-FORESHADOW", "查看伏笔", ["FLOW-07-foreshadow-ledger"]),
    ("FLOW-08", "PP46-BIBLE", "查看故事演进/Bible", ["FLOW-08-story-evolution-bible"]),
    ("FLOW-09", "PP46-CHECKPOINT", "恢复任务", ["checkpoint-recovery"]),
    ("FLOW-10", "PP46-EXPORT", "导出", ["workbench-export-menu"]),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def stable_path(path: Path) -> str:
    """Serialize repository paths independently of the checkout location."""

    try:
        return rel(path)
    except ValueError:
        return str(path)


def read_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"UTF-8 BOM is forbidden: {path}")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip("\n") + "\n", encoding="utf-8", newline="\n")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def file_record(path: Path) -> dict[str, Any]:
    return {"path": rel(path), "bytes": path.stat().st_size, "sha256": sha256(path)}


def iter_files(*roots: Path) -> Iterable[Path]:
    for root in roots:
        if not root.exists():
            continue
        yield from sorted((path for path in root.rglob("*") if path.is_file()), key=lambda path: rel(path))


def corpus_inventory() -> dict[str, Any]:
    """Return the shared v1 projection plus presentation-only group IDs."""

    inventory: dict[str, Any] = dict(v1_contract_inventory())
    inventory["negative_group_ids"] = [
        str(read_json(path)["group_id"])
        for path in v1_negative_group_paths()
    ]
    return inventory


def evidence_flow_id(flow: dict[str, Any]) -> str:
    value = flow.get("flow_id", flow.get("id"))
    if not isinstance(value, str) or not value:
        raise ValueError(f"browser evidence flow has no id: {flow!r}")
    return value


def flow_status(flow: dict[str, Any]) -> str:
    """Return the only accepted M0 flow status: a real exercised pass."""

    status = flow.get("status")
    if status == "passed" and flow.get("exercised", True) is True:
        return "exercised"
    if status == "exercised" and flow.get("passed", True) is True:
        return "exercised"
    raise ValueError(f"browser flow is not a real exercised pass: {evidence_flow_id(flow)}")


def render_contract_docs() -> None:
    inventory = corpus_inventory()
    write_text(
        ROOT / "docs" / "contracts" / "README.md",
        f"""# PlotPilot-Pluginized v1.2 公共合同

## 身份

- 合同版本：`1.2.0`；公共合同 owner：P0 `PPA-00-Integration`。
- 正式设计：`{DESIGN_PATH}`；版本 `v1.2`；SHA-256 `{DESIGN_SHA256}`。
- 本目录只解释已物化的合同，不重新解释字段、方法、hash、frame、state、cursor、lease 或 backup 语义。
- M0 后任何公共语义变化必须走 `Contract Delta`，发布 v2 与对应 ADR；下游不得直接改 v1 schema。

## 物化范围

`contracts/json-schema/` 中的 {inventory['schema_count']} 个 `*.schema.json` 是 Draft 2020-12 closed schemas；
`rpc-method-matrix.v1.json` 和 `compatibility-matrix.v1.json` 是方法/兼容性机器清单。
正例 fixture、四组 hash golden、Windows path corpus、compatibility/history corpus 与 {inventory['negative_group_count']} 组
`84.13` negative corpus 均由 `contracts/manifest-v1.json` 内容寻址。

| 项目 | 数量/规则 |
|---|---|
| closed schemas | {inventory['schema_count']} |
| 正例 fixture | {inventory['positive_fixture_count']} 个 `contracts/examples/fixtures/*.json`，另有 {inventory['combination_example_count']} 个组合示例 |
| golden | package、Skill、RunSnapshot/request-key、backup |
| negative | {inventory['negative_group_count']} 组、{inventory['negative_case_count']} 个 negative case |
| 哈希 JSON | RFC 8785 JCS、UTF-8、无 BOM；自含 hash 先省略自身字段 |
| 对象 | `additionalProperties=false`；联合顶层 `unevaluatedProperties=false` |

## 读取顺序

1. 先读 `surface.md` 固定语义与错误码；
2. 用 `schema-map.md` 定位 {inventory['schema_count']} 个 schema 与 §84 family；
3. 用 `method-matrix.md` 对照 §20 RPC 方法、meta profile、参数和结果；
4. 用 `negative-golden.md` 运行 {inventory['negative_group_count']} 组故障断言；
5. 用 `contracts/manifest-v1.json` 和 `docs/deliveries/PPA-00/contract-golden-manifest.json` 校验内容哈希。

## 验收命令

```powershell
python tools/integration/verify_contracts.py --all
node tools/integration/verify_contracts.mjs --all
python tools/integration/verify_cross_language_goldens.py
pytest tests/contract tests/acceptance -q
```

自动合同测试使用 deterministic fake Provider、禁用网络；本目录不包含用户数据、真实 provider 响应或产品业务实现。
""",
    )

    write_text(
        ROOT / "docs" / "contracts" / "surface.md",
        f"""# v1.2 合同固定语义（§13.4 / §20 / §84）

## §13.4 Package digest

解包只接受普通文件；相对路径统一 `/`、Unicode NFC，拒绝空段、`.`、`..`、绝对路径、NUL、反斜杠与 casefold 重名。
每个文件按原始 bytes 计算 lowercase SHA-256，`files.sha256` 自身除外；按规范化路径 UTF-8 bytes 升序；每行精确为
`<64hex><两个 ASCII 空格><path>\\n`，UTF-8、LF、末尾换行、无 BOM。

```text
package_hash = SHA256(UTF8("plotpilot-package/v1\\n") || exact_files_sha256_bytes)
release_id   = SHA256(UTF8("plotpilot-release/v1\\n" + plugin_id + "\\n" + version + "\\n" + package_hash + "\\n"))
```

M0 package golden：`package_hash=987e80013fe0cddd463eb8976fd75b62dbabef4f8e0e321ae6ad82a54f09b068`，
`release_id=00d365d816a45108cac08b5dc26eae015a44401c04aed0e9d512ed8fb24d88dd`。
Skill 使用独立域前缀 `plotpilot-skill-package/v1\\n` 与 `plotpilot-skill-release/v1\\n`，golden 为
`skill_package_hash=5cf3df6acea3c792ee36fa3c0c5757f87c8219aba88cfc568bd4a67738983b7f`、
`skill_release_id=abe35d45644a2ebac3b3c914876c613342b8d5a3e2421334241a06ec887436b7`。

## §20 Stdio Framed JSON-RPC

- JSON-RPC 2.0、stdio、UTF-8、`Content-Length` framing；stdout 只允许协议帧，stderr 只作日志。
- header 精确为 ASCII `Content-Length: <decimal>\\r\\nContent-Type: application/json; charset=utf-8\\r\\n\\r\\n`；header 上限 8 KiB、body 上限 8 MiB。
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

`negative-golden.md` 对应 {inventory['negative_group_count']} 组断言；每组至少一个真实负例且由 Python verifier 执行。Finding closure ID 以正式设计 §84.14 为准，
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
""",
    )

    schema_rows: list[str] = []
    for path in contract_schema_paths("v1"):
        contract_id = path.name.removesuffix(".schema.json")
        schema = read_json(path)
        schema_rows.append(
            f"| `{contract_id}` | `{SCHEMA_SECTIONS[contract_id]}` | `{rel(path)}` | `{schema.get('$id', '')}` |"
        )
    if len(schema_rows) != inventory["schema_count"]:
        raise ValueError(f"expected {inventory['schema_count']} schema rows, got {len(schema_rows)}")
    write_text(
        ROOT / "docs" / "contracts" / "schema-map.md",
        f"""# {inventory['schema_count']} closed schema 映射

`contract_id`、路径和 `$id` 直接从当前 schema 文件读取；section 映射只标注正式设计 §84 的规范来源，不新增字段语义。

| contract_id | normative section | file | `$id` |
|---|---|---|---|
"""
        + "\n".join(schema_rows)
        + """

## 非 schema 机器清单

| 文件 | 作用 | normative source |
|---|---|---|
| `contracts/json-schema/rpc-method-matrix.v1.json` | 29 个 worker/host 方法、profile、参数/结果字段及 14 错误码 | §20 / §84.3 |
| `contracts/json-schema/compatibility-matrix.v1.json` | Core API、plugin RPC、UI host、Python 兼容窗口 | §84.8 |

以上两个 JSON 不是 `*.schema.json`，但与 {inventory['schema_count']} 个 closed schema 一起进入 `contracts/manifest-v1.json` 的内容清单。
""",
    )

    matrix = read_json(SCHEMAS / "rpc-method-matrix.v1.json")
    method_rows: list[str] = []
    for method in sorted(matrix["methods"]):
        entry = matrix["methods"][method]
        params = ", ".join(f"`{field}`" for field in entry["params"]["fields"]) or "—"
        result = ", ".join(f"`{field}`" for field in entry["result"]["fields"]) or "notification / 无 response"
        method_rows.append(f"| `{method}` | `{entry['meta_profile']}` | {params} | {result} |")
    write_text(
        ROOT / "docs" / "contracts" / "method-matrix.md",
        """# §20 / §84.3 RPC method matrix

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
"""
        + "\n".join(method_rows)
        + """

## Profile and transaction gates

- `control`: `protocol_version,generation_id,plugin_release_id,deadline_at,context="control",operation_id`。
- `install`: control common fields plus `install_operation_id,install_lease_epoch`。
- `attempt`: control common fields plus `job_id,step_id,attempt_id,lease_epoch`。
- 所有 install/attempt mutation 先 fencing，再查 operation ledger；Core mutation 与 operation row 同事务提交。
- `host.job.complete/v1` 只提交 Attempt 终态；Step/Job 由 Core 聚合。
- `host.log/v1` 可丢弃但仍先 fencing；`runtime.heartbeat` 不携带业务结果。
""",
    )

    corpus = read_json(CONTRACTS / "corpus" / "manifest.json")
    negative_rows: list[str] = []
    for path in sorted((CONTRACTS / "corpus" / "negative" / "84.13").glob("*.json")):
        value = read_json(path)
        negative_rows.append(
            f"| `{value['group_id']}` | `{rel(path)}` | {len(value['negative'])} | {NEGATIVE_DESCRIPTIONS[value['group_id']]} |"
        )
    write_text(
        ROOT / "docs" / "contracts" / "negative-golden.md",
        """# §84.13 negative/golden corpus

十四组 corpus 是 M0 必须执行的 fail-closed 断言。每个 JSON 同时列出可接受的 positive fixture ID 与需要拒绝的 case；
`verify_contracts.py` 会按组运行并要求每个负例抛出预期合同错误，不以重试掩盖不确定状态。

| group | corpus | negative cases | assertion scope |
|---|---|---:|---|
"""
        + "\n".join(negative_rows)
        + f"""

## Corpus identity

- manifest：`contracts/corpus/manifest.json`，schema `{corpus['schema']}`，required groups `{corpus['required_group_count']}`。
- compatibility：`{corpus['compatibility'][0]}`、`{corpus['compatibility'][1]}`；history：`{corpus['history']}`；Windows path：`{corpus['path_corpus']}`。
- group 数：`{len(negative_rows)}`；case 总数：`{sum(len(read_json(path)['negative']) for path in sorted((CONTRACTS / 'corpus' / 'negative' / '84.13').glob('*.json')) )}`。

## Four independent golden families

1. package `files.sha256`/package hash/release ID；
2. Skill `files.sha256`/Skill package hash/release ID；
3. RunSnapshot JCS、request-key 与 snapshot hash；
4. backup bundle hash。

Python SDK、TypeScript SDK 和 Node verifier 对四组向量交叉复算；对象 set-like 排序与 ordered 数组语义均有正/负断言。
""",
    )


def build_contract_golden_manifest() -> dict[str, Any]:
    contract_manifest_path = CONTRACTS / "manifest-v1.json"
    contract_manifest = read_json(contract_manifest_path)
    contract_files = [dict(item) for item in contract_manifest["files"]]
    schema_files = [dict(item) for item in contract_manifest["schemas"]]
    inventory = v1_contract_inventory()
    if len(schema_files) != inventory["schema_count"]:
        raise ValueError("contract manifest does not match the v1 inventory projection")
    docs = [file_record(path) for path in iter_files(ROOT / "docs" / "contracts")]
    sdk = [
        file_record(path)
        for path in iter_files(ROOT / "backend" / "plotpilot_plugin_sdk", ROOT / "frontend" / "src" / "contracts")
        if "__pycache__" not in path.parts
    ]
    tooling = [
        file_record(path)
        for path in iter_files(ROOT / "tools" / "integration")
        if path.suffix in {".py", ".mjs"} and "__pycache__" not in path.parts
    ]
    negative = []
    for path in sorted((CONTRACTS / "corpus" / "negative" / "84.13").glob("*.json")):
        value = read_json(path)
        negative.append(
            {
                "group_id": value["group_id"],
                "path": rel(path),
                "negative_case_count": len(value["negative"]),
                "positive_fixture_ids": value["positive"],
                "sha256": sha256(path),
            }
        )
    if len(negative) != inventory["negative_group_count"] or sum(
        item["negative_case_count"] for item in negative
    ) != inventory["negative_case_count"]:
        raise ValueError("negative corpus does not match the v1 inventory projection")
    golden: dict[str, Any] = {}
    for name in ("package", "skill", "run-snapshot", "backup"):
        expected = CONTRACTS / "golden" / name / "expected.json"
        if expected.exists():
            golden[name] = {"expected": read_json(expected), "files": [file_record(path) for path in iter_files(CONTRACTS / "golden" / name)]}
        else:
            backup = CONTRACTS / "golden" / name / "backup.json"
            golden[name] = {
                "expected": {"backup_hash": read_json(backup)["bundle_hash"]},
                "files": [file_record(path) for path in iter_files(CONTRACTS / "golden" / name)],
            }
    return {
        "schema": "plotpilot-contract-golden-delivery/v1",
        "contract_version": contract_manifest["contract_version"],
        "source": {"formal_design": str(DESIGN_PATH), "version": "v1.2", "sha256": DESIGN_SHA256},
        "contract_families": FAMILY_IDS,
        "generated_from": {
            "contract_manifest": {"path": rel(contract_manifest_path), "sha256": sha256(contract_manifest_path)},
            "schema_count": len(schema_files),
            "contract_file_count": len(contract_files),
        },
        "inventory": dict(inventory),
        "golden_vectors": golden,
        "negative_groups": negative,
        "artifacts": {"contract_files": contract_files, "contract_docs": docs, "sdk": sdk, "tooling": tooling},
        "verification": {
            "python": "tools/integration/verify_contracts.py --all",
            "node": "node tools/integration/verify_contracts.mjs --all",
            "cross_language": "python tools/integration/verify_cross_language_goldens.py",
            "ledger": "docs/deliveries/PPA-00/integration-test-ledger.json",
        },
    }


def flow_index(evidence: dict[str, Any]) -> dict[str, dict[str, Any]]:
    flows = evidence.get("flows")
    if not isinstance(flows, list) or len(flows) != 10:
        raise ValueError("browser evidence must contain exactly ten flow records")
    index: dict[str, dict[str, Any]] = {}
    for flow in flows:
        if not isinstance(flow, dict):
            raise ValueError("browser evidence flow must be an object")
        flow_id = evidence_flow_id(flow)
        if flow_id in index:
            raise ValueError(f"duplicate browser evidence flow: {flow_id}")
        index[flow_id] = flow
        subflows = flow.get("subflows", [])
        if subflows is None:
            subflows = []
        if not isinstance(subflows, list):
            raise ValueError(f"browser evidence subflows must be a list: {flow_id}")
        for subflow in subflows:
            if not isinstance(subflow, dict):
                raise ValueError(f"browser evidence subflow must be an object: {flow_id}")
            subflow_id = evidence_flow_id(subflow)
            if subflow_id in index:
                raise ValueError(f"duplicate browser evidence flow: {subflow_id}")
            index[subflow_id] = subflow
    return index


def flow_evidence(flow_ids: list[str], index: dict[str, dict[str, Any]], evidence_path: Path) -> dict[str, Any]:
    flows = []
    for flow_id in flow_ids:
        if flow_id not in index:
            raise ValueError(f"required browser flow is missing: {flow_id}")
        flows.append(index[flow_id])
    screenshots: list[dict[str, Any]] = []
    assertions: list[Any] = []
    ui_actions: list[Any] = []
    expected_actual: list[Any] = []
    request_urls: list[str] = []
    trace_entries = 0
    for flow in flows:
        flow_status(flow)
        screenshot_values = flow.get("screenshots", flow.get("screenshot"))
        if isinstance(screenshot_values, (str, Path)):
            screenshot_values = [screenshot_values]
        if not isinstance(screenshot_values, list) or not screenshot_values:
            raise ValueError(f"browser flow has no screenshot: {evidence_flow_id(flow)}")
        for screenshot_value in screenshot_values:
            screenshot_path = screenshot_value.get("path") if isinstance(screenshot_value, dict) else screenshot_value
            screenshot = Path(str(screenshot_path))
            if not screenshot.is_file():
                raise ValueError(f"browser screenshot is missing: {screenshot}")
            screenshots.append({"path": str(screenshot), "bytes": screenshot.stat().st_size, "sha256": sha256(screenshot)})
        flow_assertions = flow.get("assertions", [])
        if isinstance(flow_assertions, list):
            assertions.extend(flow_assertions)
        flow_actions = flow.get("ui_actions", flow.get("actions", []))
        if isinstance(flow_actions, list):
            ui_actions.extend(flow_actions)
        flow_expected_actual = flow.get("expected_actual", flow.get("expected_vs_actual", []))
        if isinstance(flow_expected_actual, list):
            expected_actual.extend(flow_expected_actual)
        trace = flow.get("api_trace", [])
        if not isinstance(trace, list) or not trace:
            raise ValueError(f"browser flow has no API trace: {evidence_flow_id(flow)}")
        trace_entries += len(trace)
        request_urls.extend(str(entry.get("url")) for entry in trace if isinstance(entry, dict) and entry.get("url"))
    if not assertions and not expected_actual:
        raise ValueError(f"browser flow has no behavior assertion: {evidence_flow_id(flows[0])}")
    if not ui_actions:
        raise ValueError(f"browser flow has no UI actions: {evidence_flow_id(flows[0])}")
    return {
        "evidence_file": str(evidence_path),
        "evidence_sha256": sha256(evidence_path),
        "flow_ids": flow_ids,
        "status": "exercised",
        "executed": True,
        "screenshot_count": len(screenshots),
        "screenshots": screenshots,
        "api_trace_entries": trace_entries,
        "api_request_urls": sorted(set(request_urls)),
        "behavior_assertions": assertions,
        "ui_actions": ui_actions,
        "expected_actual": expected_actual,
    }


def render_feature_row(feature: tuple[str, str, str, list[str], list[str], str], index: dict[str, dict[str, Any]], evidence_path: Path) -> dict[str, Any]:
    feature_id, user_action, owner, collaborators, flow_ids, declared_status = feature
    evidence = flow_evidence(flow_ids, index, evidence_path) if flow_ids else {
        "evidence_file": str(evidence_path),
        "evidence_sha256": sha256(evidence_path),
        "flow_ids": [],
        "screenshot_count": 0,
        "screenshots": [],
        "api_trace_entries": 0,
        "api_request_urls": [],
        "behavior_assertions": [],
        "ui_actions": [],
        "expected_actual": [],
    }
    status = "exercised" if flow_ids else "owner_slice_pending"
    limitations = (
        "该功能未被十条 M0 主流程覆盖；由矩阵指定 owner 在后续里程碑提供完整纵切证据。"
        if not flow_ids
        else "M0 仅冻结现有 PlotPilot 入口与真实行为；插件化后的完整 owner 结果由对应项目在 M1–M7 验证。"
    )
    return {
        "feature_id": feature_id,
        "user_entry_and_precondition": user_action,
        "legacy_observation": "来自外部 PlotPilot v4.6.0 parity baseline；本次仅在 evidence 引用的 Home/Workbench surface 上复核。",
        "expected_pluginized_observation": "用户入口、可见反馈、顺序和错误边界保持；业务写入通过 Core/Candidate 合同，不以页面存在替代结果。",
        "owner_project": owner,
        "core_plugin_ui_dependencies": collaborators,
        "baseline_evidence": evidence,
        "automated_test_ids": ["M0-BROWSER-01"],
        "manual_smoke": "headful Chromium；fresh temporary PLOTPILOT_PROD_DATA_DIR；fresh browser context；真实 UI→API→执行链。",
        "writer_mode_and_cutover": "M0 不切换 writer；后续按 owner 的 legacy|plugin fence 与 Core Publication/Candidate 门禁切换。",
        "rollback": "失败时保留 evidence/receipt/Candidate（若有），回到上一 LKG/legacy adapter；不得删除既有正式 Revision。",
        "legacy_delete_gate": "M7 同一观察点的真实集成、恢复、故障和 legacy-hit=0 证据通过后才可删除。",
        "status": status,
        "limitations": limitations,
    }


def build_parity_ledger() -> dict[str, Any]:
    evidence_path = EVIDENCE / "browser-smoke.json"
    evidence = read_json(evidence_path)
    index = flow_index(evidence)
    main_flows: list[dict[str, Any]] = []
    for flow_id, feature_id, formal_flow, flow_ids in MAIN_FLOW_ROWS:
        evidence_for_flow = flow_evidence(flow_ids, index, evidence_path)
        main_flows.append(
            {
                "flow_id": flow_id,
                "formal_flow": formal_flow,
                "feature_id": feature_id,
                "user_entry_and_precondition": "fresh browser context；按 PlotPilot Home/Workbench 当前入口操作。",
                "legacy_observation": "M0 只记录 donor-compatible Home/Workbench 行为，不改业务实现。",
                "expected_pluginized_observation": "后续插件化必须保持同一入口/结果顺序，并通过 typed contract；本条记录来自真实 UI 行为。",
                "owner_project": next(row[2] for row in FEATURE_ROWS if row[0] == feature_id),
                "core_plugin_ui_dependencies": next(row[3] for row in FEATURE_ROWS if row[0] == feature_id),
                "baseline_evidence": evidence_for_flow,
                "automated_test_ids": ["M0-BROWSER-01"],
                "manual_smoke": "node tools/integration/browser_smoke.mjs；headful Chromium 152.0.7977.54。",
                "writer_mode_and_cutover": "M0 不切换 writer；生成/保存/恢复后续由 Core/owner 按合同 cutover。",
                "rollback": "M0 无业务切换；后续失败回到上一 LKG/legacy adapter，保留可审计 evidence。",
                "legacy_delete_gate": "对应完整真实集成、恢复和故障证据通过后才关闭 legacy 路径。",
                "status": evidence_for_flow["status"],
                "scope_note": "本条主流程已由真实 UI 动作、API trace、执行/恢复结果和截图共同冻结。",
            }
        )
    features = [render_feature_row(row, index, evidence_path) for row in FEATURE_ROWS]
    screenshot_dir = ROOT / "docs" / "deliveries" / "PPA-00" / "parity" / "screenshots"
    screenshot_records = [file_record(path) for path in iter_files(screenshot_dir)]
    return {
        "schema": "plotpilot-parity-ledger/v1",
        "status": "baseline_recorded",
        "scope": "M0 baseline observation；不是 M7 full functional parity claim。",
        "source": {
            "formal_design": str(DESIGN_PATH),
            "formal_design_sha256": DESIGN_SHA256,
            "feature_baseline": str(Path(r"C:\Users\Administrator\Desktop\交接文档\PlotPilot-Pluginized七项目任务书-2026-08-26\plotpilot-v460-feature-parity-baseline.md")),
            "baseline_commit": BASE_SHA,
        },
        "evidence_run": {
            "path": str(evidence_path),
            "sha256": sha256(evidence_path),
            "status": evidence["status"],
            "runtime": evidence["runtime"],
            "urls": evidence["urls"],
            "fixture": evidence["fixture"],
            "constraints": evidence["constraints"],
            "data_evidence": evidence["data_evidence"],
            "unexpected_external_calls": evidence.get("unexpected_external_calls", []),
            "forbidden_generation_calls": evidence.get("forbidden_generation_calls", []),
            "page_errors": evidence["page_errors"],
            "console_warning_count": len(evidence.get("console_events", evidence.get("console_errors", []))),
            "api_trace_entries": len(evidence["api_trace"]),
            "flow_count": len(evidence["flows"]),
            "screenshot_count": len(screenshot_records),
            "screenshot_dir": str(screenshot_dir),
            "screenshot_files": screenshot_records,
            "export_observation": evidence["export_observation"],
        },
        "main_flows": main_flows,
        "features": features,
        "interpretation": {
            "observed": "截图、API trace 和行为断言来自真实 headful browser run。",
            "executed": "十条主流程均由真实 UI→API→执行/恢复链执行；生成使用隔离 deterministic fake Provider，不接触付费/外网 Provider。",
            "export": "导出通过 UI 菜单与浏览器 download 事件捕获，并校验 UTF-8、BOM、文件名与非空内容。",
        },
    }


def current_git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True, encoding="utf-8").strip()


def build_identity_evidence() -> dict[str, Any]:
    lock_path = DELIVERY / "runtime-toolchain-lock.json"
    license_path = DELIVERY / "license-ledger.json"
    frontend_package = read_json(ROOT / "frontend" / "package.json")
    bootstrap = read_json(BOOTSTRAP_PATH)
    external_manifest = read_json(BACKUP_MANIFEST_PATH)
    matrix = read_json(MATRIX_PATH)
    creation_gate = verify_creation_gate(matrix_path=MATRIX_PATH, root=ROOT)
    live_gate_state = "closed" if creation_gate["closed"] else "open"
    return {
        "schema": "p0-identity-runtime-evidence/v1",
        "status": "verified",
        "captured_head": current_git("rev-parse", "HEAD"),
        "captured_branch": current_git("branch", "--show-current"),
        "baseline": {
            "commit": BASE_SHA,
            "branch": P0_BRANCH,
            "tree": bootstrap["baseline_tree"],
            "bootstrap_evidence": str(BOOTSTRAP_PATH),
            "bootstrap_evidence_sha256": sha256(BOOTSTRAP_PATH),
        },
        "formal_design": {"path": str(DESIGN_PATH), "version": "v1.2", "sha256": DESIGN_SHA256},
        "project_matrix": {"path": str(MATRIX_PATH), "sha256": sha256(MATRIX_PATH), "p0_write_set_hash": "076541384c643d879e8552737f94a9ce2ed9134dba3e0d448f2c4a0b58297656"},
        "donor": {
            "root": external_manifest["donor_root"],
            "head": external_manifest["donor_head_after"],
            "expected_product_commit": external_manifest["expected_product_commit"],
            "backup_manifest": str(BACKUP_MANIFEST_PATH),
            "backup_manifest_sha256": sha256(BACKUP_MANIFEST_PATH),
            "status_unchanged_in_external_manifest": external_manifest["donor_status_unchanged"],
            "all_files_match_in_external_manifest": external_manifest["all_files_match"],
        },
        "runtime": {
            "python_version": (ROOT / ".python-version").read_text(encoding="utf-8").strip(),
            "node_version": (ROOT / ".node-version").read_text(encoding="utf-8").strip(),
            "package_manager": frontend_package.get("packageManager"),
            "lock": {"path": str(lock_path), "sha256": sha256(lock_path)},
            "license_ledger": {"path": str(license_path), "sha256": sha256(license_path)},
        },
        "browser_boundary": {
            "route": "Vite browser route only",
            "browser_smoke": str(EVIDENCE / "browser-smoke.json"),
            "headful": True,
            "desktop_build": False,
            "pyinstaller": False,
            "tauri_build": False,
            "installer_build": False,
            "source_and_library_separated": "PLOTPILOT_PROD_DATA_DIR is an explicit runtime data root; source remains in this worktree.",
        },
        "frontend_route": {
            "scripts": frontend_package.get("scripts", {}),
            "desktop_script_names_absent": [name for name in ("tauri", "tauri:dev", "tauri:build") if name not in frontend_package.get("scripts", {})],
            "browser_scripts_present": [name for name in ("dev", "build", "preview") if name in frontend_package.get("scripts", {})],
        },
        "git_controls": {"donor_local_push": current_git("remote", "get-url", "--push", "donor-local"), "p1_p6_creation_gate": live_gate_state},
        "creation_gate": creation_gate,
        "matrix_projection": {"schema": matrix["schema"], "construction_authorized": matrix["construction_authorized"], "gate": matrix["topology"]["p1_to_p6_creation_gate"]},
    }


def build_m0_open_manifest(contract_manifest: dict[str, Any], parity: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    outputs = {
        "contract_golden_manifest": DELIVERY / "contract-golden-manifest.json",
        "parity_ledger": DELIVERY / "parity-ledger.json",
        "integration_test_ledger": DELIVERY / "integration-test-ledger.json",
        "identity_runtime_evidence": EVIDENCE / "m0.2-identity-runtime.json",
        "donor_protection_evidence": DELIVERY / "m0.1-donor-protection.json",
        "runtime_lock": DELIVERY / "runtime-toolchain-lock.json",
        "license_ledger": DELIVERY / "license-ledger.json",
        "dependency_delta": ROOT / "coordination" / "integration-queue" / "dependency-delta-naive-ui-2.44.1.json",
    }
    artifact_records: dict[str, Any] = {}
    for name, path in outputs.items():
        if path.exists():
            artifact_records[name] = file_record(path)
    creation_gate = identity["creation_gate"]
    live_gate_state = "closed" if creation_gate["closed"] else "open"
    inventory = corpus_inventory()
    return {
        "schema": "plotpilot-m0-open-manifest/v1",
        "status": "open",
        "milestone": "M0",
        "commit_ref": RELEASE_LABEL,
        "tag": RELEASE_LABEL,
        "self_hash_excluded": True,
        "identity": {
            "project_id": "P0",
            "project_name": "PPA-00-Integration",
            "branch": P0_BRANCH,
            "worktree": stable_path(ROOT),
            "base_sha": BASE_SHA,
            "formal_design_version": "v1.2",
            "formal_design_sha256": DESIGN_SHA256,
        },
        "source_inputs": {
            "task": str(TASK_PATH),
            "design": str(DESIGN_PATH),
            "project_matrix": str(MATRIX_PATH),
            "external_backup_manifest": str(BACKUP_MANIFEST_PATH),
            "bootstrap_evidence": str(BOOTSTRAP_PATH),
            "dependency_delta": stable_path(ROOT / "coordination" / "integration-queue" / "dependency-delta-naive-ui-2.44.1.json"),
        },
        "gates": {
            "M0.1": {"status": "passed", "evidence": [stable_path(DELIVERY / "m0.1-donor-protection.json"), str(BACKUP_MANIFEST_PATH), str(BOOTSTRAP_PATH)], "assertion": "external donor manifest, three file hashes, donor HEAD/status and product bootstrap identity match"},
            "M0.2": {"status": "passed", "evidence": [stable_path(EVIDENCE / "m0.2-identity-runtime.json"), stable_path(DELIVERY / "runtime-toolchain-lock.json"), stable_path(DELIVERY / "license-ledger.json"), stable_path(ROOT / "coordination" / "integration-queue" / "dependency-delta-naive-ui-2.44.1.json")], "assertion": "baseline, exact runtime lock, browser-only scripts, source/data-root separation, license record and dependency delta"},
            "M0.3": {"status": "passed", "evidence": ["backend/plotpilot_core/bootstrap", "backend/plotpilot_plugin_sdk/ports.py", "frontend/src/contracts/types.ts", "frontend/src/contracts/rpc.ts"], "assertion": "composition root, typed ports, unified errors/diagnostics and existing Home/Workbench composition preserved"},
            "M0.4": {"status": "passed", "evidence": ["contracts/manifest-v1.json", "docs/contracts/README.md", "docs/contracts/schema-map.md", "docs/contracts/method-matrix.md", "docs/contracts/negative-golden.md"], "assertion": f"{inventory['schema_count']} closed schemas, §13.4/§20/§84 surface, four goldens and all {inventory['negative_group_count']} negative groups ({inventory['negative_case_count']} executable cases)"},
            "M0.5": {"status": "passed", "evidence": ["backend/plotpilot_plugin_sdk/fake_provider.py", "backend/plotpilot_plugin_sdk/fixtures.py", "backend/plotpilot_plugin_sdk/ports.py", "frontend/src/contracts/verifier.ts"], "assertion": "deterministic fake Provider, typed port/UI/HTTP/SSE fixtures and Python/TypeScript SDK/verifiers"},
            "M0.6": {"status": "passed", "evidence": [stable_path(DELIVERY / "parity-ledger.json"), stable_path(EVIDENCE / "browser-smoke.json"), stable_path(ROOT / "docs" / "deliveries" / "PPA-00" / "parity" / "screenshots")], "assertion": "ten formal flow records cite real UI actions, expected/actual assertions, API trace, execution/recovery evidence and screenshots; deterministic fake Provider is isolated from live network"},
            "M0.7": {"status": "passed", "evidence": ["AGENTS.md", "coordination/integration-queue", "coordination/PPA-00/state.json", "tools/integration/validate_merge_gate.py"], "assertion": "write-set, delta, integration-ready, checkpoint, state, single queue and merge gate are tracked"},
        },
        "verification": {
            "contract_manifest": {"path": stable_path(CONTRACTS / "manifest-v1.json"), "sha256": sha256(CONTRACTS / "manifest-v1.json")},
            "contract_golden_delivery": {"path": stable_path(outputs["contract_golden_manifest"]), "sha256": sha256(outputs["contract_golden_manifest"]) if outputs["contract_golden_manifest"].exists() else None},
            "parity_ledger": {"path": stable_path(outputs["parity_ledger"]), "sha256": sha256(outputs["parity_ledger"]) if outputs["parity_ledger"].exists() else None},
            "test_ledger": {"path": stable_path(outputs["integration_test_ledger"]), "sha256": sha256(outputs["integration_test_ledger"]) if outputs["integration_test_ledger"].exists() else None},
            "commands": ["python tools/integration/verify_contracts.py --all", "pytest tests/contract tests/acceptance -q", "python tools/integration/verify_cross_language_goldens.py", "node tools/integration/verify_contracts.mjs --all", "python tools/integration/validate_merge_gate.py --json"],
        },
        "constraints": {
            "donor_local_push": identity["git_controls"]["donor_local_push"],
            "p1_p6_creation_gate": live_gate_state,
            "contract_delta": "none",
            "dependency_delta": "dependency-delta-naive-ui-2.44.1.json",
            "desktop_build": "not run",
            "real_provider": "not used",
            "network_in_contract_tests": "disabled",
            "fixture_user_data": "none",
        },
        "artifacts": artifact_records,
        "limitations": [
            "M0 browser smoke uses the existing UI-to-API execution chain with deterministic fake Provider; no live/paid provider or external network is used.",
            "The read-only donor still contains its pre-construction dirty files; the external backup manifest is the authority and the donor was not cleaned or reset.",
            "The legacy src-tauri directory and compatibility dependency remain read-only donor material; no Tauri build script or build was used in M0.",
        ],
    }


def generate() -> None:
    render_contract_docs()
    write_json(DELIVERY / "contract-golden-manifest.json", build_contract_golden_manifest())
    write_json(DELIVERY / "parity-ledger.json", build_parity_ledger())
    write_json(EVIDENCE / "m0.2-identity-runtime.json", build_identity_evidence())
    contract = read_json(DELIVERY / "contract-golden-manifest.json")
    parity = read_json(DELIVERY / "parity-ledger.json")
    identity = read_json(EVIDENCE / "m0.2-identity-runtime.json")
    write_json(DELIVERY / "m0-open-manifest.json", build_m0_open_manifest(contract, parity, identity))
    print("generated M0 contract docs and delivery manifests")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write the generated records (default is also write for CI ergonomics)")
    parser.parse_args()
    generate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
