PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: venv install run test lint fmt typecheck cov all

venv:
	$(PY) -m venv $(VENV)

install: venv
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

run:
	$(BIN)/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	$(BIN)/pytest

cov:
	$(BIN)/pytest --cov=app --cov-report=term-missing --cov-fail-under=80

lint:
	$(BIN)/ruff check .

fmt:
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

typecheck:
	$(BIN)/mypy --strict app

all: lint typecheck cov
