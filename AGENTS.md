# PlotPilot-Pluginized P0 Integration Rules

This repository is the `PPA-00-Integration` worktree.  The machine-readable
source of truth for ownership is the external
`PlotPilot-Pluginized七项目任务书-2026-08-26/project-matrix.json`; this file
only records the operating rules that keep the worktree reproducible.

## Identity and gate

- Branch: `codex/ppa-00-integration`.
- M0 base: `1c481237b6fa32ef5f85d7f8da4cb16f366cd4f0`.
- Public contract owner and sole merge owner: P0.
- Do not create a P1–P6 branch, worktree, project, or first-party plugin until
  the exact `M0-OPEN` commit and tag have been created and verified.
- `donor-local` is a read-only donor remote; its push URL must remain exactly
  `DISABLED`.

## P0 write set

Only the following paths may be changed by this project.  The project matrix
is authoritative; a new path must be assigned there before it is touched.

```text
contracts/**
backend/plotpilot_plugin_sdk/**
backend/plotpilot_core/bootstrap/**
backend/plotpilot_core/api/app.py
frontend/src/contracts/**
tests/contract/**
tests/acceptance/**
tools/integration/**
docs/contracts/**
docs/deliveries/PPA-00/**
coordination/PPA-00/**
coordination/integration-queue/**
AGENTS.md
.gitignore
.python-version
.node-version
.npmrc
pyproject.toml
requirements*.txt
package.json
pnpm-lock.yaml
pnpm-workspace.yaml
frontend/package.json
frontend/vite.config.ts
frontend/tsconfig*.json
start-webui.cmd
LICENSE
NOTICE
THIRD_PARTY_NOTICES.md
```

All existing PlotPilot business implementation directories are read-only for
P0.  A defect found there is returned to its future owner; P0 may only wire
its own composition/diagnostic seam.

`.codex/active-work.json` is a worktree-local recovery checkpoint, not a
product or release artifact.  It must remain local/ignored and must never be
used to justify a write outside the matrix.

## Contract and dependency changes

- A public v1 contract is immutable.  A discovered mismatch is recorded as a
  Contract Delta in `coordination/integration-queue/` and the affected slice
  stops; do not silently reinterpret the formal v1.2 design.
- A root/runtime dependency change is recorded as a Dependency Delta before
  editing lock files.  Pin exact versions and include license/provenance
  evidence.
- Downstream changes are accepted only as `integration-ready/v1` submissions
  with base SHA, contract manifest hash, write-set proof, and reproducible
  test evidence.
- Merge queue integration is single-writer and `--no-ff`; validate the write
  set and base before any merge.

## M0 verification boundary

- M0 must materialize the complete §13.4, §20, §84 and §84.13 surface,
  including closed schemas, method matrix, SDK/verifiers, goldens and all
  fourteen negative groups.
- Automatic contract tests use the deterministic fake Provider and disabled
  network.  Browser smoke is local Home/Workbench only, uses a fresh temporary
  `PLOTPILOT_PROD_DATA_DIR` and a fresh browser context, and does not save a
  non-empty chapter body or invoke generation pipelines.
- M0 uses the browser/Vite route only.  Do not run PyInstaller, Tauri, a
  desktop/EXE/installer build, or a desktop smoke test.
- Never commit user data, caches, virtual environments, `node_modules`, build
  output, credentials, or a live-provider response.
