BLUE := \033[1;34m
GREEN := \033[1;32m
RESET := \033[0m
PACKAGE := src/acprouter
PYTEST := uv run --extra dev python -m pytest
RUFF := uv run --extra dev ruff
TY := uv run --extra dev ty
BASEDPYRIGHT := uv run --extra dev basedpyright
MKDOCS := uv run --extra docs mkdocs

.PHONY: tests format check check-coverage save-coverage serve all prod rename

# Hack to allow passing arguments to make commands (e.g. make rename my_project)
ifeq (rename,$(firstword $(MAKECMDGOALS)))
  # use the rest as arguments for "rename"
  RUN_ARGS := $(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS))
  # ...and turn them into do-nothing targets
  $(eval $(RUN_ARGS):;@:)
endif

rename:
	@if [ -z "$(RUN_ARGS)" ]; then \
		echo "Error: Name is not provided. Usage: make rename my_awesome_project"; \
		exit 1; \
	fi
	@printf "$(BLUE)==>$(RESET) Renaming acprouter to $(RUN_ARGS)...\n"
	@python3 scripts/rename_workspace.py $(RUN_ARGS) || python scripts/rename_workspace.py $(RUN_ARGS)
	@printf "$(GREEN)✔ Project renamed to $(RUN_ARGS) successfully!$(RESET)\n"

format:
	@printf "$(BLUE)==>$(RESET) Formatting code with ruff...\n"
	@$(RUFF) format
	@printf "$(GREEN)✔ Formatting complete.$(RESET)\n"

check:
	@printf "$(BLUE)==>$(RESET) Running ruff checks and fixing issues...\n"
	@$(RUFF) check --fix --unsafe-fixes
	@printf "$(BLUE)==>$(RESET) Type checking with ty...\n"
	@$(TY) check
	@printf "$(BLUE)==>$(RESET) Type checking with basedpyright...\n"
	@$(BASEDPYRIGHT)
	@printf "$(GREEN)✔ Checking complete.$(RESET)\n"

tests:
	@printf "$(BLUE)==>$(RESET) Running tests with pytest...\n"
	@$(PYTEST)
	@printf "$(GREEN)✔ Tests complete.$(RESET)\n"

check-coverage:
	@printf "$(BLUE)==>$(RESET) Running pytest with coverage...\n"
	@$(PYTEST) --cov=$(PACKAGE) --cov-branch --cov-report=term-missing --cov-fail-under=85
	@printf "$(GREEN)✔ Coverage complete.$(RESET)\n"

save-coverage:
	@printf "$(BLUE)==>$(RESET) Saving full coverage report...\n"
	@$(PYTEST) -p pytest_cov --cov=. --cov-branch --cov-report=term-missing > COVERAGE
	@printf "$(GREEN)✔ Coverage report saved to COVERAGE.$(RESET)\n"

serve:
	@printf "$(BLUE)==>$(RESET) Serving MkDocs at http://127.0.0.1:8000 ...\n"
	@$(MKDOCS) serve --dev-addr 127.0.0.1:8000

all: format check

prod: tests format check check-coverage
