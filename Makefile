# Developer shortcuts. Windows users without make: run the commands directly.
.PHONY: eval eval-record eval-baseline test lint

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
