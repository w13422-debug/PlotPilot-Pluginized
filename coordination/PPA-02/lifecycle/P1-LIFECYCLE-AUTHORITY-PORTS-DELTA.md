# P1 Delta — lifecycle authority composition ports

## Identity

- Node: `NW-P2-LIFECYCLE-02`
- Base: `7805be2c2566aa1e63d3cdc6784a84f1aea8f463`
- Owner required: P1 Core authority/composition
- P2 status: callback seams are fail-closed; affected production completion
  slices remain stopped until P1 supplies the authorities below.

## Required host-internal ports

No new public Plugin SDK, wire schema or manifest dependency is requested.
P1 must compose the existing P2 lifecycle repository on the same authoritative
SQLite connection and provide required, non-no-op callbacks for:

1. Core Event writing for `plugin.generation.changed` on current commit and
   rollback, and `plugin.release.retiring` on the first retirement CAS.  The
   aggregate mutation, transition/revision and Event must commit or roll back
   together; SSE broadcast happens only after commit.
2. Qualification authority lookup after restart health/minimal smoke.  The
   verified record must bind its stable qualification ID, target Generation,
   health-result Asset and passing status.  Caller-provided Maps and the
   pre-commit isolated-health result are not LKG authority.
3. Candidate/Publication retirement barrier.  Before package deletion it must
   prove that every Candidate retaining the release has frozen Core-owned
   payload/hash, target/base/write-set, release/package identity, RunSnapshot
   and provenance, and that later Publication needs no plugin execution.
   The callback returns a release-bound immutable decision with stable blocker
   IDs; a naked caller boolean is not authority.

## Stopped slices

- Production LKG promotion remains stopped without the qualification authority.
- Production rollback/current mutation remains stopped without the Event writer.
- Physical package deletion and `retired` completion remain stopped without the
  Candidate/Publication barrier.  A failed barrier stays `retiring`, preserves
  package bytes and records internal `needs_attention`; P2 does not extend the
  closed public retirement schema.

The local tests may inject transaction-aware fakes solely to prove atomic
rollback and fail-closed behavior.  They are not production authority evidence.
