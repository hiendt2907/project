"""Độ trung thực ngữ cảnh của vòng ReAct chẩn đoán.

Đo trên 32 phiên THẬT trong Redis lab ngày 2026-08-02 (`omni:diag:session:*`):

    tổng lượt              185
    lượt lỗi LLM            85   ← 46%
    phiên cạn 8 lượt        20/32
    phiên kết luận «llm_error»  6   ← lượt 1 đã chẩn ĐÚNG rồi bị vứt
    lượt lệnh bị dedup âm thầm  25
    "context budget exceeded"    0   ← ngữ cảnh CHƯA BAO GIỜ tràn

Con số cuối bác bỏ giả thuyết "phình ngữ cảnh". Nguyên nhân thật là bão đồng
thời: 18 phiên 8-lượt chồng nhau trong 14 phút (10:37→10:51) cùng đập vào một
model 7B duy nhất. `LLMSemaphore` đã có sẵn và làn `reactive` được cấp slot,
nhưng `acquire_reactive()` KHÔNG có call site nào — vòng ReAct đi vòng qua nó.

Sáu bất biến dưới đây khoá lại từng khiếm khuyết đã đo được.
"""
from __future__ import annotations

import json

import pytest

from services.analyst import diagnosis_loop as dl


def _redis():
    from fakeredis.aioredis import FakeRedis

    return FakeRedis(decode_responses=True)


class _FakeResp:
    def __init__(self, content: str):
        self.message = type("M", (), {"content": content})()


class _ScriptedLLM:
    """Mỗi phần tử: dict → trả JSON; Exception → ném (mô phỏng lỗi hạ tầng)."""

    def __init__(self, script: list):
        self._script = script
        self.calls: list[list[dict]] = []

    async def chat(self, *, model, messages, format=None, options=None):
        self.calls.append([dict(m) for m in messages])
        item = self._script[min(len(self.calls) - 1, len(self._script) - 1)]
        if isinstance(item, Exception):
            raise item
        return _FakeResp(json.dumps(item))


class _RecordingSemaphore:
    def __init__(self, fail: bool = False):
        self.acquired: list[str] = []
        self.released: list[str] = []
        self._fail = fail
        self._n = 0

    async def acquire_reactive(self, timeout_s: float = 120.0) -> str:
        if self._fail:
            raise TimeoutError("llm semaphore acquire timeout")
        self._n += 1
        tok = f"r{self._n}"
        self.acquired.append(tok)
        return tok

    async def release(self, token: str) -> None:
        self.released.append(token)


_GOOD_TURN1 = {
    "reasoning": "CPU 100% vượt ngưỡng 80%, load 1m=10.81.",
    "hypothesis": "CPU saturation on host cust-app due to high load average",
    "commands_to_run": [],
    "diagnosis_complete": False,
    "confidence": 0.9,
}
_EV = {"probe": "cpu", "lane": "resource", "alert_hint": "cpu_percent=100", "extracted_fact": {}}


async def _online(redis, agent_id="cust-app"):
    import time as _t

    await redis.set(
        f"{dl._REGISTRY_KEY_PREFIX}{agent_id}",
        json.dumps({"agent_id": agent_id, "last_seen": int(_t.time())}),
    )


# ── Đ-A: kết luận lấy từ lượt TỐT NHẤT, không phải lượt CUỐI ────────────────

def test_best_turn_bo_qua_luot_loi_ha_tang():
    turns = [
        {"turn": 1, "hypothesis": "CPU saturation on cust-app", "confidence": 0.9},
        {"turn": 2, "hypothesis": "llm_error", "confidence": 0.0},
        {"turn": 3, "hypothesis": "llm_error", "confidence": 0.0},
    ]
    best = dl._best_turn(turns)
    assert best["turn"] == 1, "lượt cuối là lỗi hạ tầng — không phải kết luận"


def test_best_turn_chon_confidence_cao_nhat_giua_cac_luot_that():
    turns = [
        {"turn": 1, "hypothesis": "disk pressure", "confidence": 0.4},
        {"turn": 2, "hypothesis": "CPU saturation", "confidence": 0.85},
        {"turn": 3, "hypothesis": "parse_error", "confidence": 0.0},
    ]
    assert dl._best_turn(turns)["turn"] == 2


