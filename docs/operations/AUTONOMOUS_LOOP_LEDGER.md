# Autonomous Loop Ledger

Append-only log của skill `omni-autonomous-productizer`. Mỗi checkpoint (quota-drain, sleep, resume,
iteration DONE/PARTIAL/BLOCKED) thêm một entry mới ở CUỐI file. Không sửa/xóa entry cũ.

## Format

```
### Checkpoint <UTC ISO8601>
- Timestamp:
- Iteration:
- Quota state:
- HEAD:
- Acceptance:
- Last verified:
- Pending:
- Reset at:
- Resume action:
```

---

### Checkpoint 2026-07-02T07:14:15Z
- Timestamp: 2026-07-02T07:14:15Z
- Iteration: bootstrap (skill creation — no product iteration started yet)
- Quota state: n/a
- HEAD: e8a8c96390616e0a0cd23d9388289966960cdb08
- Acceptance: n/a
- Last verified: skill package files created + smoke test pending
- Pending: run smoke test (status + read-only reality check), then `/omni-autonomous-productizer start`
- Reset at: n/a
- Resume action: read `docs/operations/AUTONOMOUS_LOOP_STATE.json`, run `references/operating-model.md` Reality Map procedure
