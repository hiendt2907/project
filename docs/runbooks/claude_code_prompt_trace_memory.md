# Prompt: Claude Code — trace verification + repo memory sync

Copy the block below into a new Claude Code session when you need to audit a trace, fix gaps, and persist memory **in the repo** (not only local UI memory).

---

## Prompt (English)

```
You are working in the Omni Kubernetes automation repo (Python, Kafka workers).

## Goal
Verify an incident trace end-to-end, fix any real code bugs with minimal diffs, and persist conclusions in **git-tracked** documentation so the team and future Claude sessions share one source of truth.

## Inputs
- Trace ID: <PASTE_TRACE_ID>
- Optional: lab script or alert payload name if known.

## Steps
1. **Loki:** Query `{namespace="multi-agent", pod_name=~"omni-.*"} |= "<TRACE_ID>"` with `query_range` and a lookback that covers the incident (watch epoch / time window). Report row counts and streams (prober / analyst / core / executor / gateway).
2. **kubectl logs:** Grep the literal trace string across relevant deployments; map stages (ingest → evidence → plan → actions → feedback).
3. **Verdict:** Separate **expected behavior** (policy / SUGGEST-only / invariants) from **bugs**. If code is wrong, fix with tests; cite files.
4. **Repo memory (mandatory):**
   - Add or update `docs/reports/trace-audit-<trace_id>.md` (English, table + verify commands + pointers).
   - Add a short row to `docs/DOCUMENTATION_INDEX.md` (Tầng 1) linking that file.
   - Update `docs/reports/project-memory.md` → `LabVsRealAlertTesting` with counts + verdict + file references.
   - If there is a reusable symptom/fix, add or merge one entry in `docs/vendor/knownbase.md`.
   - Update `.claude/MEMORY.md` index table if a new trace-audit file was added.
5. **Tests:** Run `pytest` on touched modules; at minimum `pytest tests/test_configmap_remediation.py -q` if you touched `evidence_consumer` proof-lane logic.

## Constraints
Follow `.cursorrules`: no secrets; LLM URLs use service names; keep `trace_id` in payloads/logs discussion accurate.

## Deliverable
Short summary: Loki stats → verdict → code/doc files changed → pytest result.
```

---

## One-liner for trace `gw-prom-f58ffe43e85e`

Replace `<PASTE_TRACE_ID>` with `gw-prom-f58ffe43e85e` and note lab: `scripts/lab_nginx_test_missing_configmap_e2e.sh`. Canonical report: [`../reports/trace-audit-gw-prom-f58ffe43e85e.md`](../reports/trace-audit-gw-prom-f58ffe43e85e.md).
