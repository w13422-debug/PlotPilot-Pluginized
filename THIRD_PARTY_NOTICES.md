# Third-party notices

P0 does not vendor or copy third-party source code.  The PlotPilot baseline's
existing license text is retained in [`LICENSE`](LICENSE).  M0 tooling uses
the following external runtimes/libraries:

- Python: `jsonschema`, `rfc8785`, `pytest`, `fastapi`, `uvicorn`, `pydantic`,
  `httpx`, and their runtime dependencies.
- Frontend: the packages declared by `frontend/package.json` and resolved by
  `frontend/package-lock.json`/`pnpm-lock.yaml`.
- Browser evidence: Playwright and its bundled Chromium runtime.

The exact versions, license metadata, upstream URLs, package integrity values,
and the complete lock-resolved npm package inventory are machine-recorded in
`docs/deliveries/PPA-00/license-ledger.json`.  A missing or ambiguous license
entry is a merge blocker; it must be resolved with a Dependency Delta and an
updated ledger before integration.
