# Iteration Report Template

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

## Execution log

```
INSPECT
  Canonical implementation:
  Runtime implementation:
  Legacy path active?:
  Entrypoint:
  Image contains local HEAD?:
  State persistence location:
  Tenant identity field:
  Failure handling (log/retry/swallow):
  Operator visibility location:
  Rollback plan:

IMPLEMENT
  Files changed:

TEST
  Command:
  Result:
  Proven:
  Not proven:

BUILD
  Source commit:
  Artifact:
  Image tag:

DEPLOY
  Deployment:
  Rollout result:
  Image digest verified:
  Effective config verified:
  Rollback command:

OBSERVE RUNTIME
  Full event cycle observed:
  Evidence:

DEBUG (if needed)
  Symptom:
  Evidence:
  Hypothesis A:
  Hypothesis B:
  Fastest discriminating check:
  Smallest safe change:
  Conclusion:
```

## Definition of Done checklist

(copy từ `references/product-definition-of-done.md`, đánh dấu từng mục)

## Outcome

```
Status: DONE | PARTIAL | BLOCKED
Evidence label: VERIFIED_RUNTIME | VERIFIED_DEPLOYMENT | VERIFIED_TEST | CODE_ONLY | PARTIAL | CONTRADICTED | BLOCKED | ABSENT | UNKNOWN
Operator sees (new):
PRODUCT_PROOF row updated:
Commit:
Next bottleneck candidate:
```
