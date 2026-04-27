# Incident Post-Mortem — disabled-recall-001

**Date:** 2026-04-23T03:12:21Z
**Outcome:** VERIFIED_SUCCESS

## Summary

- **Alert:** `NginxTestContainerWaitingFaultLab`
- **Namespace:** ``
- **Workload:** ``
- **Remediation tool:** `k8s_rollout_restart`
- **Arg keys used:** `deployment`, `namespace`

## Notes

Arg values are intentionally omitted from this record to prevent credential leakage.
Retrieve current Secret/ConfigMap values from the cluster at remediation time.
