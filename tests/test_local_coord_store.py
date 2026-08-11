"""Kho điều phối CỤC BỘ trên host — thay Redis cho lease/ledger phía agent (Đ52).

Vì sao đổi, đo được trên UAT 2026-08-11: lệnh tự khắc phục `systemd.restart_unit` được
dispatch tới agent thành công (`state=QUEUED http=200`, agent `accept→progress→terminal`)
nhưng thất bại với

    outcome = {"rc": 1, "reason": "executor_exception: Timeout connecting to server"}

Nguyên nhân: `run.env` trên cả 3 VM khách trỏ
``AOIP_REDIS_URL=redis://redis.multi-agent.svc.cluster.local:6379/0`` — tên DNS chỉ phân
giải được BÊN TRONG k3s. Từ VM khách `getent hosts` không ra, kết nối treo tới timeout.
Redis service là ClusterIP, cố ý không có đường ra ngoài.

Vì sao kho CỤC BỘ mới là đúng (chứ không phải mở Redis ra ngoài hay xây API điều phối):
`ExecutionLease`/`IdempotencyLedger` chỉ được dùng trong `aoip/agent/*` — không call site
nào phía Omni — và lease scope là ``{tenant}:{unit-systemd}``, tức writer luôn là agent
TRÊN CHÍNH HOST đó. Không có nhu cầu điều phối liên máy. Bắt VM khách nối vào Redis nội bộ
của Omni còn vi phạm ranh giới NÃO/THÂN (CLAUDE.md): agent là chân tay trên hạ tầng khách,
không phải một thành phần của cluster Omni.

Bộ test này khoá đúng ngữ nghĩa Redis mà `lease.py`/`idempotency.py` dựa vào — hai file đó
KHÔNG đổi một dòng nào, nên mọi bảo đảm an toàn của chúng giữ nguyên.
"""
from __future__ import annotations

import asyncio

import pytest

from aoip.agent.idempotency import IdempotencyLedger
from aoip.agent.lease import _RELEASE_SCRIPT, _RENEW_SCRIPT, ExecutionLease
from aoip.agent.local_coord import LocalCoordStore


@pytest.fixture
def store(tmp_path):
    return LocalCoordStore(path=str(tmp_path / "coord.json"))


# ── Ngữ nghĩa SET/GET/DEL mà lease + ledger dựa vào ──────────────────────────

async def test_set_get_don_gian(store):
    assert await store.set("k", "v") is True
    assert await store.get("k") == "v"


async def test_get_key_khong_ton_tai_tra_None(store):
    assert await store.get("chua-co") is None


async def test_set_nx_chi_thanh_cong_lan_dau(store):
    """`SET NX` là nền của lease: người thứ hai PHẢI thất bại, không được ghi đè."""
    assert await store.set("k", "first", nx=True) is True
    assert await store.set("k", "second", nx=True) is None
    assert await store.get("k") == "first", "NX that bai van ghi de => lease vo nghia"


async def test_set_khong_nx_thi_ghi_de(store):
    await store.set("k", "a")
    await store.set("k", "b")
    assert await store.get("k") == "b"


async def test_delete(store):
    await store.set("k", "v")
    assert await store.delete("k") == 1
    assert await store.get("k") is None
    assert await store.delete("k") == 0


# ── TTL: holder crash thì lease phải tự giải phóng ───────────────────────────

async def test_ttl_het_han_thi_key_bien_mat(store):
    """Không có TTL thì một agent chết sẽ khoá scope VĨNH VIỄN."""
    await store.set("k", "v", ex=1)
    assert await store.get("k") == "v"
    await asyncio.sleep(1.05)
    assert await store.get("k") is None


async def test_key_het_han_thi_nx_lai_thanh_cong(store):
    """Sau khi hết hạn, agent khác PHẢI acquire được — nếu không, scope kẹt."""
    await store.set("k", "old", nx=True, ex=1)
    await asyncio.sleep(1.05)
    assert await store.set("k", "new", nx=True) is True


# ── CAS: compare-and-expire / compare-and-delete phải NGUYÊN TỬ ──────────────

