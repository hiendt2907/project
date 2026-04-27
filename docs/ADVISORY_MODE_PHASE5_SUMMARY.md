# Omni Smart-SIEM Phase 5: Advisory Mode (Level 2 Autonomy)

## Executive Summary

**Paradigm Shift:** Omni transitions from **Level 1 (Autonomous Execution)** to **Level 2 (Advisory Mode)**.

The system becomes a **24/7 Super-Analyst**, not an executor:
- ✅ Diagnoses incidents with root-cause analysis
- ✅ Predicts system degradation over time (1h, 3h, 6h, 12h, 24h)
- ✅ Suggests read-only verification steps (CLI commands)
- ✅ Proposes safe remediation actions (awaiting human approval)
- ❌ NEVER autonomously executes mutations

---

## Architecture Changes

### Before (Level 1)
```
Alert → Analyst (LLM) → ReAct Planner → Executor (autonomous) → Mutation executed
                                     ↑
                              [Mutation decision]
```

**Risk:** LLM hallucination or prompt injection could trigger unintended mutations (delete pods, data loss, etc.)

### After (Level 2 - Advisory Mode)
```
Alert → Temporal Prober → Advisory Analyst → Kill-Switch (blocks mutations) → Telegram
              ↓                    ↓                           ↓
          [1-hour rate data]  [Forecast timeline]      [Traps if attempted]
                                                              ↓
                                                      [Suggested Action]
```

**Safety:** Multi-layered:
1. LLM never recommends mutations (system prompt enforces advisory-only)
2. Kill-switch blocks any execution attempt
3. Trap+notify if LLM hallucinates (routes to Telegram as "Suggested Action")

---

## Key Features

### 1. Structured Incident Reports
**Output Schema: AnalystAdvisory**
```json
{
  "trace_id": "abc123",
  "verdict": "CRITICAL",
  "root_cause": "Pod OOMKilled due to memory leak in app container",
  "confidence": "high",
  "affected_workload": "production/api-server",
  "verification_steps": [
    {
      "order": 1,
      "command": "kubectl describe pod <pod> -n production",
      "expected_output": "OOMKilled status",
      "rationale": "Confirm memory-kill signal"
    }
  ],
  "proposed_remediation": [
    {
      "order": 1,
      "action": "Increase memory limit in Deployment",
      "args": {"namespace": "production", "deployment": "api-server", "memory_limit": "2Gi"},
      "approval_required": true,
      "rollback_plan": "kubectl set resources deployment api-server --limits memory=1Gi"
    }
  ],
  "forecast": {
    "method": "linear_extrapolation",
    "forecasts": [
      {"timeframe": "1h", "severity": "critical", "prediction": "Pod remains OOMKilled; requests queue"},
      {"timeframe": "6h", "severity": "catastrophic", "prediction": "All replicas fail; API unavailable"}
    ]
  }
}
```

### 2. Time-Series Impact Forecasting
**For Infrastructure Incidents:**
- Uses Prometheus `rate-of-change` data (1-hour historical window)
- Linear extrapolation: if CPU rising at +2%/min, predicts saturation at T+30min
- Math-based, not speculative

**For Security Incidents:**
- MITRE ATT&CK Kill Chain model
- Phases: Recon (1h) → Lateral Movement (3h) → Escalation (6h) → Persistence (12h) → Exfiltration (24h)
- Adjusts phases based on evidence (rapid tooling → accelerate timeline)

### 3. Temporal Evidence Pipeline
**Prober Enhancement:**
- Fetches not just current state, but 1-hour historical metric window
- Captures rate-of-change (change per minute)
- Includes in evidence block: `[TEMPORAL_EVIDENCE metric=cpu_percent rate_per_min=+2.1 samples=60]`

**LLM Uses Data:**
- Reads rate directly from evidence block
- No guessing; extrapolates only when data present
- Outputs: "Forecast degraded: missing rate data. Relying on heuristics." (explicit)

