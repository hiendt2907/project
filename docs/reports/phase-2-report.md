# Phase 2 Report - Classifier & Matrix Refactor

## Objective
Move classification to label-first matching with deterministic priority.

## Scope
- `src/workers/diagnostic_mapping.py`
- `config/diagnostic_matrix.yaml`
- `tests/test_diagnostic_mapping.py`

## Contract Changes
- Matrix rows now support priority and label predicates (`alertname`, `domain`, `reason`, `workload`).
- Regex remains fallback only when label predicates are absent.

## What Changed in System Behavior
- `ProbeFailureLab` classification now resolves via labels and avoids broad regex capture.
- Deterministic row ordering reduces generic-row misroutes.

## Tests/E2E
- `tests/test_diagnostic_mapping.py`
- `scripts/validate_classifier_regression_gate.py`

## Known Risks
- Alerts without structured labels still depend on regex fallback quality.

## Memory Applied
- Applied from `docs/reports/project-memory.md`: `FailurePatterns`, `CrossPhaseConstraints`.
