# Remaining Delta — P3B Generation 2

## External production activation gate

G2 deliberately does not fabricate a second authority. Production composition must provide both of the following in the existing P1-owned durable transaction domain:

1. the Broker reservation ledger adapter implementing `lookup/get_reservation/reserve/attach_envelope/attach_child/record`; and
2. atomic `create_or_recover_child` behavior for the same reserved operation key.

The production `CapabilityBroker` constructor rejects the exported test-only InMemory authorities and fails closed when authoritative ports are absent. `CapabilityBroker.for_test` is the only constructor path that supplies those InMemory authorities.

No public contract/SDK/Jobs/Events change is requested by this Delta. Central coordination owns any later activation work outside the P3B write set.

## External gates

- Reuse reviewer `01a0438c-f40b-7ec1-b943-f2a9a711b375` for the full G2 matrix.
- F008 remains WITHDRAWN and untouched.
- Central exact HEAD/write-set/clean-tree verification and P0 `no-ff` integration remain pending.
- This source candidate does not claim PASS, Finding closure, or merge eligibility.
