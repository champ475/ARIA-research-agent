# Developer shortcuts. Windows users without make: run the commands directly.
.PHONY: eval eval-record eval-baseline test lint lock lock-check loadtest

eval:  ## golden-set eval against the baseline (replay; no network) — PRD LLM-01
	python evals/runner.py --replay --compare evals/baseline.json

eval-record:  ## record missing LLM responses with the configured providers (resumable)
	python evals/runner.py --record

eval-baseline:  ## accept the current replay results as the new baseline (review the diff first)
	python evals/runner.py --replay --out evals/baseline.json

test:
	python -m pytest tests/ -q

lint:
	ruff check . && ruff format --check . && mypy

lock:  ## regenerate the hashed lock files after editing requirements*.txt — PRD SEC-14
	python scripts/lock.py

lock-check:
	python scripts/lock.py --check

loadtest:  ## load + chaos profile and report (needs loadtests/requirements.txt and a throwaway database) — PRD TEST-06
	python loadtests/run.py --database-url $(LOAD_DATABASE_URL) --users 20 --duration 300 --version dev
