# PlotPilot-Pluginized 当前项目说明

更新时间：2026-09-12

## 1. 项目定位

PlotPilot-Pluginized 是 PlotPilot 的 WebUI 与插件化重构方向：

> **作者归属声明：** 本仓库不是 PlotPilot 原作者的官方仓库，也不代表原作者或原项目团队。本项目是在既有 PlotPilot 源码和公开项目基础上进行的独立插件化改造、WebUI 集成和工程化实验；本项目维护者不自称原软件作者。原始代码、品牌、作者署名和许可证权利仍按上游项目及本仓库许可证处理。

- **Core** 负责 Workspace、Document、Revision、Job、Asset、Candidate、Publication 等稳定权威数据；
- **插件**负责模型 Provider、Prompt-Skill、Planner、写作、质量分析、导出等可替换能力；
- **WebUI**只负责用户入口、状态展示和调用已注册能力，不把具体能力写死在页面里；
- 插件通过版本、包哈希、Release 和 Generation 绑定，避免页面直接导入某个插件源码；
- 未安装、未激活或不满足 Generation 条件的能力应明确显示为不可用，而不是静默伪装成已完成。

这套设计的目标是：以后升级某个拆书、文风、规划或导出能力时，优先替换插件，不必重写 Core 和整套 WebUI。

交流 QQ 群：`663844122`。欢迎对插件化改造、WebUI 集成和小说工程化感兴趣的朋友加入交流。

## 2. 当前公开分支

### 稳定 WebUI 预览

- 分支：`codex/webui-preview-accepted-20260912`
- 提交：`73642143979ee2f253c5d6fa5a61b91d9c7ccf02`
- 状态：可启动、可查看、已完成启动检查与浏览器预览
- 用途：当前应优先从这个分支体验软件

已可用的主要功能：

- 新建和查看 Core Workspace；
- 保存书名、梗概、市场分区和目标篇幅；
- 新建、选择、编辑和保存章节 Core Document/Revision；
- 查看 Core 信息和任务抽屉基础入口。

### P2AB 未审计 WIP

- 分支：`codex/macro-planning-p2ab-g2-wip-unaccepted`
- 快照提交：`5133c06b847440b732821c3d6ff56128704e355f`
- 状态：**WIP、未审计、不可作为稳定版本使用**

该分支包含暂停前的 P2AB G2 模型 Broker/Host Provider Adapter 结构替换。它只是为了保留和研究当前进度而上传，不能直接合并到稳定分支，也不能据此宣称模型调用链已经完成。

## 3. 当前明确不可用的功能

稳定预览中，下列界面可能已经存在，但对应运行能力尚未完成激活：

- Project Planner 宏观规划与大纲生成；
- 真实 Provider 模型调用；
- Candidate 生成、审阅和 Publication；
- 伏笔账本；
- 故事演进 / Story Bible 投影；
- 完整检查点历史恢复；
- 插件导出 Artifact 下载。

原因不是页面没有按钮，而是对应的 Provider wheel、Prompt-Skill/Planner Release、活动 Generation 或 Core 读写投影尚未形成完整闭环。页面会显示 `Missing` 或禁用状态，避免把半成品误认为可用功能。

## 4. 当前主要问题

1. **插件包和 Generation 尚未完整激活**：三个 first-party 插件源码可以复用，但稳定预览中尚未生成并注册完整的离线包、包哈希和三成员 Generation。
2. **Provider 运行接线仍未完成**：P2AB 的 Host-owned Provider Adapter 已形成 WIP 快照，但最后一次接口调整后没有完成完整测试和独立审计。
3. **旧版丰富功能与新 Core 尚未全部接通**：仓库中保留了人物、Bible、规划、自动驾驶和导出等大量旧实现；源码存在不等于当前稳定 WebUI 已经挂载。
4. **历史 README 与当前插件化预览存在口径差异**：根 README 仍保留较多旧版剧情引擎说明；本文件以当前两个公开分支和实际 WebUI 行为为准。
5. **当前开发已暂停**：暂停是为了先观察稳定 WebUI、评估源码和决定后续是否继续施工或整理公开仓库。

## 5. 启动稳定 WebUI

在仓库根目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-webui.ps1 -Mode launch
```

或双击：

```text
start-webui.cmd
```

默认地址：

- WebUI：`http://127.0.0.1:3000/`
- Backend health：`http://127.0.0.1:8005/health`

启动器会检查端口所有者，不会接管未知进程。数据目录默认位于本机用户数据目录；不要把真实数据库、日志、`.env` 或 API Key 提交到 Git。

## 6. 恢复开发时的正确顺序

1. 以稳定 WebUI 分支为基线，不把 WIP 当作已验收代码；
2. 对 P2AB WIP 补齐接口同步、定向测试、备份/恢复、合同和回归门禁；
3. 由独立审计者复核 P2AB，复核通过后再集成；
4. 生成 Provider、Prompt-Skill、Planner 的确定性离线包；
5. 原子激活包含三个成员的 Generation；
6. 接通 Planner、Candidate、Publication 和 WebUI；
7. 最后运行浏览器完整流程，再决定是否创建正式 Release。

## 7. 分支使用规则

- 稳定预览分支用于查看当前能运行的 WebUI；
- `*-wip-unaccepted` 分支只用于审阅、实验和后续施工；
- 未审计 WIP 不得直接合并、打 Release 或作为用户默认版本；
- 不提交本地数据库、小说正文、缓存、构建产物、虚拟环境、`node_modules`、凭据和 API Key。
