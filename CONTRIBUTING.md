# Contributing

## Setup

Install the local development environment with `uv`:

```bash
uv sync --extra dev --extra docs
```

Install pre-commit hooks:

```bash
uv run pre-commit install
```

## Development Commands

- `make format`
  Runs `uv run --extra dev ruff format`.
- `make check`
  Runs `uv run --extra dev ruff check --fix --unsafe-fixes`, `uv run --extra dev ty check`, and `uv run --extra dev basedpyright`.
- `make tests`
  Runs `uv run --extra dev python -m pytest`.
- `make check-coverage`
  Runs `uv run --extra dev python -m pytest --cov=src/acprouter --cov-branch --cov-report=term-missing --cov-fail-under=90`.
- `make prod`
  Runs tests, formatting, lint/type checks, and the coverage gate.

## Recommended Pre-PR Flow

```bash
make format
make check
make tests
make check-coverage
```

If documentation changed, also run:

```bash
uv run --extra docs mkdocs build --strict
```
