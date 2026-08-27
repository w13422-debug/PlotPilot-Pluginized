# P5 integration gates

The dependency-ready slice intentionally has no private host adapter.

| Gate | Required owner contract/port | P5 continuation |
|---|---|---|
| P1 | Core Story Document/Relation reads plus Candidate staging, Publication CAS and history lookup | Map immutable planner/story-state drafts to `candidate-item/v1`; read published revisions for projections |
| P2 | Installed Release/Generation, frozen Plan and Prompt/Skill runtime | Package the declared entrypoints and bind the immutable identifiers carried by `Binding` |
| P3 | Job, Provider/Broker invocation and provenance receipt | Execute generation and attach the public `result-bundle/v1` producer/receipt fields |
| P4 | Fixed Planning/Story State pages and Core Candidate controls | Invoke the registered capabilities; do not place authority or publication controls inside plugin UI |
| P6 | Published Story State capability | Supply only P1-published revision references for chapter context and accept chapter-settlement proposals as candidates |

Until these ports exist, P5 does not write a Core database, construct a second
Bible/body store, invent an external schema, or ship a substitute runtime.
