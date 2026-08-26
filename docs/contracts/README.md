# PlotPilot-Pluginized v1.2 公共合同

## 身份

- 合同版本：`1.2.0`；公共合同 owner：P0 `PPA-00-Integration`。
- 正式设计：`C:\Users\Administrator\Desktop\交接文档\PlotPilot-Pluginized正式设计规划-2026-08-25-v1.md`；版本 `v1.2`；SHA-256 `e70f450b75cfa753148cf620d13855d0073f7d61294e7599ea9cc98f3e612b7b`。
- 本目录只解释已物化的合同，不重新解释字段、方法、hash、frame、state、cursor、lease 或 backup 语义。
- M0 后任何公共语义变化必须走 `Contract Delta`，发布 v2 与对应 ADR；下游不得直接改 v1 schema。

## 物化范围

`contracts/json-schema/` 中的 48 个 `*.schema.json` 是 Draft 2020-12 closed schemas；
`rpc-method-matrix.v1.json` 和 `compatibility-matrix.v1.json` 是方法/兼容性机器清单。
正例 fixture、四组 hash golden、Windows path corpus、compatibility/history corpus 与 14 组
`84.13` negative corpus 均由 `contracts/manifest-v1.json` 内容寻址。

| 项目 | 数量/规则 |
|---|---|
| closed schemas | 48 |
| 正例 fixture | 35 个 `contracts/examples/fixtures/*.json`，另有 4 个组合示例 |
| golden | package、Skill、RunSnapshot/request-key、backup |
| negative | 14 组、105 个 negative case |
| 哈希 JSON | RFC 8785 JCS、UTF-8、无 BOM；自含 hash 先省略自身字段 |
| 对象 | `additionalProperties=false`；联合顶层 `unevaluatedProperties=false` |

## 读取顺序

1. 先读 `surface.md` 固定语义与错误码；
2. 用 `schema-map.md` 定位 48 个 schema 与 §84 family；
3. 用 `method-matrix.md` 对照 §20 RPC 方法、meta profile、参数和结果；
4. 用 `negative-golden.md` 运行 14 组故障断言；
5. 用 `contracts/manifest-v1.json` 和 `docs/deliveries/PPA-00/contract-golden-manifest.json` 校验内容哈希。

## 验收命令

```powershell
python tools/integration/verify_contracts.py --all
node tools/integration/verify_contracts.mjs --all
python tools/integration/verify_cross_language_goldens.py
pytest tests/contract tests/acceptance -q
```

自动合同测试使用 deterministic fake Provider、禁用网络；本目录不包含用户数据、真实 provider 响应或产品业务实现。
