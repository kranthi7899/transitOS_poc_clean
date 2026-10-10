.PHONY: setup test verify schemas
setup:
	uv sync
test:
	uv run pytest -q
schemas:
	uv run python -m transitos.contracts.export contracts-schema
verify: schemas test
