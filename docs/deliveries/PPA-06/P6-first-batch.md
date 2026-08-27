# PPA-06 Writing — first dependency-ready batch

## Identity

- Branch: `codex/ppa-06-writing`
- Accepted base: `42123d1a5126bb2bef31304b0498e2e7def9183e`
- Construction commits:
  - `59b2a584` — deterministic Export Suite core and tests
  - `8a8c9879` — dependency-free Chapter/Autopilot/Quality decisions
- Donor: `C:\Users\Administrator\Desktop\写作资料汇总\PlotPilot-update-v4.6.0`
  at specified source commit `1c481237b6fa32ef5f85d7f8da4cb16f366cd4f0`
  (read-only; donor-local push remains `DISABLED`).

## Reuse decision

- **Thin adaptation:** the existing `application/core/services/export_service.py`
  format, sort, filename, MIME, and encoding behavior was extracted behind
  immutable current-revision values. Repository writes were not copied.
- **Reference only:** `AutopilotRecoveryPolicy` contributed its stable stage
  vocabulary. DB cleanup, daemon ownership, and resume behavior wait for P3.
- **Thin adaptation:** deterministic language-style guardrails now return
  immutable Findings only; no enforcement/write path was copied.
- Local tool selection returned unrelated candidates (`stack_match=60` but no
  export capability match), so none was copied. The exact in-repository donor
  had lower risk and adaptation cost. The governing construction specification
  already records no additional GitHub reuse benefit for this frozen parity
  work.

## Delivered behavior

1. P0 v1-shaped `com.plotpilot.export-suite` source package with closed
   manifest, package hash manifest, license, schema, deterministic renderers,
   and a real callable domain entrypoint.
2. Immutable current-revision hash verification, stable chapter-number order,
   whole-book and single-chapter legacy filename behavior, four source-baseline
   formats, exact MIME/extension mapping, Markdown UTF-8 without BOM, and
   provenance hashes.
3. Export exceptions cannot mutate the frozen body input; no repository or
   Publication path exists in the plugin.
4. Deterministic Context/Skill freeze, explicit-enable Autopilot stage order,
   and read-only Quality Findings.

## Verification

Command:

```text
python -m pytest tests/p6-writing -q
```

Raw result: `15 passed in 1.12s`.

Additional checks:

```text
python -m compileall -q first-party-plugins/export-suite/backend/src first-party-plugins/chapter-workflow/backend/src first-party-plugins/autopilot/backend/src first-party-plugins/quality-suite/backend/src
git diff --check
```

Both exited `0`. Raw evidence is under
`docs/deliveries/PPA-06/evidence/raw/`.

## Write-set proof

Every path from base through `8a8c9879` is within:

- `first-party-plugins/{export-suite,chapter-workflow,autopilot,quality-suite}/**`
- `tests/p6-writing/**`

This delivery/evidence and coordination note add only the owned
`docs/deliveries/PPA-06/**` and `coordination/PPA-06/**` paths. Exact paths are
captured in `evidence/raw/changed-paths.txt`.

## Not claimed

- No durable Asset or re-download until P1's real Asset/current-revision port.
- No installable release wheel until P2's real package pipeline.
- No Job/stream/checkpoint/Provider/Broker until P3.
- No fixed UI Slot or browser download until P4.
- No Story State composition until P5.
- No Candidate/Publication, no second正文 truth, no fake runtime/UI/data, and no
  desktop/EXE/installer build.

P0 should integrate these commits with `--no-ff` in order after reviewing the
real dependency gates above.
