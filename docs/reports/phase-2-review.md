# Phase 2 Review - Classifier & Matrix Refactor

## Findings
- Over-broad regex rows are the main source of incident-group drift.
- Priority ordering is required to keep specific rows ahead of generic catch-all.

## Design Decisions
- Label predicates are strict and must all match when configured on a row.
- Sorting by `priority` is explicit instead of relying on file order alone.

## Trade-offs
- Requires more disciplined alert labeling upstream to unlock full classifier precision.