def test_best_turn_rong_khi_moi_luot_deu_hong():
    assert dl._best_turn([{"turn": 1, "hypothesis": "llm_error", "confidence": 0.0}]) is None


@pytest.mark.asyncio
async def test_ket_luan_dung_cua_luot_1_khong_bi_loi_ha_tang_ghi_de():
    """Ca thật `ra-1d897ff0cc93`: lượt 1 conf=0.85 «CPU saturation», final=«llm_error» conf=0.0."""
    redis = _redis()
    await _online(redis)
    llm = _ScriptedLLM([_GOOD_TURN1] + [RuntimeError("Request timed out")] * 8)

    session = await dl.run_diagnosis_loop(
        redis=redis, llm_client=llm, agent_id="cust-app", ev_doc=_EV, trace_id="t-best",
    )

    assert session["final"]["root_cause"] != "llm_error"
    assert "CPU saturation" in session["final"]["root_cause"]
    assert session["final"]["confidence"] > 0.0


# ── Đ-B: lỗi hạ tầng KHÔNG được ghi vào lịch sử như lời trợ lý ──────────────

@pytest.mark.asyncio
async def test_loi_ha_tang_khong_chui_vao_lich_su_hoi_thoai():
    """Trước đây `messages.append({"role":"assistant", ...})` nhét nguyên dict lỗi vào,
    nên lượt sau model đọc chính "câu trả lời trước" của mình là `hypothesis=llm_error`."""
    redis = _redis()
    await _online(redis)
    llm = _ScriptedLLM([_GOOD_TURN1, RuntimeError("Request timed out"), _GOOD_TURN1])

    await dl.run_diagnosis_loop(
        redis=redis, llm_client=llm, agent_id="cust-app", ev_doc=_EV, trace_id="t-poison",
    )

    for msgs in llm.calls:
        for m in msgs:
            assert "llm_error" not in m["content"], f"lịch sử bị nhiễm: {m['content'][:120]}"
            assert "LLM error:" not in m["content"]


# ── Đ-C: ngắt mạch khi LLM chết liên tiếp ───────────────────────────────────

@pytest.mark.asyncio
async def test_ngat_mach_sau_n_loi_lien_tiep():
    """Đo thật: 7 lượt lỗi liên tiếp × timeout 120s = phiên treo ~14 phút, và mỗi
    lượt lại đập thêm vào chính con model đang quá tải — bão tự khuếch đại."""
    redis = _redis()
    await _online(redis)
    llm = _ScriptedLLM([_GOOD_TURN1] + [RuntimeError("Request timed out")] * 20)

    session = await dl.run_diagnosis_loop(
        redis=redis, llm_client=llm, agent_id="cust-app", ev_doc=_EV, trace_id="t-breaker",
    )

    assert len(llm.calls) <= 1 + dl._MAX_CONSECUTIVE_LLM_ERRORS
    assert session["total_turns"] < dl._MAX_TURNS


@pytest.mark.asyncio
async def test_mot_loi_don_le_khong_ngat_mach():
    redis = _redis()
    await _online(redis)
    done = {**_GOOD_TURN1, "diagnosis_complete": True, "root_cause": "CPU saturation on cust-app"}
    llm = _ScriptedLLM([_GOOD_TURN1, RuntimeError("blip"), done])

    session = await dl.run_diagnosis_loop(
        redis=redis, llm_client=llm, agent_id="cust-app", ev_doc=_EV, trace_id="t-blip",
    )

    assert "CPU saturation" in session["final"]["root_cause"]


# ── Đ-D: lệnh bị chặn/dedup phải NÓI RÕ lý do cho LLM ───────────────────────

def test_followup_noi_ro_ly_do_khi_lenh_bi_dedup():
    """Ca thật `ra-7be04b7fb43e`: 7/8 lượt lệnh bị dedup, model chỉ nhận
    "(no commands were dispatched)" nên hỏi lại y hệt cho tới khi cạn lượt."""
    text = dl._build_followup_context(
        [], next_turn=3, suppressed=[{"command": "df", "args": ["-h"]}],
    )
    assert "df -h" in text
    assert "already" in text.lower() or "đã" in text.lower()
    assert "(no commands were dispatched)" not in text


