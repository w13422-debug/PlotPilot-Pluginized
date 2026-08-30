# Wave D five-source integration evidence

- Start H_C: `8e3d3a7268e0d28559a7212790e3ab9f22c1453a`
- Five-source merge tip: `08cd1726dce76419408af786789b28c7a09f8e3c`
- Merge-tip tree: `ac2c9bdacf657864d85e4f6828446c057835ee88`
- Sources: `28b2f7e307d166e85d5a715b9992eed5f0701151, 7b6e104c39d89417a3d7594a6d75ffc83f5b3371, 2b096d3dc0810bb73947f4cb2c9c8ea48372d141, fad52f0b91a477da4be7695f20ab6ff7a22f88d8, eda1a7e34f73c78b9819c390653906b70afa56af`
- Merge commits: `93526465b7b59353e769119c0f28d2110659e050, 7ae521f2090a3a2a14cc586eeae485c0b612eda3, 290ed63c7b0cb602c96c7718f5b43a5b707f3026, d6c7115024070b00f2c6cc69febdf18348ee8652, 08cd1726dce76419408af786789b28c7a09f8e3c`
- Source path union: **130**, out-of-set: **0**
- Evidence status: **BLOCKED_INTEGRATION_VALIDATION**

## Blocking results

1. `WAVE-D-INTEGRATION-SEMANTIC-001`: Story State regression exits 2 because accepted Prompt Runtime G2 removed the `run_skill_chain` package export still consumed by Story State G3 test support.
2. `WAVE-D-INTEGRATION-RUFF-001`: affected-path Ruff exits 1 with two import-order errors and one unused import in accepted P1 files.

No source was edited after the five no-ff merges. This evidence commit records the immutable blocked integration state; it does not claim merge eligibility or Wave D PASS.
