.PHONY: llm-spend llm-smoke llm-report setup test lint verify schemas demo report data data-check kafka-up kafka-down kafka-status kafka-topics kafka-purge kafka-test verify-kafka postgres-up postgres-down postgres-status postgres-purge postgres-test verify-postgres
setup:
	uv sync
test:
	uv run pytest -q
lint:
	uv run ruff check src tests
schemas:
	uv run python -m transitos.contracts.export contracts-schema
# Track F scenarios against the REAL integrated system (exit 1 on any failure)
report:
	uv run python -m transitos.verification.report --world transitos.integration.world:make_world --out verification-report.json
demo:
	uv run python -m transitos.integration.demo
verify: schemas lint test report data-check

# ---- Phase 10 external-data pipeline (the only target that touches the network is `data`; needs `uv sync --extra data`) ----
# data: fetch (polite, manifest hosts only) -> verify (writes data/FETCH_REPORT.md) -> load data/reference.db -> derive data/derived/*.yaml
data:
	uv run --extra data python -m transitos.data.fetch
	uv run --extra data python -m transitos.data.verify
	uv run --extra data python -m transitos.data.load
	uv run --extra data python -m transitos.data.derive
# data-check: OFFLINE; reads only committed files (data/derived, fixtures, config) and validates them
data-check:
	uv run python -m transitos.data.check

# ---- Kafka profile (project-local Apache Kafka under .local/, see scripts/kafka.sh) ----
KAFKA ?= 127.0.0.1:9092
KAFKA_ENV = TRANSITOS_BUS=kafka TRANSITOS_KAFKA=$(KAFKA)
kafka-up:
	scripts/kafka.sh up
kafka-down:
	scripts/kafka.sh down
kafka-status:
	scripts/kafka.sh status
kafka-topics:
	scripts/kafka.sh topics
kafka-purge:
	scripts/kafka.sh purge
# Kafka-marked tests only (bus contract suite on KafkaBus + Kafka-specific tests incl. broker restart)
kafka-test:
	TRANSITOS_KAFKA=$(KAFKA) TRANSITOS_KAFKA_ADMIN=1 uv run --extra kafka pytest -q -m "kafka or kafka_admin"
# The whole system on Kafka: every System/World uses KafkaBus; full test suite + Track F report
verify-kafka:
	scripts/kafka.sh status
	uv run --extra kafka ruff check src tests
	$(KAFKA_ENV) TRANSITOS_KAFKA_ADMIN=1 uv run --extra kafka pytest -q
	$(KAFKA_ENV) uv run --extra kafka python -m transitos.verification.report --world transitos.integration.world:make_world --out verification-report-kafka.json
	scripts/kafka.sh purge

# ---- Postgres profile (project-local PostgreSQL under .local/, see scripts/postgres.sh) ----
PG_ENV = TRANSITOS_DB=postgres
postgres-up:
	scripts/postgres.sh up
postgres-down:
	scripts/postgres.sh down
postgres-status:
	scripts/postgres.sh status
postgres-purge:
	scripts/postgres.sh purge
# Postgres-marked tests only (storage/bus-plumbing contract suites on PgDb + Postgres-specific tests incl. server restart)
postgres-test:
	TRANSITOS_PG_ADMIN=1 uv run --extra postgres pytest -q -m "postgres or postgres_admin"
# The whole system on Postgres: every System/World stores in Postgres (bus, outbox, crosswalk, audit, run state,
# idempotency, kill switch, LangGraph checkpoints); full test suite + Track F report
verify-postgres:
	scripts/postgres.sh status
	uv run --extra postgres ruff check src tests
	$(PG_ENV) TRANSITOS_PG_ADMIN=1 uv run --extra postgres pytest -q
	$(PG_ENV) uv run --extra postgres python -m transitos.verification.report --world transitos.integration.world:make_world --out verification-report-postgres.json
	scripts/postgres.sh purge

# ---- Claude Haiku as the reasoner (optional; owner cap: never above $10, default $3; see docs/LLM.md) ----
# The key comes from ANTHROPIC_API_KEY or the git-ignored .env.local. Nothing here runs in `make verify`.
llm-spend:
	uv run python -m transitos.workflow.reasoning_anthropic spend
# 2 live checks, a few thousandths of a dollar
llm-smoke:
	TRANSITOS_LIVE_LLM=1 uv run --extra llm pytest -q -m llm_live tests/llm
# the whole scenario report with Haiku as the reasoner (about 50 calls, a few cents)
llm-report:
	TRANSITOS_REASONER=anthropic uv run --extra llm python -m transitos.verification.report --world transitos.integration.world:make_world --out verification-report-llm.json
