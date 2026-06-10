#!/usr/bin/env python3
"""Seed 4 K8s PlaybookSpec vào PlaybookStore (Redis).

Usage:
  PYTHONPATH=src .venv/bin/python scripts/seed_k8s_playbooks.py --redis-url redis://localhost:16379/0
"""

from __future__ import annotations

import argparse
import asyncio

import redis.asyncio as aredis

from services.playbook.store import PlaybookStore
from workers.schemas.playbook import (
    GRAD_CANDIDATE,
    PlaybookSpec,
    PlaybookStepSpec,
    ProofOfFault,
    TriggerMatch,
    VerifySpec,
)

SEEDS: list[PlaybookSpec] = [
    PlaybookSpec(
        playbook_id="PB-K8S-OOM-RESTART",
        name="OOMKilled workload — rollout restart",
        domain="k8s",
        trigger=TriggerMatch(
            lanes=["SYS_HARD_FAIL", "SYS_RESOURCE"],
            fault_keywords=["oomkilled", "oom"],
        ),
        proof_of_fault=ProofOfFault(fault_keywords=["oomkilled", "oom"]),
        steps=[PlaybookStepSpec(
            step_order=1, backend="k8s", action="k8s_rollout_restart",
            params_template={"namespace": "{namespace}", "deployment": "{deployment}"},
            verify=VerifySpec(settle_sec=45, attempts=3),
            rollback_type="snapshot",
        )],
        initial_graduation=GRAD_CANDIDATE,
        approved_by="seed",
        notes="OOM lặp lại ≥2 lần — restart đưa pod về trạng thái sạch; fix gốc vẫn cần tăng limit (advisory).",
    ),
    PlaybookSpec(
        playbook_id="PB-K8S-CRASHLOOP-RESTART",
        name="CrashLoopBackOff — rollout restart sau proof",
        domain="k8s",
        trigger=TriggerMatch(
            lanes=["SYS_HARD_FAIL"],
            fault_keywords=["crashloop", "crashloopbackoff", "backoff"],
        ),
        proof_of_fault=ProofOfFault(fault_keywords=["crashloop", "backoff"]),
        steps=[PlaybookStepSpec(
            step_order=1, backend="k8s", action="k8s_rollout_restart",
            params_template={"namespace": "{namespace}", "deployment": "{deployment}"},
            verify=VerifySpec(settle_sec=60, attempts=4),
            rollback_type="snapshot",
        )],
        initial_graduation=GRAD_CANDIDATE,
        approved_by="seed",
        notes="INV_NO_RESTART_ON_BROKEN_SPEC: proof-of-fault reconcile chặn restart khi spec hỏng (ConfigMap thiếu) — broken-spec lane đi đường advisory.",
    ),
    PlaybookSpec(
        playbook_id="PB-K8S-CPU-RESTART",
        name="CPU saturation workload — rollout restart",
        domain="k8s",
        trigger=TriggerMatch(
            lanes=["SYS_RESOURCE"],
            fault_keywords=["cpu", "highcpu", "cpu utilization", "millicore"],
        ),
        proof_of_fault=ProofOfFault(fault_keywords=["cpu"]),
        steps=[PlaybookStepSpec(
            step_order=1, backend="k8s", action="k8s_rollout_restart",
            params_template={"namespace": "{namespace}", "deployment": "{deployment}"},
            verify=VerifySpec(settle_sec=60, attempts=3),
            rollback_type="snapshot",
        )],
        initial_graduation=GRAD_CANDIDATE,
        approved_by="seed",
        notes="Khớp hành vi autonomous rollout hiện hành; scale-variant (cần relative replicas) để Phase sau.",
    ),
    PlaybookSpec(
        playbook_id="PB-K8S-STUCK-POD-EVICT",
        name="Pod kẹt Terminating/NotReady — delete pod (HITL)",
        domain="k8s",
        trigger=TriggerMatch(
            lanes=["SYS_HARD_FAIL"],
            fault_keywords=["terminating", "stuck", "notready"],
        ),
        proof_of_fault=ProofOfFault(fault_keywords=["terminating", "stuck"]),
        steps=[PlaybookStepSpec(
            step_order=1, backend="k8s", action="k8s_delete_pod",
            params_template={"namespace": "{namespace}", "pod": "{pod}"},
            verify=VerifySpec(settle_sec=45, attempts=3),
            rollback_type="none",  # delete không rollback được
            requires_hitl=True,
        )],
        initial_graduation=GRAD_CANDIDATE,
        approved_by="seed",
        notes="rollback_type=none → HITL bắt buộc vĩnh viễn ở step level.",
    ),
]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--redis-url", default="redis://localhost:16379/0")
    args = ap.parse_args()
    r = aredis.from_url(args.redis_url)
    store = PlaybookStore(r)
    for spec in SEEDS:
        await store.upsert_spec(spec)
        print(f"seeded {spec.playbook_id} v{spec.version} ({spec.name})")
    ids = await r.smembers("pbspec:index")
    print(f"pbspec:index = {sorted(i.decode() for i in ids)}")
    await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
