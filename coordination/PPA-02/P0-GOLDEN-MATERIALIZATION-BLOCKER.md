# P0 integration evidence gap: golden working-tree bytes

- Observed on base: `42123d1a5126bb2bef31304b0498e2e7def9183e`
- Scope stopped: direct repository-golden acceptance proof only; P2 implementation does not edit P0 contracts.
- `contracts/golden/package/data/rules.json` is absent from this worktree.
- `contracts/golden/skill/files.sha256`, `prompt.txt`, and `skill.json` are materialized with CRLF although v1 requires exact LF bytes.
- `python tools/integration/verify_contracts.py --all` also reports `contracts/manifest-v1.json` drift on the non-ASCII workspace path.
- `python -m pytest tests/contract/test_goldens.py -q` result: `1 failed, 1 passed`; failure is missing `contracts/golden/package/data/rules.json`.

P0 action requested: restore the accepted golden corpus as exact bytes and re-run the P0 verifier from the integrated checkout. No Contract Delta is requested; the existing v1 semantics are sufficient.
