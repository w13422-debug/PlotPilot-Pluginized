# §84.13 negative/golden corpus

十四组 corpus 是 M0 必须执行的 fail-closed 断言。每个 JSON 同时列出可接受的 positive fixture ID 与需要拒绝的 case；
`verify_contracts.py` 会按组运行并要求每个负例抛出预期合同错误，不以重试掩盖不确定状态。

| group | corpus | negative cases | assertion scope |
|---|---|---:|---|
| `84.13-01` | `contracts/corpus/negative/84.13/01.json` | 2 | JCS set permutation、Skill order permutation 与 Package/RunSnapshot/Skill golden 独立复算 |
| `84.13-02` | `contracts/corpus/negative/84.13/02.json` | 3 | Result profile、bundle、item 错配；Candidate target/mutation/base/write-set、parent cycle、cross-workspace source 与逐项映射 |
| `84.13-03` | `contracts/corpus/negative/84.13/03.json` | 2 | Broker invoke ACK-loss、child cancel、receipt propagation、Data format/interpreter/Plan/Snapshot mismatch |
| `84.13-04` | `contracts/corpus/negative/84.13/04.json` | 2 | operation key ACK-loss、首包/中间/final upload 重试、status 恢复和同 key 不同 payload |
| `84.13-05` | `contracts/corpus/negative/84.13/05.json` | 3 | Attempt cancel/complete race、fresh resume、checkpoint 倒退/跨 Snapshot、shadow lease stale |
| `84.13-06` | `contracts/corpus/negative/84.13/06.json` | 3 | Bundle producer/snapshot/lease/receipt/staging/outcome transaction 边界 |
| `84.13-07` | `contracts/corpus/negative/84.13/07.json` | 3 | 并发 install base CAS、crash point、rollback once、safe mode exit、settings validator failure |
| `84.13-08` | `contracts/corpus/negative/84.13/08.json` | 3 | retire/new pin race、可恢复 Attempt blocker、package 删除后的 Candidate Publication |
| `84.13-09` | `contracts/corpus/negative/84.13/09.json` | 2 | aggregate/Event crash、SSE gap/cursor ahead/snapshot convergence、Core/Job cursor 混用 |
| `84.13-10` | `contracts/corpus/negative/84.13/10.json` | 3 | raw stream 超过 ACK、cancel/crash race、唯一 incomplete Candidate、terminal hydration |
| `84.13-11` | `contracts/corpus/negative/84.13/11.json` | 3 | Skill executed/failed/skipped、model claim、verified patch 组合和 patch 篡改 |
| `84.13-12` | `contracts/corpus/negative/84.13/12.json` | 3 | Worker immutable URL/CSP、stale/duplicate intent、unknown component/prop/event、无限循环导航仍响应 |
| `84.13-13` | `contracts/corpus/negative/84.13/13.json` | 3 | backup mode 条件、manifest/hash、crash staging、restore 新 root、projection rebuild |
| `84.13-14` | `contracts/corpus/negative/84.13/14.json` | 4 | compatibility valid/invalid、历史 v1 raw bytes reader、Windows reserved/ADS/trailing dot-space/casefold collision |

## Corpus identity

- manifest：`contracts/corpus/manifest.json`，schema `contract-corpus/v1`，required groups `14`。
- compatibility：`compatibility/valid.json`、`compatibility/invalid.json`；history：`history/v1-raw.json`；Windows path：`paths/windows-paths.json`。
- group 数：`14`；case 总数：`39`。

## Four independent golden families

1. package `files.sha256`/package hash/release ID；
2. Skill `files.sha256`/Skill package hash/release ID；
3. RunSnapshot JCS、request-key 与 snapshot hash；
4. backup bundle hash。

Python SDK、TypeScript SDK 和 Node verifier 对四组向量交叉复算；对象 set-like 排序与 ordered 数组语义均有正/负断言。