### 4. Kill-Switch Enforcement
**Hardcoded Defaults:**
```python
OMNI_AUTO_EXECUTE_ENABLED = False  # Always False
OMNI_SIEM_SUGGEST_ONLY = True       # Advisory only
```

**Multi-Layer Blocking:**
1. System prompt: "You MUST NEVER recommend mutations"
2. Advisory schema: no `execute_action` field; only `proposed_remediation` (advisory-only)
3. Kill-switch module: intercepts any mutation attempt
4. Trap+notify: if LLM hallucination, routes to Telegram as "Suggested Action"

**Red Team Results:**
- ✅ Prompt injection blocked (evidence sanitized)
- ✅ Hallucination detected (rate data validation enforces honesty)
- ✅ Embedded mutations blocked (forbidden keywords scan)
- ✅ Temporal data race handled (timestamp validation)

### 5. Telegram Rendering
**Aggressive Markdown with Emojis:**
```
✅ Verdict: CRITICAL
🔍 Root Cause: Pod OOMKilled due to memory leak; 850 MB usage vs 512 MB request
🎯 Confidence: high

🔎 Verification Steps (read-only):
Step 1: Confirm OOMKilled signal
kubectl describe pod <pod> -n <ns>
Expected: OOMKilled status

⚙️ Proposed Remediation (advisory):
Step 1: Increase memory limit in Deployment
Approval Required (🔒)

📈 Impact Forecast (linear_extrapolation):
✅ 1h [high]: Pod remains OOMKilled; requests queue
⚠️ 3h [high]: Cascading failures; load balancer redirects
🔴 6h [critical]: 90% of traffic fails over
💥 12h [critical]: Full service degradation
```

---

## Benefits

| Aspect | Before (Level 1) | After (Level 2 - Advisory) |
|--------|-----------------|---------------------------|
| **Execution Speed** | 30s alert → 1s mutation | 30s alert → 5s advisory (human decides) |
| **Safety** | High risk of unintended mutations | Mutations require human approval |
| **Transparency** | "Executing X"; no reasoning | Full reasoning + verification steps + rollback plan |
| **Operator Control** | Reactive (react to executed mutation) | Proactive (review before execution) |
| **Forensics** | "What was executed?" | "What was proposed? Why? How to verify? How to rollback?" |
| **Compliance** | Mutations logged but auto-executed | All suggested actions logged; human audit trail |

---

## Deployment Timeline

| Phase | Duration | Activity |
|-------|----------|----------|
| **Phase 5a** | Week 1 | Code review + security audit (red team) |
| **Phase 5b** | Week 2 | Deploy to lab; e2e test with sample incidents |
| **Phase 5c** | Week 3 | Deploy to staging; monitor forecasting accuracy |
| **Phase 5d** | Week 4 | Gradual prod rollout (10% → 50% → 100% of workers) |
| **Phase 5e** | Ongoing | Monitor + fine-tune forecast timelines (ML tuning) |

---

## Operational Handoff

**For SRE/Oncall Teams:**
1. Monitor Telegram channel for AnalystAdvisory messages
2. For each incident:
   - Review root_cause + verification_steps
   - Run read-only commands to confirm
   - Decide if proposed_remediation is safe
   - Execute manually if approved (copy command from advisory)
3. Log execution decision (approve, modify, skip, defer)

**For Security Team:**
- SIEM alerts route through Kill Chain forecasting
- Escalation_reason field flags incidents needing HITL approval (security, unknown cause)
- Full reasoning trail for audits

**For Architects:**
- Monitor `forecast_accuracy` metrics (1h, 3h, 6h, 12h predictions vs actual)
- Tune rate-of-change thresholds over time
- Refine Kill Chain phase timings for your incident patterns

---

## Risk Mitigation

### **Risk: Analyst becomes "noise" (too many advisories)**
**Mitigation:**
- Tunable verdict thresholds (only send URGENT + CRITICAL, not INVESTIGATE)
- Batch advisories in quiet periods
- A/B test 2 forecasting modes, measure engagement

