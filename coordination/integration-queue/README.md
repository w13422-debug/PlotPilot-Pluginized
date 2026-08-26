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
- Empty `pending_*` arrays in `queue-state-v1.json` mean there is no open
  delta at the M0 baseline; they do not waive the policy for later work.

The queue contains no product implementation, user data, credentials, or
mock responses.  Development fakes belong in the P0 SDK fixture package and
must never enter a user-facing path.
