# PPA-06B Export / Quality — source candidate delivery record

## Identity and gate

- Project: `P6B` / `PPA-06B-Export-Quality`
- Branch: `codex/ppa-06b-export-quality`
- Accepted base: `1b352be671e70a2ce443b10499060982e93d64b7`
- Working tree: `C:\Users\Administrator\Desktop\写作资料汇总\PlotPilot-Pluginized-worktrees\PPA-06B-Export-Quality`
- Donor-local push: `DISABLED`
- Source head: recorded from Git after the single source-only candidate commit and returned with this delivery; the record is intentionally non-self-referential.
- Review gate: Sol-only read-only review is required; Luna implementation/test evidence is not acceptance or merge eligibility.

## Reuse decision

The exact in-repository PlotPilot v4.6.0 export implementation remains the source of
format, filename, order, MIME/download payload, and encoding parity. The existing P1
AssetStore shape is used only through the explicit `P1AssetStoreAdapter`. The local
gongju selector returned unrelated video/Git/hardware helpers, so no tool was copied;
there is no additional reuse benefit for this narrow domain adapter.

No new dependency, root manifest/lock, contract, public SDK, UI, Chapter Workflow,
Autopilot, P2, or P3 file was changed.

## Delivered implementation

### Export Suite

- `build_export_from_snapshot` first verifies the caller-supplied `run-snapshot/v1`,
  binds `parameters_asset_id` to its exact `asset_hashes` digest, reads the
  Core-created `export-current-revisions/v1` JSON Asset, and verifies its exact JCS
  bytes and RunSnapshot revision projection.
- Chapter body bytes are read only through an injected `read_asset` port, checked for
  hash, `text/plain`, strict UTF-8, no BOM, optional P1 metadata identity/workspace,
  and contiguous manifest order. No正文/revision write exists.
- `export_to_asset` has one injected immutable `create_asset` call. The renderer's
  legacy Markdown download media type remains `text/markdown; charset=utf-8`; the
  Asset metadata boundary uses contract-valid `text/markdown;charset=utf-8` without
  changing output bytes or filename.
- `ExportResult` carries immutable output metadata and a deterministic
  `export-receipt/v1` provenance projection. P1 `read/describe/put` is available only
  through `P1AssetStoreAdapter`.

### Quality Suite

- `freeze_source` requires workspace/revision/Asset/content hashes and validates
  immutable UTF-8 content, BOM/encoding boundaries, Asset metadata aliases, and
  cross-field provenance.
- Diagnostic, Finding, and proposed-candidate projections are canonical UTF-8 JSON
  bytes with stable IDs and SHA-256 digests. Candidate status is `proposed` and
  `publication_eligibility` is `none`.
- `quality-bundle/v1` is explicitly a private in-memory domain projection, not a
  published Core contract. There is no `publish`, staging, Revision, Candidate,
  Publication, database, or file-write callable.

## Runtime gate / Delta

The installable plugin manifest/wheel/worker and durable Job/Attempt/runtime path
remain stopped because the accepted P1 current-pointer/export snapshot integration
and real P2/P3 runtime ports are absent. The precise dependency Delta is:
`coordination/PPA-06/p6b-export-quality/runtime-port-delta-v1.json`.

This candidate therefore claims only the independently complete injected domain
seams and deterministic renderer/quality behavior. It does not claim a UI/download
route or automatic staging/publication.

## Evidence

Raw command output is kept under `evidence/raw/`:

- `pytest-p6b.stdout.txt` / `pytest-p6b.stderr.txt`
- `pytest-p6-writing.stdout.txt` / `pytest-p6-writing.stderr.txt`
- `compileall.stdout.txt` / `compileall.stderr.txt`
- `repeated-render-probe.stdout.txt` / `repeated-render-probe.stderr.txt`
- `git-diff-check.stdout.txt` / `git-diff-check.stderr.txt`
- `validation-exit-codes.txt`
- `changed-paths.txt`
- `git-status-before-delivery.txt`

The exact command, exit code, and observed result are also recorded in
`source-candidate-v1.json`. The source candidate must be reviewed by
`gpt-5.6-sol/max` before P0 considers no-ff integration.