### **Risk: Forecasts are wrong**
**Mitigation:**
- Always explicitly state basis ("missing rate data" vs "linear extrapolation")
- Human must verify with read-only steps before trusting forecast
- Async learning loop: log actual vs predicted; improve models

### **Risk: Operators ignore advisories (alert fatigue)**
**Mitigation:**
- Start with high-confidence incidents only (confidence=high)
- Include clear verification steps (immediate action vs passive review)
- Track metrics: advisory→action rate; measure if trend improves over time

### **Risk: Kill-switch doesn't work (mutation somehow executes)**
**Mitigation:**
- Pre-deploy validation: assert OMNI_AUTO_EXECUTE_ENABLED == False
- Pre-execution hook: reject any mutation if kill-switch active
- Post-execution hook: trap attempted mutations, store in Redis alert queue
- Alert if kill_switch_blocked events appear in logs

---

## Success Metrics (Phase 5 + Beyond)

**Technical:**
- 99%+ of advisories parse correctly (schema validation)
- 0% unintended mutations (kill-switch effectiveness)
- Forecast accuracy: 1h predictions ≥ 85%, 6h ≥ 70%, 12h ≥ 60%

**Operational:**
- Advisory-to-action latency: < 2min (human reviews + executes)
- Operator approval rate: baseline (Week 1) vs trend (Week 4+)
- MTTR improvement: incidents with advisory vs without (historical comparison)

**Safety:**
- 0 kill-switch bypasses (grep logs)
- 100% audit trail logged (who approved what action, when)
- 0 unauthorized mutations (compare executed vs proposed)

---

## Next Phases (Beyond Phase 5)

**Phase 6: Selective Auto-Execute**
- Enable auto-execute for low-risk operations (e.g., rollout restart)
- Require human approval for sensitive operations (secrets, RBAC)
- Grade incidents by risk; auto-execute only on low-risk subset

**Phase 7: Continuous Learning**
- Track forecast accuracy; refine rate-of-change models
- Build incident similarity index (this alert looks like incident X from 3 weeks ago)
- Personalize Kill Chain phases per workload type

**Phase 8: Full Autonomy (Conditional)**
- Re-enable auto-execute after 6 months of zero unintended mutations
- Require incident pre-approval playbooks
- Maintain human-in-loop for critical / data-sensitive operations

---

## Questions & Answers

**Q: Will the system be slower (requiring human approval)?**
A: Slightly slower to execute (human review adds 1-5 min), but faster to diagnose (super-analyst runs continuously). Net effect: better MTTR overall because false positives drop (advisor filters noise).

**Q: What if a forecast is wrong?**
A: Forecasts are explicitly labeled with confidence + basis. Operators must always verify with read-only steps before trusting. Wrong forecast is OK as long as it's honest about uncertainty.

**Q: Can an operator accidentally approve a bad remediation?**
A: Proposed_remediation includes rollback_plan for each step. Operator can execute, monitor, and rollback if needed. Rollback is always included (not optional).

**Q: Is this "set and forget"?**
A: No. Requires tuning:
- Forecast thresholds (what triggers URGENT vs CRITICAL)
- Kill Chain phase timings (adjusted per incident patterns)
- Temporal window size (currently 1h; may need 24h for slow leaks)

---

## Stakeholder Sign-Off

- [ ] **CEO/Product:** Confirms Advisory Mode strategy aligns with 2026 roadmap
- [ ] **VP Security:** Confirms Kill-Switch design; sign off on red team findings
- [ ] **Head of SRE:** Confirms operational model (manual execution) is sustainable
- [ ] **Engineering Lead:** Confirms deployment timeline is achievable

---

**Phase 5 Complete:** Omni Smart-SIEM is now a 24/7 Super-Analyst, providing structured predictive insights without autonomous risk.