def test_followup_giu_nguyen_thong_bao_cu_khi_khong_co_gi_bi_chan():
    assert "(no commands were dispatched)" in dl._build_followup_context([], next_turn=2)


# ── Đ-E: ngân sách ngữ cảnh phải chừa chỗ cho phần model SINH RA ────────────

def test_ngan_sach_tru_phan_sinh_ra():
    """num_ctx là cửa sổ CHUNG: prompt + completion. Không trừ `num_predict` thì
    ngay tại biên Ollama vẫn âm thầm cắt ĐẦU ngữ cảnh — tức là mất system prompt."""
    num_ctx = 2000
    per_msg = (num_ctx - dl._NUM_PREDICT) * dl._CHARS_PER_TOKEN // 4
    messages = [
        {"role": "system", "content": "S" * per_msg},
        {"role": "user", "content": "U" * per_msg},
        {"role": "assistant", "content": "A" * per_msg},
        {"role": "user", "content": "B" * per_msg},
        {"role": "assistant", "content": "C" * per_msg},
        {"role": "user", "content": "D" * per_msg},
        {"role": "assistant", "content": "E" * per_msg},
    ]
    out = dl._enforce_context_budget(messages, num_ctx)

    assert sum(len(m["content"]) for m in out) < num_ctx * dl._CHARS_PER_TOKEN
    assert out[0]["content"].startswith("S"), "system prompt phải nguyên vẹn"


# ── Đ-F: vòng ReAct phải xếp hàng qua làn reactive của LLMSemaphore ─────────

@pytest.mark.asyncio
async def test_moi_luot_lay_va_tra_slot_lan_reactive():
    """`acquire_reactive()` có 0 call site trong toàn repo trước bản vá, trong khi
    làn reactive vẫn được cấp slot — cơ chế chống bão đã dựng nhưng chưa nối."""
    redis = _redis()
    await _online(redis)
    sem = _RecordingSemaphore()
    done = {**_GOOD_TURN1, "diagnosis_complete": True, "root_cause": "CPU saturation on cust-app"}
    llm = _ScriptedLLM([_GOOD_TURN1, done])

    await dl.run_diagnosis_loop(
        redis=redis, llm_client=llm, agent_id="cust-app", ev_doc=_EV,
        trace_id="t-sem", semaphore=sem,
    )

    assert len(sem.acquired) == len(llm.calls) >= 2
    assert sem.released == sem.acquired, "rò slot = làn reactive cạn dần rồi tắc vĩnh viễn"


@pytest.mark.asyncio
async def test_slot_duoc_tra_ve_ca_khi_llm_nem_loi():
    redis = _redis()
    await _online(redis)
    sem = _RecordingSemaphore()
    llm = _ScriptedLLM([_GOOD_TURN1] + [RuntimeError("Request timed out")] * 8)

    await dl.run_diagnosis_loop(
        redis=redis, llm_client=llm, agent_id="cust-app", ev_doc=_EV,
        trace_id="t-sem-err", semaphore=sem,
    )

    assert sem.released == sem.acquired


@pytest.mark.asyncio
async def test_khong_lay_duoc_slot_thi_suy_giam_chu_khong_vo():
    redis = _redis()
    await _online(redis)
    llm = _ScriptedLLM([_GOOD_TURN1] * 8)

    session = await dl.run_diagnosis_loop(
        redis=redis, llm_client=llm, agent_id="cust-app", ev_doc=_EV,
        trace_id="t-sem-busy", semaphore=_RecordingSemaphore(fail=True),
    )

    assert llm.calls == [], "hết slot ⇒ KHÔNG được gọi LLM (đó là điểm của semaphore)"
    assert "root_cause" in session["final"], "vẫn phải trả về session hợp lệ, không crash"
    assert session["final"]["confidence"] == 0.0


