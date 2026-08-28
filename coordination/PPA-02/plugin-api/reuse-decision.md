# NW-P2-PLUGIN-API-G2 reuse decision

## Decision

Use the accepted H_C implementation directly and add only a thin read-only
adapter in the owned Plugin API subtree.

- Release authority: `PackageStore.list_releases(verify=True)` and
  `PackageStore.get(...)`.
- Worker authority: `PluginProcessSupervisor.status(...)` and its closed
  `WorkerStatus` DTO.
- Manifest validation: existing `plugin-manifest/v1` validator.
- Transport profiles: the existing v1 identifier, exact SemVer and lowercase
  SHA-256 profiles.
- HTTP composition: existing FastAPI/Starlette runtime; no new dependency.

No frozen G1 source was reused. The frozen review evidence was read only to
identify negative surfaces.

## Knowledge evidence

1. `kb-plotpilot-pluginized-nw-p1-core-http-runtime-seam-blocker-20260828`
   - Path: `C:\Users\Administrator\Documents\Local-KnowledgeBase\20_项目档案\PlotPilot-Pluginized\交付记录\2026-08-28-NW-P1-CORE-HTTP-01-runtime-seam-blocker.md`
   - SHA-256: `14936673ecdd5696d01f520780ebdc47a29b59dcb9bc39a7374a15bc9581a52d`
   - Status: `draft`; used only as evidence that an unaccepted runtime seam
     must produce a Delta rather than a fake adapter.
2. `ppa-nw-p2-lifecycle-f012-formal-migration-20260828`
   - Path: `C:\Users\Administrator\Documents\Local-KnowledgeBase\20_项目档案\PlotPilot-Pluginized\交付记录\2026-08-28-PPA-NW-P2-Lifecycle-F012-formal-migration.md`
   - SHA-256: `85436d276f07ca3a348e35167eeef71711a700c87b1787c27833074e17ad8755`
   - Status: `delivered`; confirms the accepted formal Lifecycle migration
     remains the authority and must not be recreated in this API slice.

The callable security KB route result was adjacent rather than project-scoped;
its route/method matrix guidance was used only to strengthen negative tests.

## Local tool library

The bounded selector returned video, Git rescue and hardware tools. None
provides FastAPI façade, PackageStore, Supervisor, schema or route-inventory
capability, so copying one would increase dependencies and adaptation cost.

## GitHub decision

Skipped by the frozen taskbook because the current project already contains
all required authorities, validators and HTTP runtime. No missing helper was
proven, so an external search had no additional reuse benefit or supply-chain
justification.
