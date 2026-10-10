.PHONY: setup test lint verify schemas demo report
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
verify: schemas lint test report
