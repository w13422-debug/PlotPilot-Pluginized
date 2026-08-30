# ADR-044：M4/M5 Public Surface v2

**状态：** Accepted for the P0 source wave
**Owner：** P0 / contract integration
**Scope：** Candidate、Publication、Story-State projection、Durable Job HTTP/SSE、plugin lifecycle

## Context

M0 已冻结现有 v1 contract、SDK 和 `backend/plotpilot_core/api/v1/**`。M4/M5
需要把 Candidate review、Core-only Publication、Story-State projection、Job
恢复以及插件 generation 生命周期暴露给后续项目，但不能把这些新增字段、状态、
cursor 或 HTTP methods 静默塞入 v1。这个 ADR 因而只定义 additive v2 surface；
它不改变正式设计 v1.2 的任何字节。

## Decision

1. **Roots and ownership.** v2 使用 `/api/v2/core`、`/api/v2/jobs` 和
   `/api/v2/plugins`。Core 是 Workspace、Revision、Candidate、Publication、
   Job Event/SSE 及 plugin lifecycle 的公共权威；插件没有 Publication API。
2. **Candidate boundary.** Candidate 明确携带 target、mutation、payload Asset/hash、
   base、完整 write-set、parent IDs、source refs、outcome 和 eligibility。target、
   base、write-set 必须属于同一 Workspace；跨 Workspace 的 parent/source 只可作为
   lineage/evidence。review 只产生 review result，不改变正文。
3. **Publication.** `publication.accept` 是唯一 Publication route。普通 `partial`
   Candidate 只能审核；只有 Core 以 durable stream 产生的 `incomplete_stream`
   Candidate 能以显式 accept 进入同一 CAS、payload、receipt 和 provenance gate。
   `publication_operation_key` 重试必须幂等，换 payload 必须拒绝。
4. **Projection.** Story-State projection input 只接受 Core 派生的 Publication、
   Candidate、当前 Revision、immutable Asset/hash 和闭合 provenance receipt graph；
   该输入是只读投影，不是第二个正文 authority。
5. **Jobs and cursors.** Job snapshot 是 Job 权威；Job Event cursor 与 Core Event、
   Candidate cursor 分离。SSE 恢复明确表达 cursor-ahead、replay gap 和 snapshot
   recovery，恢复后只能从更大的同域 cursor 继续。
6. **Lifecycle.** install/upgrade/retire/rollback 采用显式 action、operation key
   和 expected/target generation。active Job、pinned release 或 generation race
   必须 fail-closed；任何 lifecycle command 都不得直接产生 Publication。

## Closed contract inventory

The existing `tools/integration/generate_contract_schemas.py` is extended with one
v2 lane and emits the following closed schemas:

- `candidate-query-result-v2` — list/get/preview request and result variants;
- `core-authority-command-query-v2` — authority query/result, Publication command/result,
  and Core HTTP errors;
- `candidate-review-v2` — review query, decision command and result;
- `story-state-projection-input-v2` — projection input and receipt closure;
- `job-http-command-query-v2` — list/snapshot/start/control/event/SSE recovery;
- `plugin-api-command-query-v2` — discovery and install/upgrade/retire/rollback.

`core-api-method-matrix.v2.json` records each route's method, path identity, request/result
schema, success/failure status, operation-key requirement and cursor domain. It is an
adjudicated P0 minimum surface, not a claim that formal design v1.2 already froze every
post-M0 DTO field.

## Verification and compatibility

The v2 corpus and goldens live below `contracts/corpus/m4-m5-public-surface-v2/` and
`contracts/golden/m4-m5-public-surface-v2/`. Python and TypeScript ingress both call the
existing strict schema verifier; semantic checks cover Workspace crossing, write-set/base
binding, parent cycles, partial Publication, projection closure, operation-key reuse,
cursor-domain/ahead handling and lifecycle races.

The v1 manifest remains generated from the v1 inventory only. `manifest-v2.json` adds the
v2 inventory while retaining the accepted v1 manifest identity and file records. The v1
byte/surface guard is run against accepted E0 `761c79a8343dbc17ee8f40e21e60fb962eeecd79`.

## Consequences

Downstream P1/P2/P3/P4/P5/P6 work can consume one explicit additive contract without
editing v1. Implementations must keep the Core Publication boundary and evidence closure;
the v2 SDK modules are validation-only and do not expose a database handle or a direct
publication side effect.
