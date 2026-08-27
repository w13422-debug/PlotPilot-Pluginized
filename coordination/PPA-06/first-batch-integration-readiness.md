# P6 first-batch integration readiness

## Ready now

- `export-suite` accepts immutable, hash-verified current-revision records and
  renders the four formats present in the v4.6.0 source baseline:
  EPUB, PDF, DOCX, and Markdown.
- Ordering, legacy filename sanitization, MIME/extension mapping, Markdown
  UTF-8-without-BOM behavior, source revision provenance, and failure purity
  have targeted tests.
- `chapter-workflow` freezes ordered Context/Skill provenance without starting
  a Job or creating a Candidate.
- `autopilot` exposes only the deterministic baseline stage order and rejects
  implicit enablement.
- `quality-suite` performs read-only deterministic checks and returns Findings;
  it has no publication or body-write path.

## Real dependency gates (not replaced locally)

| Owner | Required integration | P6 behavior until ready |
|---|---|---|
| P1 | list/read current document revisions; create durable Asset with hash/MIME/size/logical role/provenance; Candidate/Publication | Export renderer consumes frozen values only. It does not persist an Asset or claim browser re-download. Chapter/Quality do not publish. |
| P2 | package build/install, Generation/Plan/Skill runtime, framed worker adapter | Source package and manifest are integration-ready; no private runtime is added. The release wheel must be produced by the real package pipeline. |
| P3 | durable Job/stream/checkpoint/Provider/Broker and host callbacks | No local Job ledger, fake high-water mark, checkpoint, or Broker exists. |
| P4 | fixed Slot contribution, Candidate controls, browser download | `ui` remains `null`; there is no fake button or local download substitute. |
| P5 | published Story State read and chapter-settlement capability | No Story State import or local mirror exists. |

The P0 SDK currently exposes only `read_asset/create_asset/stage_candidate/
commit_checkpoint`; it does not expose the ordered current-revision query needed
by Export. This batch therefore stops at the frozen DTO boundary. If P0 chooses
to add that public surface, P0 owns the contract change; P6 will consume it in
a later integration batch.

## Capability-owner boundary

`validate_selection()` rejects both zero-owner and dual-owner states. The
actual workspace capability flag remains Core/Platform state and is not stored
by P6. Legacy fallback and deletion remain gated on real parity and browser
download evidence.