async def test_renew_chi_khi_token_khop(store):
    await store.set("lease:x", "tok-A", ex=60)
    assert await store.eval(_RENEW_SCRIPT, 1, "lease:x", "tok-A", 60) == 1
    assert await store.eval(_RENEW_SCRIPT, 1, "lease:x", "tok-KHAC", 60) == 0
    assert await store.get("lease:x") == "tok-A", "renew sai token khong duoc doi gia tri"


async def test_release_chi_khi_token_khop(store):
    """Đây là race mà Lua script gốc sinh ra để chặn: KHÔNG được xoá lease của người khác."""
    await store.set("lease:x", "tok-A", ex=60)
    assert await store.eval(_RELEASE_SCRIPT, 1, "lease:x", "tok-KHAC") == 0
    assert await store.get("lease:x") == "tok-A"
    assert await store.eval(_RELEASE_SCRIPT, 1, "lease:x", "tok-A") == 1
    assert await store.get("lease:x") is None


async def test_eval_script_la_khong_duoc_am_tham_thanh_cong(store):
    """Script không nhận ra ⇒ nổ, KHÔNG trả 0 giả vờ ổn.

    Nếu `lease.py` đổi script mà store không biết, im lặng trả 0 sẽ khiến renew luôn
    thất bại → mọi recovery escalate mà không ai hiểu vì sao.
    """
    with pytest.raises(NotImplementedError):
        await store.eval("return 1", 1, "k", "v")


# ── Dùng THẬT qua chính ExecutionLease / IdempotencyLedger (không mock) ──────

async def test_execution_lease_chay_that_tren_store(store):
    lease = ExecutionLease(store)
    tok = await lease.acquire("loyalty-uat:payment-api", holder="agent-1", ttl_s=60)
    assert tok is not None

    # Agent khác KHÔNG được vào cùng scope
    assert await lease.acquire("loyalty-uat:payment-api", holder="agent-2", ttl_s=60) is None

    assert await lease.renew("loyalty-uat:payment-api", token=tok, ttl_s=60) is True
    assert await lease.release("loyalty-uat:payment-api", token=tok) is True
    # giải phóng rồi thì agent khác vào được
    assert await lease.acquire("loyalty-uat:payment-api", holder="agent-2", ttl_s=60) is not None


async def test_lease_tenant_khac_nhau_khong_dung_do(store):
    """`canonical_scope` nhúng tenant — hai tenant cùng tên unit không được chặn nhau."""
    lease = ExecutionLease(store)
    assert await lease.acquire("tenant-A:payment-api", holder="a", ttl_s=60) is not None
    assert await lease.acquire("tenant-B:payment-api", holder="b", ttl_s=60) is not None


async def test_idempotency_ledger_chay_that_tren_store(store):
    ledger = IdempotencyLedger(store)
    assert await ledger.get("cmd-1") is None
    assert await ledger.claim("cmd-1", holder="agent-1") is True
    assert await ledger.claim("cmd-1", holder="agent-2") is False, "claim 2 lan = chay 2 lan"

    await ledger.record("cmd-1", status="recovered", outcome={"rc": 0})
    rec = await ledger.get("cmd-1")
    assert rec["status"] == "recovered"


# ── Bền vững: agent restart không được mất trạng thái ────────────────────────

async def test_du_lieu_ton_tai_qua_tien_trinh_moi(tmp_path):
    """Ledger nằm trong bộ nhớ ⇒ agent restart giữa mutation là mất dấu → chạy lại mù."""
    p = str(tmp_path / "coord.json")
    await LocalCoordStore(path=p).set("k", "v", ex=300)
    assert await LocalCoordStore(path=p).get("k") == "v"


async def test_file_hong_khong_lam_chet_agent(tmp_path):
    """File rác (đĩa đầy, kill giữa lúc ghi) ⇒ coi như rỗng, KHÔNG nổ ngược vào agent."""
    p = tmp_path / "coord.json"
    p.write_text("{khong-phai-json")
    store = LocalCoordStore(path=str(p))
    assert await store.get("bat-ky") is None
    assert await store.set("k", "v") is True
    assert await store.get("k") == "v"


