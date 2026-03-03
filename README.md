# PDF Extraction API Regression Runner

This repository contains a Python CLI script to regression-test a PDF extraction API against JSON reference files.

## What It Does
- Sends PDFs from `test payload/` to the configured API endpoint.
- Compares each API response with the matching reference JSON in `test reference/`.
- Produces:
  - archived raw API responses in `response archive/<timestamp>/`
  - a human-readable test report in `Archived Test reports/<timestamp>/test_report.txt`

## Requirements
- Python 3.9+
- Dependencies from `requirements.txt`

Install dependencies:

```powershell
pip install -r requirements.txt
```

## Quick Start
Run one test cycle:

```powershell
python .\api_regression_runner.py --runs 1
```

Run with detailed failed field output:

```powershell
python .\api_regression_runner.py --runs 1 --report-failed-fields
```

Run 3 times for consistency summary:

```powershell
python .\api_regression_runner.py --runs 3
```

## Input and Output Layout
- `test payload/`: input PDF files
- `test reference/`: expected JSON files (must match PDF stem name)
  - Example: `test payload/Test.pdf` maps to `test reference/Test.json`
- `response archive/`: per-run API response JSON and run metadata
- `Archived Test reports/`: generated text reports

## Report Notes
- `Reference leaf fields`: total terminal values in reference JSON.
- `Response leaf fields`: total terminal values in response JSON.
- `Matched/Mismatched/Missing/Unexpected`: field-level comparison results.
- `Reference match %`: matched reference fields as a percentage of total reference leaf fields.
- `3-Run Consistency Summary` checks repeatability across 3 runs (same output hash), not correctness vs reference.

## Useful CLI Options
- `--endpoint`: API URL (default is set in script).
- `--timeout-seconds`: request timeout.
- `--payload-dir`, `--reference-dir`: custom input directories.
- `--response-archive-dir`, `--reports-dir`: custom output locations.
- `--report-failed-fields`: include per-field mismatch lines.

