# PPA-04B bounded remediation handoff

## Frozen review input

- Blocked candidate parent: `5a85cc4801d245f81a5d3a970ac471cde1bd6895`
- Finding Manifest: `p4b-finding-manifest-20260827.json`
- Manifest SHA-256:
  `1fb985e2c8f3d5fdbe02f878d1cf870ab1b6b457b13df9afce4859db5be57bdf`
- Required same reviewer task: `01a0436d-6d77-7a83-8f8b-fe54fb8bcf51`

## Per-ID candidate evidence

| Finding | Implementation | Focused regression |
|---|---|---|
| `P4B-SOL-F-001` | `uiSession.ts` constructs or validates Host events against the installed tree, exact current render/freshness identity, and declared action/component event rules; `workerHost.ts` exposes the gated send paths. | `P4B-SOL-F-001 gates Host events by installed tree, exact freshness and declared action` covers no-tree, stale render, freshness mismatch and undeclared action/event. |
| `P4B-SOL-F-002` | Session render/intent high-water marks are initialized from init baselines; incoming values must be strictly greater; supplied sessions must match configured baselines; restart carries both fences into the replacement session/init. | `P4B-SOL-F-002 enforces nonzero high-water, custom-session consistency and restart replay fencing` covers nonzero baselines, session mismatch, restart and render replay. |
| `P4B-SOL-F-003` | The intent ledger distinguishes pending from completed entries. An identical in-flight duplicate is coalesced, and `completeValidatedIntent` atomically records one immutable ACK for exact replay. | `P4B-SOL-F-003 coalesces identical pending intents and replays one immutable ACK` verifies one dispatch, no premature ACK, one completion and object-identical frozen replay. |
| `P4B-SOL-F-004` | `parsePluginWorkerUrl` accepts only primitive strings before canonical route/origin checks; URL objects are never normalized into accepted input. | `P4B-SOL-F-004 accepts only primitive canonical same-origin release/hash Worker strings` contrasts rejected dot-segment strings and normalizing URL objects with accepted canonical strings. |

## Boundary

No public contract, dependency, view, route, store, Core module or desktop
artifact was added or changed. This file records remediation evidence only. It
does not mark any Finding `PASS`, close any Finding, or grant merge eligibility;
those decisions remain with the required same reviewer and P0 integrator.