async def test_hai_store_cung_file_thay_du_lieu_cua_nhau(tmp_path):
    """Hai tiến trình trên cùng host phải serialize — đây là điều Redis từng lo.

    Ca thật: sự cố dual-agent (Đ50) từng có 2 process cùng chạy. Với file chung + flock,
    lease vẫn đúng; với Redis-per-process thì cũng đúng, nhưng file còn CHẶT hơn vì
    không phụ thuộc mạng.
    """
    p = str(tmp_path / "coord.json")
    a, b = LocalCoordStore(path=p), LocalCoordStore(path=p)
    assert await a.set("lease:x", "tok-a", nx=True, ex=60) is True
    assert await b.set("lease:x", "tok-b", nx=True, ex=60) is None
    assert await b.get("lease:x") == "tok-a"


# ── Bootstrap agent phải dùng kho cục bộ, KHÔNG nối Redis từ xa ──────────────

def test_bootstrap_mutation_dung_kho_cuc_bo_khong_can_redis(tmp_path):
    """Agent bật mutation KHÔNG được đòi `AOIP_REDIS_URL` nữa.

    Trước Đ52, thiếu biến này thì bootstrap nổ `AgentBootstrapError`; có biến thì nối vào
    một DNS nội bộ k3s không phân giải được từ VM khách ⇒ mọi recovery chết ở
    `executor_exception: Timeout connecting to server`. Cả hai đường đều sai.
    """
    from aoip.agent import runtime_config as rc

    env = {
        "AOIP_AUDIT_LOG_PATH": str(tmp_path / "audit.log"),
        "AOIP_COORD_STORE_PATH": str(tmp_path / "coord.json"),
        "AOIP_GATE_ALLOWED_FAILURE_MODES": "process_down",
        "AOIP_GATE_ALLOWED_SUBSTRATES": "systemd",
        "AOIP_GATE_SCOPE_PREFIX": "loyalty-uat",
        "AOIP_GATE_MAX_RISK": "0.5",
        "AOIP_GATE_MIN_DIAGNOSIS_CONFIDENCE": "0.7",
        "AOIP_GATE_MAX_DIAGNOSIS_AGE_S": "600",
        "AOIP_ALLOWED_SYSTEMD_UNITS": "payment-api.service",
        "AOIP_AUTO_EXECUTE_ENABLED": "true",
        # CỐ Ý không có AOIP_REDIS_URL
    }
    executor, status = rc.build_agent_runtime(
        mode=rc.MODE_MUTATION_ENABLED, agent_id="agent-1", env=env,
    )
    assert callable(executor)
    assert status.executor_status == rc.STATUS_ACTIVE


def test_bootstrap_bo_qua_AOIP_REDIS_URL_con_sot_lai(tmp_path):
    """`run.env` cũ trên VM vẫn còn dòng `AOIP_REDIS_URL` — không được vì thế mà nối lại.

    Cả 3 VM khách đang có dòng đó; xoá được thì tốt nhưng bootstrap KHÔNG ĐƯỢC phụ thuộc
    vào việc ai đó nhớ xoá.
    """
    from aoip.agent import runtime_config as rc

    env = {
        "AOIP_AUDIT_LOG_PATH": str(tmp_path / "audit.log"),
        "AOIP_COORD_STORE_PATH": str(tmp_path / "coord.json"),
        "AOIP_GATE_ALLOWED_FAILURE_MODES": "process_down",
        "AOIP_GATE_ALLOWED_SUBSTRATES": "systemd",
        "AOIP_GATE_SCOPE_PREFIX": "loyalty-uat",
        "AOIP_GATE_MAX_RISK": "0.5",
        "AOIP_GATE_MIN_DIAGNOSIS_CONFIDENCE": "0.7",
        "AOIP_GATE_MAX_DIAGNOSIS_AGE_S": "600",
        "AOIP_ALLOWED_SYSTEMD_UNITS": "payment-api.service",
        "AOIP_REDIS_URL": "redis://khong-ton-tai.invalid:6379/0",
    }
    executor, status = rc.build_agent_runtime(
        mode=rc.MODE_MUTATION_ENABLED, agent_id="agent-1", env=env,
    )
    assert callable(executor) and status.executor_status == rc.STATUS_ACTIVE
