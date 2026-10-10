# Developer commands. Works on Linux/macOS and on Windows with GNU make
# (e.g. `choco install make` or Git Bash). Every target uses the project venv.

ifeq ($(OS),Windows_NT)
  VENV_BIN := .venv/Scripts
else
  VENV_BIN := .venv/bin
endif

PYTHON ?= python
PY := $(VENV_BIN)/python
CODE := app.py incident_suite tests

.PHONY: setup run test smoke lint format types deadcode security audit check lock lock-check eval

## setup: create .venv, install pinned deps plus dev tools, install git hooks
setup:
	$(PYTHON) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements-dev.txt
	$(PY) -m pip install --no-deps -e .
	$(PY) -m pre_commit install

## run: start the Streamlit app on http://localhost:8501
run:
	$(PY) -m streamlit run app.py

## test: unit and integration tests with coverage (no API key or network needed)
test:
	$(PY) -m pytest --cov --cov-report=term-missing

## smoke: opt-in live checks against OpenRouter (needs OPENROUTER_API_KEY, costs a fraction of a cent)
smoke:
	$(PY) -m pytest -m live -v

lint:
	$(PY) -m ruff check $(CODE)
	$(PY) -m ruff format --check $(CODE)

format:
	$(PY) -m ruff check --fix $(CODE)
	$(PY) -m ruff format $(CODE)

types:
	$(PY) -m mypy

deadcode:
	$(PY) -m vulture

security:
	$(PY) -m bandit -q -c pyproject.toml -r app.py incident_suite

audit:
	$(PY) -m pip_audit -r requirements.txt --progress-spinner off

## check: everything CI runs that needs no key (gitleaks runs in pre-commit and CI)
check: lint types deadcode security test audit

## lock: regenerate pinned requirements from pyproject.toml (run on Linux / CI Python 3.12)
lock:
	$(PY) -m piptools compile --quiet --strip-extras --output-file requirements.txt pyproject.toml
	$(PY) -m piptools compile --quiet --strip-extras --extra dev --constraint requirements.txt --output-file requirements-dev.txt pyproject.toml

## lock-check: fail if the lock files are out of date with pyproject.toml
lock-check: lock
	git diff --exit-code -- requirements.txt requirements-dev.txt

## eval: accuracy report over the labelled sample logs (needs OPENROUTER_API_KEY)
eval:
	$(PY) -m eval.run_eval
