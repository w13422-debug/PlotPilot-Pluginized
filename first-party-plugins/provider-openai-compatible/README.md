# OpenAI-compatible Provider

这是 PlotPilot 的 first-party、SDK-only OpenAI-compatible Provider 子单元。
它接受通用的 `ModelProfileRevision`-like mapping/dataclass，构造
`/chat/completions` 形状的请求，并把全部 I/O 委托给宿主注入的
`OpenAICompatibleTransport`。

## 运行边界

- `OpenAICompatibleProvider(transport=...)` 的 `transport` 是强制参数；本包不
  创建 HTTP client，也没有真实网络默认路径。
- 单元测试和离线宿主路径使用 `DeterministicTransport`/`TestTransport`。
- 真实网络实现必须由宿主显式注入；本包运行时只依赖
  `plotpilot_plugin_sdk` 与 Python stdlib。
- API key 只接受一次性 `api_key` 参数或 `SecretProvider` 查找结果，绝不写入
  profile、receipt、repr 或日志安全视图。
- Provider 只返回 `ProviderResult`/`ModelReceipt` 等 provider-owned 数据；它不
  访问 Core DB，也不创建 Core provenance receipt 或修改 Job/Candidate/Publication。

## 不确定窗口与恢复

生命周期会保留 `prepared -> sent -> received -> receipted` 轨迹，并显式区分
`failed`、`cancelled` 和 `uncertain`。发送前 transport 失败是 `failed`；发送后但
未收到响应、响应持久化失败或 receipt sink 失败是 `uncertain`。v1 replay policy
只接受 `idempotent_auto`、`manual_if_unknown` 与 `never_replay`。仅
`idempotent_auto` 会对显式标记为 transient 的发送前失败，按 profile 的
`max_retries` 使用同一 provider/model/invocation key 有界重试；一旦可能已发送，
transport 若明确声明 `supports_query=True` 就只用相同 key 查询，否则返回
`uncertain`，绝不再次 send，也不会自动切换 provider/model。

Transport failure 的发送边界是显式三态 `sent` / `not_sent` / `unknown`；旧式
boolean `sent=False` 不构成未发送证明。只有明确 `not_sent` 且明确 transient 才能
进入上述有界重试。缺失、冲突或任何 positive sent 证据均按 `sent/unknown`
处理。raw byte stream 保持原始字节；宿主 response 的 `iter_lines()` 则恢复每行
换行和空行 SSE frame boundary 后再解析。

流式 OpenAI/SSE 数据被归一化为单调文本前缀，每个 chunk 带 `seq`、`prefix`、
`prefix_hash` 和 delta。receipt 记录 request/response hash、可用 token/cost、
终止原因、错误和输入上下文的安全视图。

## 源码/测试打包约定

`plugin.json` 按 code-plugin v1 manifest 约定声明宿主打包时生成的 wheel、wheelhouse
和 requirements lock 路径。源码交付不伪造 wheel/binary；`backend/dist/` 与
`backend/wheels/` 是发布流水线的输出位置，当前 source checkout 中不要求它们
存在。定向测试直接把 `backend/src` 与仓库 SDK 加入 import path，不访问网络。

`backend/requirements.lock` 明确没有第三方运行时依赖。`migrations/manifest.json`
是 provider-owned 空迁移清单，供宿主按 manifest 的 transactional-shadow 入口处理；
本实现不保存 provider secret 或 Core 数据。
