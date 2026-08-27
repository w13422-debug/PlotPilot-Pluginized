# P0 Integration Queue

This is the single, P0-owned intake surface for downstream integration.  It
is deliberately a small, file-backed queue: one submission document, one
Contract Delta document, and one Dependency Delta document are the only
accepted shapes.  The external `project-matrix.json` remains the machine
authority for ownership and write sets.

## Required submission

Copy `integration-ready-v1.json` to a new file named after the submission ID
and fill every required field.  A submission is not ready until it contains:

1. the producing project, source/base SHA and exact contract-manifest hash;
2. the changed-path list and a write-set proof against the matrix;
3. any Contract Delta and Dependency Delta IDs (or explicit empty arrays);
4. commands with exit codes and raw-output artifact paths;
5. a reviewer and finding-closure reference.

Run the gate from this worktree before asking P0 to merge:

```powershell
python tools/integration/validate_merge_gate.py --submission <path> --json
```

P0 performs the final `--no-ff` merge and reruns the affected tests.  No
submission may modify a public v1 contract without a Contract Delta; no root
dependency or lockfile change may proceed without a Dependency Delta.

## Delta policy

- **Contract Delta** records a discovered mismatch with the immutable v1.2
  design.  It is a stop signal for the affected slice, not permission to
  reinterpret the contract.
- **Dependency Delta** records a root/runtime dependency addition, removal, or
  version change, including license, provenance, compatibility, and validation
  evidence.
- `queue-state-v1.json` records `PPA-01-CD-001` as an accepted-for-adjudication
  Contract Delta.  Its public Core API/Publication slice is stopped until the
  P0 decision's contracts are published; P1's unrelated internal authority
  work may still be submitted.  Empty `pending_*` arrays only describe the
  historical M0 baseline and never waive the policy for later work.

The queue contains no product implementation, user data, credentials, or
mock responses.  Development fakes belong in the P0 SDK fixture package and
must never enter a user-facing path.

## M1–M7 downstream registration

P0 在 `coordination/PPA-00/downstream-registry-v1.json` 登记六个保存项目的 Codex 身份、矩阵写集、依赖顺序与 accepted base。
当前 accepted base 为 `M0-OPEN-R4` peeled commit `42123d1a5126bb2bef31304b0498e2e7def9183e`。
登记不等于入队：P0 只处理落入本目录、状态为 `ready=true` 的真实 `integration-ready/v1` submission；分支存在、下游 task 状态或 commit 本身都不能替代 submission。
验证通过后才由 P0 按矩阵依赖使用 `git merge --no-ff`，合并后重跑受影响门禁并记录 receipt。

## PPA-01-CD-001 adjudication

`PPA-01-CD-001` is accepted as a scoped public-contract gap by
`coordination/PPA-00/contract-decision-PPA-01-CD-001.json` and
`docs/contracts/adr-041-core-api-contract-v1.md`.  P0 will publish the
minimal `core-authority-command-query/v1`, `publication-command-result/v1`
and `asset-metadata/v1` families; the existing plugin RPC matrix and SDK
remain unchanged.  P1 may first submit only its internal authority/migration
batch.  After that batch is no-ff integrated and the P0 contract patch has
passed focused review, P1 and P4 must fast-forward clean worktrees to the new
integration head before resuming the stopped public slices.
