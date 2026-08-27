# P4-INT-002 — Immutable plugin worker route gate

P4 batch 2 freezes and validates the v1 Slot/tree boundary without starting a
Worker. The real Dedicated Worker slice requires P0/P2 to publish the immutable
Core URL `/__plotpilot/plugin-worker/<release_id>/<bundle_hash>/worker.js` with
the frozen response CSP. No matching route is present at the accepted baseline.

P4 will not substitute `blob:`, fetch a bundle, or add a development-only route.
Worker lifecycle, watchdog, message/intent/ack, and renderer mounting continue
after the immutable URL provider is integrated.
