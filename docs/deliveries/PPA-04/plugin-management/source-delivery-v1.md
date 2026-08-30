# PPA-04 Plugin Management G2 源码交付

## 身份与边界

- Task：`WAVE-D-G2-NW-P4-PLUGIN-MGMT-SOURCE-01`
- 精确基线：`8e3d3a7268e0d28559a7212790e3ab9f22c1453a`
- 分支：`codex/nw-p4-plugin-mgmt-g2`
- 写集严格限制在 `frontend/src/core/plugins/**`、`frontend/src/components/settings/plugin-management/**`、`tests/p4-webui/plugin-management/**` 与本目录/对应 coordination 目录。
- 未修改公共合同、SDK、router、store、`AppSettingsModal.vue`、root manifest/lock；未读取或复制冻结 G1 候选源码。

## 实现摘要

`model.ts` 对 `plugin-plan/v1` 的每个字段、数组成员、nullable synthesizer 做显式 materialization，输入可为真实 Vue Proxy，输出始终为递归 plain DTO。append/remove/move 使用稳定排序与 `1..n` 稠密重编号；save 只生成带 `operation=create_revision`、`plan_id`、`expected_base_revision` 和下一 revision 的 CAS intent，并校验请求/返回内容精确一致。dirty refresh 保留本地 draft，对同 revision 内容漂移和新 authority revision fail-closed 为 conflict。

同一模型还提供：

- 复用 H_C `unicodeNfcCasefold` / `normalizeWindowsPath` 的 `.zip/.ppplugin` 预检、共同根剥离和 root `plugin.json` 约束；
- 五个生命周期终态、failure code、active operation、busy、stream interruption 的 processing predicate；
- 单调 snapshot epoch，旧 response/error/finalizer 均不能覆盖新投影；
- prototype-backed ingress 与 selected-plan/list identity 校验。

Vue 组件只做整体浅替换：`PluginManagementPanel` 使用 `shallowRef` 保存 editor/snapshot，未接入安装、启停、退休、删除、Apply 或真实 Plan repository；gateway 未注入时显示 unavailable。

## Finding 覆盖（待 controller fresh Sol/max acceptance）

- `NW-P4-PM-SOL-F-001`：字段级 Proxy-safe load/save、create-revision CAS intent、same-base request/result drift 负例。
- `NW-P4-PM-SOL-F-002`：sparse order、append/remove/move 边界、不可变输入与真实 Vue ingress 负例。
- `NW-P4-PM-SOL-F-003`：冻结 Unicode/NFC/full-casefold、Windows path、嵌套/多根 manifest、`.zip/.ppplugin` 与目录项回归。
- `NW-P4-PM-SOL-F-004`：五终态、failure code、非 active operation、stream interruption。
- `NW-P4-PM-SOL-F-005`：response/error/loading-finalizer 共享 epoch guard 与乱序响应回归。
- `NW-P4-PM-SOL-F-006`：dirty draft 保留、immutable revision drift/conflict、conflict 后禁止编辑/保存。

## 验证证据

- `node --experimental-strip-types --test tests/p4-webui/plugin-management/*.test.mts`：`23 passed, 0 failed`，原始输出见 `coordination/PPA-04/plugin-management/raw-plugin-tests.txt`。
- `node --experimental-strip-types --test tests/p4-webui/*.test.mts`：`13 passed, 0 failed`，原始输出见 `coordination/PPA-04/plugin-management/raw-p4-tests.txt`。
- `git diff --check`：`exit=0`，原始输出见 `coordination/PPA-04/plugin-management/raw-diff-check.txt`。
- taskbook 精确命令 `frontend/node_modules/.bin/vue-tsc.cmd --noEmit -p frontend/tsconfig.app.json --incremental false`：当前 worktree 依赖不存在，记录为 `ENV_UNAVAILABLE`，不能计为 PASS；原始输出见 `coordination/PPA-04/plugin-management/raw-vue-tsc.txt`。

## 复用与依赖

本地知识库路由检索到的 PlotPilot 记录未提供可直接复用的 Plugin Management 实现；H_C schema/fixture 与 verifier helpers 是权威复用来源。gongju selector 未找到 TypeScript/DTO 匹配工具，因此未复制第三方或冻结候选代码。运行时依赖保持不变；Plugin API G2、Plan repository/Plan-switch CAS、P4 Assembly 与 Runtime Composition 仍是停止能力/下游依赖。

## 交付状态

本文件与源码/测试同属唯一 source commit。未执行 merge、rebase、cherry-pick、tag、push、browser、GUI、Tauri、desktop、EXE、installer 或最终产物构建；不作 merge eligibility 或 controller acceptance 声明。
