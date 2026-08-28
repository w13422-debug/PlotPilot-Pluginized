# Wave C P0 integration source candidate — parent-receipt follow-up

Status: `H_C_F001_SOURCE_INTEGRATED_PENDING_FRESH_SOL`

- Original Wave C source candidate: `a83af804a092a8274e8b7a71273aeb5cee3c4cd1`; evidence candidate: `f5e7feaee17ed7c331eeb63a5fe185df7e2e8b2f` (preserved, now superseded).
- Accepted follow-up source: `108fa8378ff418d2b4f5f4d5ed3ec71218354f85`; source tree: `20ce37796a615c8cad064d39f389c6141bc67830`.
- Follow-up no-ff merge: `8748957f97b65534b295e70dca7cd4a4d9378479` with ordered parents `f5e7feaee17ed7c331eeb63a5fe185df7e2e8b2f` and `108fa8378ff418d2b4f5f4d5ed3ec71218354f85`.
- First-parent delta: 7 paths, byte-identical to the accepted source aggregate; no contract, public SDK, or public route drift.
- Original source union: 138 paths; follow-up overlap: 5; final non-evidence source union and actual delta: 140; out-of-union: 0.
- Rejected source `7948da473f3e5066b13d3fe6239561572bce86b9` is neither an ancestor nor the second parent.
- Integrated validation: Backup 74 passed; P1 159 passed; P3 39 passed; Ruff, compileall, schema determinism, manifest determinism, and diff check all exited 0.
- The first diagnostic harness observed the Backup test exit 0 but failed while echoing decoded output to CP936; the corrected UTF-8 evidence harness reran and persisted the complete passing transcript.
- No release tag or push was created. Fresh Hubble Sol integrated acceptance remains pending; this record does not claim final Wave C PASS.
