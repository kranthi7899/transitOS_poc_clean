# Assumptions register (doubles as Phase 0 question list for the client)

| ID | Assumption | Where | Status | Question for the real product owner |
|---|---|---|---|---|
| A1 | Maintenance product accepts an Idempotency-Key header | maintenance mock | open | Do FASTER writes support an idempotency key? |
| A2 | Scheduling has no write endpoint for pulling a vehicle | scheduling mock | open | Is there a write API for vehicle removal in Ecolane/TripShot? |
| A3 | Driver hours/qualifications only via daily CSV | workforce mock | open | Which system holds operator hours and qualifications? |
| A4 | LangGraph re-runs the interrupt node from its start on resume | workflow | open (CHECK) | Confirm in current LangGraph docs |
| A5 | Scheduling is the system of record for vehicle availability | workflow | open | Which product owns vehicle availability? |
| A6 | Operator max hours/day = 10 | policy | open | Agency-specific labor rules? |
| A7 | PM urgent threshold = 1000 mi overdue | policy | open | Agency PM policy? |
| A8 | Telematics webhook signature scheme | telematics mock | open | Real auth scheme? |
| A9 | One active run per vehicle | workflow | open | Operational reality? |
| A10 | Local profile (SQLite + in-process bus) is behaviourally equivalent to Kafka/Postgres for POC | plumbing | open | n/a (internal) |
