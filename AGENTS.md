# AGENTS.md

Guidance for coding agents working in this repository.

## Project Scope
- Main executable: `api_regression_runner.py`
- Purpose: run PDF extraction API regression checks and generate archived reports.
- Test assets:
  - `test payload/` for PDFs
  - `test reference/` for expected JSON

## Core Behavior (Current)
- Supports response shapes:
  - list responses
  - wrapped list responses (`results`, `data`, `items`)
  - single-object response (mapped when one payload file is present)
- Comparison is field-based using JSON leaf values.
- PASS requires:
  - 100% reference field match
  - 0 unexpected fields in response
- 3-run consistency uses response hash stability (repeatability), separate from correctness.

## Safe Working Rules
- Do not modify files in archived output folders unless user explicitly requests cleanup:
  - `response archive/`
  - `Archived Test reports/`
- Keep output format backward-compatible when possible.
- Preserve current CLI flags unless the user asks for interface changes.

## Validation Checklist After Changes
- Run syntax check:
  - `python -m py_compile api_regression_runner.py`
- Run smoke test:
  - `python api_regression_runner.py --runs 1`
- If comparison logic changed, validate detailed diff output:
  - `python api_regression_runner.py --runs 1 --report-failed-fields`

## Documentation Sync
- If behavior, metrics, or CLI options change, update:
  - `README.md`
  - this `AGENTS.md`

