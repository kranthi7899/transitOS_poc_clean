# Phase 10 owner decisions (2026-10-12)

Design principle (owner): the products are mature and do not make mistakes. We identify the same vehicle across
products, adapt to whatever fields and schema each product provides, and use real data whenever we can match and
retrieve it. We do not invent facts and we do not build machinery to compensate for flaky products. When a field is
simply not provided, the system carries on with the next best option and says what it could not verify.
Standing rule: decide small things ourselves, record each here with one line of reasoning so the owner can overrule it;
ask only when the answer changes what the product does for a user, or cannot be undone.

| ID | Decision | Where | Status | One-line reasoning |
|---|---|---|---|---|
| A185 | Q1: a limit a block declares (`max_vehicle_length_ft`) is checked only against a value the product PROVIDED (native) or derived by a deterministic mapping. Over the limit: excluded. Not provided: an unverifiable item (requires acknowledgement, the detail names `length_ft`), ranked after every verified option, still offered even when it is the only option. An agency default or a "typical for the type" value never decides anything: the class length table is removed. Supersedes A66's lengths and A70/F9 (an assumed-default breach no longer excludes). | `clearance/rules.py::resolve_length`, `clearance/facts.py` (Tables), `tools/config/vehicle_classes.yaml`, golden rows `length_*`, `tests/workflow/test_length_limit_q1.py` | decided (owner) | We never invent a fact; a missing field is said out loud and the option stays available. |
| A186 | Q2: the OP-310 seed edit (shift ends 12:00 so agency-042's run needs an operator swap, A67) is accepted. | `mocks/seed.py` | decided (owner) | It is a fixture choice that makes the demo exercise the swap; no product behaviour depends on it. |
| A187 | Q3: A140 is confirmed: any candidate at or above the fuzzy floor with no automatic MATCH is AMBIGUOUS with ALL candidates and their evidence shown to a person, sorted by score (then id); NO_MATCH only when there is no candidate at all; a vehicle is never attributed automatically below the MATCH bar. | `plumbing/resolver.py::decide`, panel `/unresolved` | decided (owner) | A person decides whenever the evidence is not strong enough, and sees everything we have. |
