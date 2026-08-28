# NW-P2-PLUGIN-API-G2 source delivery

## Scope

This source generation starts from exact H_C
`8e3d3a7268e0d28559a7212790e3ab9f22c1453a` and adds a strict read-only
Plugin API. It does not mount the router, mutate public contracts or simulate
any stopped authority.

Actual source routes:

1. `GET /api/v1/plugins/releases`
2. `GET /api/v1/plugins/releases/{plugin_id}/{version}` with optional exact
   lowercase `package_hash`
3. `GET /api/v1/plugins/workers/{worker_id}/status`

The actual `APIRoute` inventory is normalized by
`route_inventory(...)`; `build_composition_delta(...)` refuses to generate a
Delta if the inventory differs from the independent three-route allowlist.

## Authority boundaries

- Release list and exact lookup call the real H_C `PackageStore` with
  verification enabled.
- Only the exact H_C missing-release sentinel
  `FileNotFoundError((plugin_id, version))` with no `errno` or `filename` is
  translated to local typed 404.
- Registry corruption, missing package bytes, integrity failures and other
  `FileNotFoundError` shapes remain typed 500, never 404. There is no broad
  `OSError` handler.
- Worker status calls `PluginProcessSupervisor.status`; only `None` is local
  typed 404. Internal exceptions are not reclassified as absence.
- Release and worker authority results are projected through closed local
  DTOs. Manifest validation, finite JSON, canonical Base64, exact ID/SemVer/hash
  profiles and range checks fail closed.

`plugin-api-local-error/v1` is explicitly source-local. P0 still owns public
error-family adjudication and publication.

## SPA boundary

`PluginApiBoundaryMiddleware` derives its decisions from the real route
objects. Under `/api/v1/plugins` it prevents unknown paths and wrong methods
from reaching the legacy SPA catch-all without adding fake business routes to
the allowlist. P0 must install the middleware and mount the accepted router
before `/{full_path:path}` in `NW-P0-RUNTIME-COMPOSITION-02`.

## Composition Delta

The committed Delta at
`coordination/PPA-02/plugin-api/composition-delta-v1.json` is generated from
the actual router and names all required stops:

- P0 authoritative **Workspace Plan-switch CAS**;
- exact node **NW-P0-RUNTIME-COMPOSITION-02**;
- **public error-family gate**;
- all P1 shared transaction/Event/qualification/Settings/retirement and
  Candidate/Publication seams;
- app mount, external pin release and public schema/SDK changes.

Every source route is implemented but remains `integration_state=stopped`
until P0 closes these gates.

## Finding coverage implemented for external review

- F-001: no Settings route, port or mutation.
- F-002: no LKG promotion or caller-provided qualification evidence.
- F-003: no safe-mode command or external pin releaser.
- F-004: retained request and authority data are closed, validated projections.
- F-005: exact ID, SemVer, lowercase SHA-256 and Base64 profiles.
- F-006: concrete true-missing 404 versus corrupt-store 500 boundary.
- F-007: actual-inventory-derived Delta with every named P0 gate.
- F-008: decisive concrete store, authority drift, route/method and SPA tests.

These are implementation claims only. Finding closure and source acceptance
remain reserved for a fresh Sol/max reviewer and the controller.

## Directed validation

- Plugin API: `55 passed in 0.68s`.
- Lifecycle + Supervisor: `171 passed in 5.11s`.
- Compileall: exit `0`.
- Contract manifest: `contract manifest is deterministic (141 files)`.
- Ruff owned paths: `All checks passed!`.
- `git diff --check`: exit `0`, no stdout.

Raw evidence is under `docs/deliveries/PPA-02/plugin-api/evidence/raw/` and the
machine summary is `evidence/verification.json`.

## Dependency and stop state

Dependencies are accepted H_C PackageStore/Lifecycle/Supervisor, followed by
Plugin Management G2, Workspace Plan-switch CAS, runtime composition and the
public error-family gate. Installation/Generation/retirement reads and every
plugin mutation remain stopped. No public contract, SDK, `app.py`, manifest or
lock was changed.
