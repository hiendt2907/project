"""Phase 5 (tieu chi nghiem thu) - E2E "Senior SRE nhan ban giao he thong moi".

Kich ban 6 buoc theo ``plans/omni-close-autonomous-sre-gaps-2026-07-23.md``:
discover -> hoi nguoi -> nhan tra loi -> verify -> thuc thi (co gate) -> bao cao.

THAT tren VM lab (``cust-app``, qua ``orb -m``), KHONG mock noi bo, theo dung
quy uoc ``remote-agent-test``: business logic that (question_lifecycle,
competency_matrix, aoip.recovery / aoip.capabilities.systemd_kill_unit) chay
voi FakeRedis(decode_responses=True) (state channel — khong phai he thong
duoi test) + transport SSH THAT (subprocess ``orb -m cust-app <argv>``) cho
moi lenh systemctl — khong co FakeSystemd nao o day.

AN TOAN / KHONG PHA HOAI:
- Target la 1 unit THROWAWAY tu tao/tu xoa trong module fixture
  (``e2e-phase5-demo.service``, ``Restart=always``) — KHONG dung service that
  nao khac tren cust-app.
- Buoc 5 (thuc thi) CHI chay toi ``MODE_SHADOW`` (observe_only) — dung chi thi
  an toan cua orchestrator ("neu can mutate that tren VM, CHI chay qua dung
  gate observe_only/shadow co san, KHONG bypass gate de test cho nhanh").
  KHONG tu approve/thuc thi ``MODE_HUMAN_APPROVED`` that len VM trong test nay
  (xem docstring cuoi file — day la gioi han co chu y, escalate len user).
- Buoc "hoi nguoi" dung script gia lap tra loi (KHONG cho nguoi that) — dung
  cho phep ro rang cua plan Phase 5 cho nhu cau CI.

Neu ``orb`` (OrbStack CLI) khong co san tren may chay test -> skip toan bo
file (moi truong khong co VM lab that, khong the coi la "E2E that" neu gia lap).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time

import pytest
from fakeredis.aioredis import FakeRedis

from aoip import audit
from aoip.capabilities.systemd_kill_unit import (
    MODE_SHADOW,
    OUTCOME_APPROVAL_REJECTED,
    OUTCOME_SHADOW_RECOMMENDATION,
    SystemdRestartPolicy,
    build_systemd_kill_unit_executor,
    build_typed_payload,
    issue_capability_command,
)
from aoip.claims_store import load_claims
from aoip.competency_matrix import EntityCompetency, FacetState, FacetValue
from aoip.objects import Finding
from aoip.question_lifecycle import (
    ensure_question_for_unknown,
    render_telegram_text,
    submit_answer,
    sync_unknowns_from_competency,
)
from aoip.recovery import RecoveryGate

pytestmark = [
    pytest.mark.integration,  # can VM lab that (orb) — khong chay trong suite mac dinh
    pytest.mark.skipif(
        shutil.which("orb") is None,
        reason="OrbStack CLI (orb) khong co san — khong the chay E2E that tren VM lab",
    ),
]

TENANT = "e2e-lab-senior-sre"
VM = "cust-app"
UNIT = "e2e-phase5-demo.service"
_ORB_TIMEOUT_DEFAULT = 15.0


class RealSSHTransport:
    """Transport THAT — moi ``run()`` la 1 subprocess ``orb -m cust-app <argv>``
    that su, khong mock/khong fake state. Cung interface (``run(argv, timeout)
    -> (stdout, rc)``) ma moi capability AOIP da mong doi (xem FakeSystemd
    trong test_m1_systemd_recovery_e2e.py / test_capability_systemd_kill_unit.py
    — day la ban THAT cua no, tro vao VM lab that thay vi fake)."""

    target = VM

    def __init__(self, vm: str = VM) -> None:
        self._vm = vm

    async def run(self, argv: list[str], *, timeout: float = _ORB_TIMEOUT_DEFAULT) -> tuple[str, int]:
        proc = await asyncio.create_subprocess_exec(
            "orb", "-m", self._vm, *argv,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return "", 1
        rc = proc.returncode or 0
        if rc != 0 and err:
            return out.decode(errors="replace") + "\n[stderr]" + err.decode(errors="replace"), rc
        return out.decode(errors="replace"), rc


@pytest.fixture(scope="module")
def transport() -> RealSSHTransport:
    return RealSSHTransport()


@pytest.fixture(scope="module", autouse=True)
def demo_unit_lifecycle():
    """Tao 1 systemd unit throwaway that tren cust-app truoc khi chay E2E,
    xoa sach sau khi xong (kha nghich, khong dung service that nao khac)."""
    unit_def = (
        "[Unit]\nDescription=E2E Phase5 demo throwaway unit (safe to kill, self-heals)\n\n"
        "[Service]\nExecStart=/bin/sh -c \"while true; do sleep 5; done\"\n"
        "Restart=always\nRestartSec=1\n\n[Install]\nWantedBy=multi-user.target\n"
    )
    setup = (
        f"printf '%s' '{unit_def}' | sudo tee /etc/systemd/system/{UNIT} >/dev/null && "
        f"sudo systemctl daemon-reload && sudo systemctl enable --now {UNIT}"
    )
    rc = _shell_on_vm(setup)
    assert rc == 0, "khong tao duoc unit throwaway tren VM lab — khong the chay E2E that"
    time.sleep(1)
    yield
    _shell_on_vm(
        f"sudo systemctl disable --now {UNIT}; "
        f"sudo rm -f /etc/systemd/system/{UNIT}; sudo systemctl daemon-reload"
    )


def _shell_on_vm(script: str) -> int:
    import subprocess
    return subprocess.run(["orb", "-m", VM, "bash", "-c", script],
                          capture_output=True, timeout=30).returncode


def _redis() -> FakeRedis:
    return FakeRedis(decode_responses=True)


def _gate() -> RecoveryGate:
    return RecoveryGate(allowed_failure_modes=frozenset({"resource_runaway"}),
                        allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                        scope_prefix="svc:", min_diagnosis_confidence=0.0, max_diagnosis_age_s=1e9,
                        allowed_targets=frozenset({UNIT}))


def _policy() -> SystemdRestartPolicy:
    return SystemdRestartPolicy(allowed_units=frozenset({UNIT}))


# ── Buoc 1: Discover (that, tren VM lab) ──────────────────────────────────

class TestStep1Discover:
    async def test_discover_finds_the_never_before_seen_unit(self, transport):
        """Agent chay discovery read-only THAT tren cust-app (coi nhu VM moi —
        unit vua tao chua tung xuat hien trong bat ky KB/system-model nao)."""
        out, rc = await transport.run(
            ["systemctl", "list-units", "--type=service", "--no-legend", "--no-pager", "--plain"])
        assert rc == 0
        assert UNIT in out, "discovery khong thay unit throwaway tren VM that — moi truong sai"

        exists_out, rc2 = await transport.run(
            ["systemctl", "show", "-p", "LoadState", "--value", UNIT])
        assert rc2 == 0 and exists_out.strip() == "loaded"


# ── Buoc 2+3: Hoi nguoi -> nhan tra loi (that, qua question_lifecycle) ────

class TestStep2And3AskAndAnswer:
    async def test_unknown_gap_opens_question_and_simulated_answer_becomes_claim(self):
        redis = _redis()
        # "Discover" phat hien 1 facet chua biet cho unit moi: runbook/restart
        # contract cua no (co dung Restart=always khong? khong co tai lieu nao
        # noi ca) -> competency projection = UNKNOWN cho facet 'runbook'.
        competency = EntityCompetency(
            entity_type="service", entity_id=f"svc:{UNIT}",
            facets={"runbook": FacetValue(state=FacetState.UNKNOWN)},
        )
        touched = await sync_unknowns_from_competency(redis, TENANT, competency)
        assert len(touched) == 1
        unknown = touched[0]
        assert unknown["facet"] == "runbook" and unknown["reason"] == "missing"

        question = await ensure_question_for_unknown(redis, TENANT, unknown)
        assert question is not None
        assert question["status"] == "PENDING"
        telegram_text = render_telegram_text(question)
        assert telegram_text  # day la text THAT se gui qua Telegram

        # Script gia lap tra loi Telegram cho CI (KHONG cho nguoi that — cho
        # phep ro rang cua plan Phase 5).
        simulated_answer_value = "Restart=always (systemd tu heal trong ~1s sau SIGTERM)"
        answer = await submit_answer(
            redis, TENANT, question["question_id"],
            answered_by="simulated-ci-oncall", value=simulated_answer_value,
            source_channel="telegram", confidence=0.9,
        )
        assert answer is not None
        assert answer["value"] == simulated_answer_value

        claims = await load_claims(redis, TENANT)
        assert any(c.predicate == "has_runbook" and c.value == simulated_answer_value for c in claims), (
            "Answer khong duoc chieu thanh Claim dung predicate — vo hop dong Answer->Claim")

        # State duoc giu lai cho buoc Verify (Redis rieng cua test nay — khong
        # chia se giua test method, nen truyen qua file-level cache).
        _SHARED["redis"] = redis
        _SHARED["question_id"] = question["question_id"]
        _SHARED["claimed_restart_policy"] = simulated_answer_value


_SHARED: dict = {}


# ── Buoc 4: Verify (probe THAT tren VM — khong tin loi khai) ──────────────

class TestStep4Verify:
    async def test_claim_is_independently_verified_against_real_vm_state(self, transport):
        assert "claimed_restart_policy" in _SHARED, "Buoc 2+3 phai chay truoc (thu tu file pytest)"
        claimed = _SHARED["claimed_restart_policy"]

        # KHONG tin loi khai — probe THAT tren VM lab de kiem chung claim.
        out, rc = await transport.run(["systemctl", "show", "-p", "Restart", "--value", UNIT])
        assert rc == 0
        real_restart_policy = out.strip()

        verified = real_restart_policy == "always"
        _SHARED["verify_passed"] = verified
        _SHARED["verify_real_value"] = real_restart_policy
        assert verified, (
            f"Claim 'Restart=always' KHONG duoc probe that xac nhan "
            f"(that su={real_restart_policy!r}) — day la loi that can bao cao, "
            f"khong duoc gia vo pass")


# ── Buoc 5: Thuc thi qua gate (that tren VM, CHI toi observe_only/shadow) ──

class TestStep5ExecuteThroughGate:
    async def test_unapproved_command_is_rejected_fail_closed_even_before_shadow(
        self, transport, tmp_path,
    ):
        """Fail-closed: neu khong co approval that, capability tu choi truoc
        khi cham toi buoc shadow/execute nao — chung minh gate khong the bi
        bo qua bang cach thieu approval."""
        typed = build_typed_payload(mission_id="mis-e2e", decision_id="dec-e2e",
                                    incident_id="inc-e2e-handoff",
                                    summary="E2E Phase5 demo unit flagged runaway (simulated)",
                                    unit=UNIT)
        now = time.time()
        cmd = issue_capability_command(typed_payload=typed, approver="nobody", tenant=TENANT,
                                      issued_at=now, expires_at=now + 300,
                                      findings=(), diagnosis_confidence=None)
        cmd["approval"]["approved"] = False  # chua ai approve that

        audit_log = audit.FileAuditLog(tmp_path / "e2e_phase5_rejected_audit.jsonl")
        executor = await build_systemd_kill_unit_executor(
            redis=_redis(), holder="e2e-phase5-agent", transport=transport,
            audit_log=audit_log, gate=_gate(), policy=_policy(),
            tenant=TENANT, mode=MODE_SHADOW,
        )
        status, result = await executor(cmd)

        assert status == "FAILED"
        assert result["product_outcome"] == OUTCOME_APPROVAL_REJECTED

    async def test_shadow_mode_runs_real_preflight_against_real_vm_and_recommends_only(
        self, transport, tmp_path,
    ):
        """Buoc 5 CHI di toi day (MODE_SHADOW/observe_only) tren VM lab that —
        theo dung gioi han an toan cua orchestrator. KHONG chay MODE_HUMAN_APPROVED
        thuc su mutate VM trong test tu dong nay (xem docstring cuoi file)."""
        typed = build_typed_payload(mission_id="mis-e2e-handoff", decision_id="dec-e2e-shadow",
                                    incident_id="inc-e2e-handoff",
                                    summary="E2E Phase5 demo unit flagged runaway (simulated evidence)",
                                    unit=UNIT)
        finding = Finding(claim=f"svc:{UNIT} resource_runaway (simulated for E2E demo)",
                          references=("e2e-demo-probe-1",), verdict=True, confidence=0.9)
        now = time.time()
        cmd = issue_capability_command(
            typed_payload=typed, approver="simulated-ci-approver", tenant=TENANT,
            issued_at=now, expires_at=now + 300, findings=(finding,), diagnosis_confidence=0.9,
        )
        # approved=True o day dai dien cho "quyet dinh HITL da duoc ghi nhan"
        # (durable command envelope) — KHONG phai self-approve mutation that:
        # mode=MODE_SHADOW ben duoi dam bao AGENT khong bao gio mutate that du
        # payload noi approved, dung chinh gioi han an toan da chi dinh.

        redis = _redis()
        audit_log = audit.FileAuditLog(tmp_path / "e2e_phase5_audit.jsonl")
        executor = await build_systemd_kill_unit_executor(
            redis=redis, holder="e2e-phase5-agent", transport=transport,
            audit_log=audit_log, gate=_gate(), policy=_policy(),
            tenant=TENANT, mode=MODE_SHADOW,
        )
        status, result = await executor(cmd)

        assert status == "COMPLETED"
        assert result["product_outcome"] == OUTCOME_SHADOW_RECOMMENDATION
        checks = {c["check"]: c["ok"] for c in result["evidence"]["capability_checks"]}
        assert checks.get("unit_allowlisted") is True
        assert checks.get("unit_exists") is True, (
            "unit_exists phai la 1 real SSH round-trip toi VM lab — neu False, "
            "gia dinh VM lab that da sai")
        assert "would_execute" in result["evidence"]
        assert UNIT in result["evidence"]["would_execute"]

        _SHARED["shadow_result"] = result


# ── Buoc 6: Bao cao (tong hop toan luong, co bang chung tung buoc) ────────

class TestStep6Report:
    async def test_final_report_summarizes_full_handoff_flow_with_evidence(self):
        assert _SHARED.get("verify_passed") is True
        assert _SHARED.get("shadow_result") is not None

        report = {
            "tenant": TENANT, "vm": VM, "unit": UNIT,
            "step1_discover": "PASS — unit that thay tren VM lab qua systemctl that",
            "step2_ask": {"question_id": _SHARED["question_id"], "channel": "telegram (simulated answer)"},
            "step3_answer_to_claim": "PASS — Answer chieu dung predicate has_runbook qua submit_answer",
            "step4_verify": {
                "status": "PASS" if _SHARED["verify_passed"] else "FAIL",
                "claimed": _SHARED["claimed_restart_policy"],
                "probed_real_value": _SHARED["verify_real_value"],
            },
            "step5_execute": {
                "mode": "shadow (observe_only) — gioi han an toan, KHONG chay human_approved that",
                "product_outcome": _SHARED["shadow_result"]["product_outcome"],
                "gap": (
                    "MODE_HUMAN_APPROVED that (SIGTERM that len VM) KHONG duoc chay trong "
                    "E2E tu dong nay — day la gioi han AN TOAN co chu y (orchestrator: chi "
                    "duoc di qua observe_only/shadow, khong bypass gate de test nhanh), "
                    "KHONG phai gap cua Phase 1-4. Can user quyet dinh neu muon chay tiep "
                    "buoc mutate that (tren unit throwaway, phuc hoi duoc qua Restart=always)."
                ),
            },
            "step6_report": "generated (file nay)",
        }
        assert report["step4_verify"]["status"] == "PASS"
        # Bao cao duoc "phat" (in ra) de nguoi van hanh xem trong log test —
        # khong giau gap nao (feedback_chaos_test_protocol).
        print(json.dumps(report, indent=2, ensure_ascii=False))