@pytest.mark.asyncio
async def test_semaphore_la_tuy_chon_khong_pha_call_site_cu():
    redis = _redis()
    await _online(redis)
    done = {**_GOOD_TURN1, "diagnosis_complete": True, "root_cause": "CPU saturation on cust-app"}
    session = await dl.run_diagnosis_loop(
        redis=redis, llm_client=_ScriptedLLM([_GOOD_TURN1, done]),
        agent_id="cust-app", ev_doc=_EV, trace_id="t-nosem",
    )
    assert "CPU saturation" in session["final"]["root_cause"]


# ── Đ-G (phản biện agent, 2026-08-02): 3 lỗi HIGH trong chính bản vá Đ-F ────

class _FlakyReleaseSemaphore(_RecordingSemaphore):
    """release() ném lỗi mạng thoáng qua — đúng ca Redis delete/rpush lỗi tạm thời."""

    async def release(self, token: str) -> None:
        raise ConnectionError("redis rpush failed transiently")


@pytest.mark.asyncio
async def test_loi_release_semaphore_khong_lam_mat_ca_phien():
    """`finally: await semaphore.release(...)` không tự bọc lỗi ⇒ exception ở
    RELEASE (xảy ra ở MỌI lượt, không chỉ khi bận) thoát thẳng khỏi vòng for,
    khiến `redis.set(session)` cuối hàm KHÔNG BAO GIỜ chạy — vi phạm
    INV_DIAG_STORED một cách âm thầm, mất luôn kết luận ĐÚNG đã có ở lượt 1."""
    redis = _redis()
    await _online(redis)
    done = {**_GOOD_TURN1, "diagnosis_complete": True, "root_cause": "CPU saturation on cust-app"}
    llm = _ScriptedLLM([_GOOD_TURN1, done])

    session = await dl.run_diagnosis_loop(
        redis=redis, llm_client=llm, agent_id="cust-app", ev_doc=_EV,
        trace_id="t-release-flaky", semaphore=_FlakyReleaseSemaphore(),
    )

    assert "CPU saturation" in session["final"]["root_cause"]
    assert await redis.get(f"{dl._SESSION_KEY_PREFIX}t-release-flaky") is not None


def test_fallback_remediation_co_nhanh_cpu():
    """Ca thật `ra-1d897ff0cc93`: root_cause khôi phục bởi `_best_turn` là
    "CPU saturation on host cust-app due to high load average", nhưng
    `_fallback_remediation_steps` không có nhánh CPU nên rơi vào catch-all
    (df -h, free -h, journalctl --failed) — không liên quan gì tới CPU."""
    steps = dl._fallback_remediation_steps("CPU saturation on host cust-app due to high load average")
    assert any("top" in s.lower() or "cpu" in s.lower() or "load" in s.lower() for s in steps)
    assert not any("df -h" in s for s in steps), "đây là remediation CPU, không phải đĩa"


@pytest.mark.asyncio
async def test_phien_het_slot_ngay_luot_1_duoc_danh_dau_khac_phien_that():
    """0 lượt vì hết slot LLM và 0 lượt vì phiên thật-nhưng-rỗng phải phân
    biệt được ở `degraded_reason` — đây đúng loại tín hiệu bộ đo 46% lỗi hạ
    tầng cần để giám sát tiếp (CRAT/KPI downstream không tự suy ra được)."""
    redis = _redis()
    await _online(redis)
    session = await dl.run_diagnosis_loop(
        redis=redis, llm_client=_ScriptedLLM([_GOOD_TURN1]), agent_id="cust-app",
        ev_doc=_EV, trace_id="t-sem-busy-flag", semaphore=_RecordingSemaphore(fail=True),
    )
    assert session["degraded"] is True
    assert "llm" in session["degraded_reason"].lower() or "slot" in session["degraded_reason"].lower()


@pytest.mark.asyncio
async def test_agent_offline_van_giu_nguyen_chuoi_cu():
    """Không được phá vỡ assertion cũ (`test_diag_loop_tooling.py`) khi thêm lý do mới."""
    redis = _redis()
    session = await dl.run_diagnosis_loop(
        redis=redis, llm_client=_ScriptedLLM([_GOOD_TURN1] * 3),
        agent_id="cust-offline", ev_doc=_EV, trace_id="t-offline-unchanged",
    )
    assert session["degraded"] is True
    assert "agent_offline" in session["degraded_reason"]
