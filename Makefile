.PHONY: install lint format type security test build check all clean scan-tls

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

type:
	uvx --with "httpx[http2]" ty check src tests

security:
	uv run bandit -c pyproject.toml -r src

test:
	uv run coverage run -m unittest discover -s tests -v
	uv run coverage report

build:
	uv build
	uv run python scripts/check_zero_deps.py

scan-tls:
	bash scripts/scan_tls.sh

check: lint type security test

all: check build

clean:
	rm -rf dist build .coverage .mypy_cache .ruff_cache *.egg-info src/*.egg-info
