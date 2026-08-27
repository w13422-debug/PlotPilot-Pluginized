# ADR-043：Core HTTP 请求失败合同与 400 组合边界

- **状态**：Implemented；source candidate，待 fresh Sol Max 聚焦复核
- **日期**：2026-08-28
- **Owner**：P0 / PPA-00-Integration
- **Decision**：`PPA-00-NW-P1-CORE-HTTP-01-ADJUDICATION-001`
- **正式设计**：v1.2，SHA-256 `e70f450b75cfa753148cf620d13855d0073f7d61294e7599ea9cc98f3e612b7b`

## 决策

`core-api-method-matrix/v1` 已发布的 `core-http-error/v1` 只表达一个有效 typed request 进入 Core domain 后的 404/409/422 失败。无法解码或无法形成有效 request DTO 的输入不能冒充该 domain error，也不能泄露 FastAPI 默认 422 body。

P0 因此首次 additive 发布两个独立 family：

1. `core-http-request-error/v1`：closed response envelope，字段固定为 `schema,error_code,message,retryable`；
2. `core-http-request-failure-policy/v1`：closed composition policy，固定 `status=400`、`error_schema=core-http-request-error/v1`、`retryable=false`，并冻结四个有序 binding。

四个 binding 是：

| source | error_code | scope |
|---|---|---|
| `json_decode` | `malformed_json` | `all_core_routes` |
| `closed_request_schema` | `invalid_request` | `all_core_routes` |
| `query_decode` | `invalid_query` | `all_core_routes` |
| `asset_range_bounds` | `range_out_of_bounds` | `asset.range` |

所有 envelope 都是 HTTP 400 且 `retryable=false`。未知字段、未知 code、错误 status、retryable drift、缺失/重复/重排/错配 binding 均 fail-closed。

## Asset range 边界

- `0 <= offset < total_size`：返回最多 requested `length` 的 bounded bytes；
- `offset == total_size`：HTTP 200，返回空 chunk、`length=0`、`next_offset=null` 和空字节 SHA-256；
- `offset > total_size`：HTTP 400 `core-http-request-error/v1` / `range_out_of_bounds`；
- 本 v1 不发布 206/416、`Range`/`Content-Range`、ETag、304 或条件读取语义。

## 不变项与所有权

- 不修改或重新解释 `core-http-error/v1`、`core-api-method-matrix/v1`、Publication/Asset/authority v1 DTO 或 Plugin RPC method matrix；
- P0 composition 在调用 P1 authority 前执行 JSON/query/closed-schema/range-boundary 映射；
- P1 的真实 authority、Publication、Asset runtime 与 HTTP router 不在本提交实现；
- `backend/plotpilot_core/api/app.py` 的 production mount 继续等待真实 P1 router 被接受，不在本提交触碰；
- Plugin worker/UI Bundle 不获得 Core HTTP 或 Publication 权限。

## 可执行物化与消费门

Schema 由 `tools/integration/generate_contract_schemas.py` 确定生成；Python SDK 位于 `backend/plotpilot_plugin_sdk/core_api.py`；TypeScript unknown-ingress parser 位于 `frontend/src/contracts/core-api.ts`；positive/negative vectors 位于 `contracts/golden/core-http-request-failure-v1/` 与 `contracts/corpus/core-http-request-failure-v1/`，并由 `contracts/manifest-v1.json` 内容寻址。

本提交只形成 source candidate。fresh Sol Max 聚焦复核通过前，不授予 merge eligibility，不允许 P1 router 或 P0 app mount 消费该 family。
