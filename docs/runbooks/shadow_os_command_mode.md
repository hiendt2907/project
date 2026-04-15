# Shadow OS Command Mode Runbook

## Purpose

Operate Omni in suggestion-only mode where remediation is delivered as `SUGGEST_OS_RUNBOOK` and executed by operator-controlled commands.

## Core invariants

- `OMNI_SHADOW_OS_MODE=true` blocks SDK mutate execution.
- Every step must include:
  - `dry_run_command`
  - `command`
  - `rollback_command`
  - `evidence_refs`
- Execute dry-run first; only proceed when dry-run returns success.

## Execution workflow

1. Receive Telegram runbook with `trace_id` + ordered steps.
2. Run step using CLI:
   - `python scripts/omni_shadow_exec_feedback.py --trace-id <trace> --step-id <step> --dry-run-command "<...>" --command "<...>"`
3. CLI captures stdout/stderr/exit code and publishes Kafka `omni-action-feedback`.
4. Omni re-evaluates from feedback and emits next guidance or success transition.

## Safety controls

- No destructive commands without rollback and escalation marker.
- Restrict host-level execution to allowlisted environments and dedicated privileged executor.
- `nsenter` host context wrapper:
  - `nsenter -t 1 -m -u -i -n -p -- <linux_command>`

## Mock testing guidance (M4)

- For DiskPressure/OOMKilled simulation, run only in cgroup/container-limited scope or loopback-mounted filesystem.
- Never run stress commands directly against host root filesystem.
- Tag all mock feedback with `mock_case_id` to prevent production memory pollution.
