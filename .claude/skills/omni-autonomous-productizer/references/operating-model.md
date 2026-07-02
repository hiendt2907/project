# Operating Model

## Reality Map (bắt buộc mỗi start/resume)

```
CURRENT REALITY MAP

Branch:
HEAD:
Working tree:
Active workloads:
Images/digests:
Safety mode:
Tenant lab:
OrbStack VMs:
Remote Agents:
Redis:
Kafka:
Database:
LLM:
Golden journey last verified point:
First broken link:
Current bottleneck:
Known unrelated changes:
```

Không dựa vào `docs/handoffs/CURRENT_SESSION.md` nếu runtime mâu thuẫn với nó — runtime luôn thắng,
ghi rõ drift vào ledger.

### Datastore/API checklist khi dựng Reality Map

Redis health + namespace · Kafka topics/groups/offset/lag · database migrations/schema · tenant
records · Agent registry · Twin revision · Facts/provenance · unknown/question · command state ·
tenant API · Agent API · onboarding API · competency API · unknown/question API · readiness API ·
mission/command/audit API · portal nếu có.

## Vertical slice format

```
Iteration ID:
Selected bottleneck:
Symptom:
Evidence:
Root-cause hypotheses:
Fastest discriminating check:
Why first:
Product outcome:
Acceptance criteria:
Files expected:
Runtime targets:
Rollback:
Out of scope:
```

Slice phải đi xuyên: contract → code/config → persistence → runtime wiring → API/operator
visibility → tests → build → deploy → runtime proof → documentation → commit. Không mở nhiều epic
song song — đúng MỘT bottleneck mỗi iteration.

## Inspect-before-code rule (trả lời trước khi sửa bất kỳ dòng code nào)

1. Canonical implementation ở đâu?
2. Runtime đang chạy implementation nào?
3. Có legacy implementation active không?
4. Entrypoint thực tế là gì?
5. Image có chứa local HEAD không?
6. State persist ở đâu?
7. Tenant identity truyền qua field nào?
8. Failure bị log/retry hay bị swallow?
9. Operator nhìn kết quả ở đâu?
10. Rollback thế nào?

Luôn so sánh **local HEAD vs built image vs deployed image digest vs code bên trong running pod**.
Không mặc định bug nằm trong source code khi có thể là deployment drift (dùng
`kubectl exec ... -- python -c "import inspect; print(inspect.getsource(...))"` hoặc tương đương để
verify code thật đang chạy trong pod).

## Debug discipline

```
Symptom:
Evidence:
Hypothesis A:
Hypothesis B:
Fastest discriminating check:
Smallest safe change:
Test result:
Deploy result:
Runtime result:
Conclusion:
```

Không sửa nhiều hypothesis cùng lúc. Không tăng timeout để che logic bug. Không disable test hoặc
readiness probe để có màu xanh. Không dùng `except Exception: pass`.

## Runtime validation — full event cycle

Mỗi iteration phải quan sát ít nhất MỘT full event cycle thật, ví dụ (onboarding):

```
Agent discovery tick → evidence forwarded → Kafka offset tăng → consumer nhận
→ Observation → Fact → Twin revision tăng → Competency/Unknown API thay đổi
```

hoặc (closed-loop mutation, ngoài phạm vi khi `OMNI_AUTO_EXECUTE_ENABLED=false`):

```
Incident → evidence → mission → policy → approval → command queued → delivered
→ accepted → running → verification → reconciliation → completed/escalated
→ audit → Twin update
```

Nếu bất kỳ mũi tên nào không có evidence cụ thể → capability đó là PARTIAL, không phải DONE.

## Operator visibility — tối thiểu phải trả lời "operator nhìn thấy gì mới?"

- **Platform**: component health, image/digest, worker role, restart/error, Redis/Kafka/DB/LLM,
  lag/backlog.
- **Tenant**: tenant, Agents, online/offline/stale, discovery state, last seen, onboarding
  progress, Twin revision.
- **Knowledge**: hosts, services, ports, dependency, Facts, provenance, freshness, contradictions,
  competency, unknowns, questions/claims.
- **Operations**: missions, decisions, approvals, commands, execution, verification,
  reconciliation, outcomes, audit.

Raw Redis hoặc Python internal function chỉ là supporting evidence — KHÔNG phải productized
visibility. Nếu chỉ có `redis-cli`/`kubectl exec` để xem, ghi rõ ⚠️ trong PRODUCT_PROOF, không tính
là "operator-visible".

## Remote Agent invariants

Remote Agent phải là sensor + typed executor, KHÔNG phải generic remote shell.

Discovery phải: tenant-scoped, agent-scoped, host-scoped, periodic, observable, có provenance, có
timestamp, có content hash, tuân thủ data residency.

Mutation phải: typed, allowlisted, policy-controlled, reversible nếu có thể, có precondition, có
lease/fencing, có idempotency key, có post-verification, có reconciliation, có audit trail. Không
gọi external mutation là exactly-once.

## LLM invariants

LLM được phép: diễn giải, phân loại, lập hypothesis, đề xuất mission, tạo explanation, tổng hợp
evidence.

LLM KHÔNG được: tự tuyên bố Fact không có evidence, tự nâng Claim thành VERIFIED, tự bỏ qua policy,
tự chọn action nguy hiểm, tự xác nhận UnderstandingComplete, làm nguồn sự thật duy nhất.
Deterministic policy luôn là authority cuối cùng.
