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

1. `com.plotpilot.export-suite` domain source with license, request schema, and
   deterministic renderers. Runtime packaging is stopped by formal Contract
   Delta `P6-CD-EXPORT-CURRENT-REVISIONS-001`; no fake manifest/wheel/worker is
   shipped.
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

Final raw result after review remediation: `18 passed`.

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
- No `plugin.json`, worker, or installable release wheel until P0 resolves the
  current-revision contract and P1/P2/P3 real ports are integrated.
- No Job/stream/checkpoint/Provider/Broker until P3.
- No fixed UI Slot or browser download until P4.
- No Story State composition until P5.
- No Candidate/Publication, no second正文 truth, no fake runtime/UI/data, and no
  desktop/EXE/installer build.

P0 should integrate these commits with `--no-ff` in order after reviewing the
real dependency gates above.

## Independent review closure

The first focused review returned `REQUEST_CHANGES` with findings P6-F01..F08.
The bounded remediation removed the false runtime/package claim, raised the
formal Contract Delta, added CJK-font fail-closed PDF rendering, changed
single-chapter selection to exact `document_id`, replaced ambiguous context
fingerprinting with canonical structured JSON, restored Autopilot audit loop/
review/completion decisions, and adapted the donor's actual four language
pattern families. Final recheck evidence is recorded in
`integration-ready-v1.json`.
