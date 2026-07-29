"""Test tính chất cho `case_ledger.scoring` — tầng chống bùa số.

Mỗi test ở đây bảo vệ MỘT tính chất của thiết kế sổ ca
(`plans/case-ledger-design-2026-07-30.md`). Chúng cố tình không kiểm tra
"hàm có chạy không" mà kiểm tra "hàm có còn khó làm đẹp không" — vì toàn bộ
giá trị của module này nằm ở chỗ nó không cho phép Omni trông giỏi hơn thực tế.
"""

from __future__ import annotations

from services.case_ledger.scoring import (
    DEFAULT_MIN_ACCURACY_LB,
    build_competency_report,
    wilson_lower_bound,
)


def _case(
    posture: str = "DIAGNOSED",
    diagnosis: str = "UNJUDGED",
    remedy: str = "UNJUDGED",
    recurred: bool = False,
) -> dict:
    return {
        "posture": posture,
        "diagnosis_verdict": diagnosis,
        "remedy_verdict": remedy,
        "recurred": recurred,
    }


def _report(cases: list[dict], **kw):
    return build_competency_report(
        cases, pattern_key="p1", tenant_id="t1", **kw
    )


# ── Cận dưới Wilson ──────────────────────────────────────────────────────────


def test_wilson_3_tren_3_khong_phai_bang_chung_du_manh():
    """3/3 = 100% phải bị phạt xuống dưới 0.5.

    Đây là lý do tồn tại của cả module. Tỉ lệ thô 3/3 nói dối một cách hợp pháp:
    ba lần đúng liên tiếp hoàn toàn có thể là may. Nếu ai đó đổi hàm này sang
    ``successes/total``, Omni sẽ xin được quyền tự thực thi sau đúng ba ca may.
    """
    lb = wilson_lower_bound(3, 3)
    assert 0.0 < lb < 0.5
    assert lb < DEFAULT_MIN_ACCURACY_LB


def test_wilson_tang_don_dieu_theo_n_khi_giu_nguyen_ti_le():
    """Cùng tỉ lệ 100%, nhiều mẫu hơn phải cho cận dưới cao hơn hẳn.

    Tính chất này thay thế cho một ngưỡng ``n`` do người tự nghĩ ra. Nếu nó mất,
    hệ thống lại cần một hằng số tuỳ tiện mà ai cũng có thể nới dần.
    """
    seq = [wilson_lower_bound(n, n) for n in (3, 10, 30, 100)]
    assert seq == sorted(seq)
    assert all(a < b for a, b in zip(seq, seq[1:]))
    assert wilson_lower_bound(30, 30) > wilson_lower_bound(3, 3) + 0.3
    # Nhưng không bao giờ chạm 1.0 — bất định không bao giờ biến mất hoàn toàn.
    assert wilson_lower_bound(100, 100) < 1.0


def test_wilson_khong_mau_tra_ve_0_khong_chia_cho_0():
    """n=0 phải ra 0.0, không ZeroDivisionError.

    Pattern chưa gặp ca nào là trường hợp thường gặp nhất lúc khởi động tenant
    mới; nổ ở đây là làm sập đường báo cáo năng lực.
    """
    assert wilson_lower_bound(0, 0) == 0.0
    assert wilson_lower_bound(5, 0) == 0.0
    assert wilson_lower_bound(0, 10) == 0.0


def test_wilson_luon_nam_trong_0_1_va_khong_vuot_ti_le_tho():
    """Cận dưới không bao giờ vượt điểm ước lượng — nếu vượt thì nó không phải cận dưới."""
    for successes, total in ((0, 5), (1, 5), (4, 5), (5, 5), (17, 40)):
        lb = wilson_lower_bound(successes, total)
        assert 0.0 <= lb <= successes / total


# ── PARTIAL ──────────────────────────────────────────────────────────────────


def test_partial_khong_duoc_tinh_la_thanh_cong():
    """Nửa đúng không phải bằng chứng để trao quyền.

    PARTIAL vẫn vào MẪU SỐ (đã được chấm) nhưng không vào tử số. Nếu ai đó tính
    PARTIAL là CORRECT thì độ chính xác sẽ phồng lên đúng bằng tỉ lệ ca mơ hồ —
    mà ca mơ hồ chính là ca Omni yếu nhất.
    """
    cases = [_case(diagnosis="CORRECT")] * 5 + [_case(diagnosis="PARTIAL")] * 5
    rep = _report(cases)
    assert rep.partial == 5
    assert rep.correct == 5
    assert rep.accuracy_raw == 0.5  # 5 / (5 correct + 0 incorrect + 5 partial)
    assert rep.accuracy_lower_bound < 0.5


# ── Chống bùa số bằng từ chối ────────────────────────────────────────────────


def test_refused_khong_vao_mau_so_do_chinh_xac():
    """Ca REFUSED không có chẩn đoán nào để chấm → không được làm loãng độ chính xác."""
    cases = [_case(diagnosis="CORRECT")] * 4 + [_case(posture="REFUSED")] * 6
    rep = _report(cases)
    assert rep.refused == 6
    assert rep.correct == 4
    assert rep.incorrect == 0
    assert rep.accuracy_raw == 1.0  # mẫu số chỉ gồm 4 ca DIAGNOSED đã chấm


def test_tu_choi_moi_ca_kho_thi_do_chinh_xac_cao_nhung_KHONG_eligible():
    """Test then chốt của cả thiết kế — chống chiến lược "chỉ nhận việc dễ".

    Một Omni từ chối mọi ca khó và chỉ xử lý ca dễ sẽ có hồ sơ chính xác đẹp
    (30/30 đúng, cận dưới ~0.88). Nếu chỉ đo độ chính xác thì nó đủ điều kiện
    xin quyền — trông như cẩn thận, thực chất vô dụng vì 60 ca khó đã đẩy hết
    cho người.

    Độ phủ là số kéo ngược lại: DIAGNOSED/(DIAGNOSED+REFUSED) = 0.33 → trượt.
    Nếu test này biến mất, cả cơ chế đánh giá năng lực mất ý nghĩa.
    """
    cases = [_case(diagnosis="CORRECT")] * 30 + [_case(posture="REFUSED")] * 60
    rep = _report(cases)

    assert rep.accuracy_raw == 1.0
    assert rep.accuracy_lower_bound > DEFAULT_MIN_ACCURACY_LB  # hồ sơ "đẹp"
    assert rep.coverage < 0.5  # nhưng độ phủ tố cáo
    assert rep.eligible is False
    assert any("độ phủ" in b for b in rep.blockers)


def test_lam_that_thi_ca_hai_so_cung_dep_va_eligible():
    """Đối chứng dương: không từ chối bừa, chấm đủ, đúng nhiều → mới qua.

    Nếu không có test này thì mọi thay đổi khiến `eligible` luôn False cũng sẽ
    "xanh" — một hàng rào luôn đóng cũng vô dụng như một hàng rào luôn mở.
    """
    cases = (
        [_case(diagnosis="CORRECT")] * 40
        + [_case(diagnosis="INCORRECT")] * 5
        + [_case(diagnosis="UNJUDGED")] * 5
        + [_case(posture="REFUSED")] * 20
    )
    rep = _report(cases)
    assert rep.coverage > 0.5
    assert rep.unjudged_ratio < 0.4
    assert rep.accuracy_lower_bound >= DEFAULT_MIN_ACCURACY_LB
    assert rep.eligible is True
    assert rep.blockers == ()


# ── OUT_OF_SCOPE ─────────────────────────────────────────────────────────────


def test_out_of_scope_khong_vao_mau_so_do_phu():
    """Giới hạn quyền do NGƯỜI đặt, phạt Omni vì nó là vô lý.

    OUT_OF_SCOPE = chẩn đoán được nhưng ngoài quyền hạn (code/kiến trúc). Nếu
    những ca này vào mẫu số độ phủ thì một tenant siết quyền chặt sẽ vĩnh viễn
    khoá Omni ở tier thấp bất kể nó làm tốt tới đâu.
    """
    khong_oos = [_case(diagnosis="CORRECT")] * 30 + [_case(posture="REFUSED")] * 10
    co_oos = khong_oos + [_case(posture="OUT_OF_SCOPE")] * 50

    r1, r2 = _report(khong_oos), _report(co_oos)
    assert r2.out_of_scope == 50
    assert r2.coverage == r1.coverage  # 50 ca ngoài phạm vi không đổi độ phủ
    assert r2.total_cases == r1.total_cases + 50  # nhưng vẫn hiện trong tổng
    assert r2.eligible is True


# ── Im lặng ──────────────────────────────────────────────────────────────────


def test_ti_le_unjudged_cao_tu_chan_eligible():
    """Im lặng không phải đồng ý.

    Ca UNJUDGED không vào tử số lẫn mẫu số độ chính xác — nên một Omni chỉ được
    chấm vài ca may mắn sẽ có accuracy 1.0. Chặn nằm ở `unjudged_ratio`: đa số
    phát biểu không ai xác nhận thì không có bằng chứng gì cả.
    """
    cases = [_case(diagnosis="CORRECT")] * 30 + [_case(diagnosis="UNJUDGED")] * 70
    rep = _report(cases)
    assert rep.unjudged == 70
    assert rep.accuracy_raw == 1.0
    assert rep.unjudged_ratio == 0.7
    assert rep.eligible is False
    assert any("chưa phán quyết" in b for b in rep.blockers)


def test_khong_ca_nao_duoc_cham_thi_khong_eligible_va_ratio_bang_1():
    """Sổ rỗng / chưa ai chấm → phải trượt kèm lý do rõ, không phải "đủ điều kiện mặc định"."""
    rong = _report([])
    assert rong.total_cases == 0
    assert rong.unjudged_ratio == 1.0
    assert rong.eligible is False
    assert any("chưa có ca nào được chẩn đoán" in b for b in rong.blockers)


# ── Blockers nêu đúng lý do ──────────────────────────────────────────────────


def test_blockers_neu_dung_ly_do_truot_do_chinh_xac():
    """Trượt vì đoán sai nhiều thì blocker phải nói về độ chính xác, không nói lung tung."""
    cases = [_case(diagnosis="CORRECT")] * 10 + [_case(diagnosis="INCORRECT")] * 10
    rep = _report(cases)
    assert rep.eligible is False
    assert any("cận dưới độ chính xác" in b for b in rep.blockers)
    assert not any("độ phủ" in b for b in rep.blockers)
    assert not any("chưa phán quyết" in b for b in rep.blockers)


def test_blockers_gom_du_moi_ly_do_khi_truot_nhieu_tieu_chi():
    """Trượt cả ba thì phải liệt kê cả ba — báo cáo cho admin khách phải đủ, không dừng ở lỗi đầu."""
    cases = (
        [_case(diagnosis="INCORRECT")] * 5
        + [_case(diagnosis="UNJUDGED")] * 20
        + [_case(posture="REFUSED")] * 60
    )
    rep = _report(cases)
    assert rep.eligible is False
    assert any("cận dưới độ chính xác" in b for b in rep.blockers)
    assert any("độ phủ" in b for b in rep.blockers)
    assert any("chưa phán quyết" in b for b in rep.blockers)


# ── Hai nhãn tách rời + số liệu phụ ──────────────────────────────────────────


def test_hai_nhan_tach_roi_cham_remedy_cho_ket_qua_khac_diagnosis():
    """Đoán trúng nguyên nhân nhưng xử lý dở là chuyện thường — hai nhãn phải chấm độc lập.

    Gộp một nhãn là mất vĩnh viễn thông tin Omni yếu ở khâu nào.
    """
    cases = [_case(diagnosis="CORRECT", remedy="INCORRECT")] * 20
    chan_doan = _report(cases, verdict_field="diagnosis_verdict")
    khac_phuc = _report(cases, verdict_field="remedy_verdict")
    assert chan_doan.accuracy_raw == 1.0
    assert khac_phuc.accuracy_raw == 0.0
    assert chan_doan.eligible is True
    assert khac_phuc.eligible is False


def test_recurrence_rate_tinh_tren_toan_bo_ca_ke_ca_refused():
    """Tái diễn là nhãn mạnh nhất (đo từ hệ thống khách) — REFUSED tái diễn vẫn phải đếm."""
    cases = (
        [_case(diagnosis="CORRECT", recurred=True)] * 2
        + [_case(diagnosis="CORRECT")] * 2
        + [_case(posture="REFUSED", recurred=True)] * 2
        + [_case(posture="OUT_OF_SCOPE")] * 4
    )
    rep = _report(cases)
    assert rep.total_cases == 10
    assert rep.recurrence_rate == 0.4


def test_as_dict_giu_du_moi_con_so_de_khach_tai_dung():
    """Báo cáo phải tái dựng được từ ledger — thiếu một trường là mất khả năng đối chiếu."""
    rep = _report([_case(diagnosis="CORRECT")] * 3)
    d = rep.as_dict()
    for key in (
        "pattern_key", "tenant_id", "total_cases", "diagnosed", "refused",
        "out_of_scope", "correct", "incorrect", "partial", "unjudged",
        "accuracy_lower_bound", "accuracy_raw", "coverage", "unjudged_ratio",
        "recurrence_rate", "eligible", "blockers",
    ):
        assert key in d
    assert isinstance(d["blockers"], list)
    assert d["accuracy_lower_bound"] == round(rep.accuracy_lower_bound, 4)


def test_muc_phat_mau_nho_duoc_neo_bang_so_cu_the():
    """Neo mức phạt mẫu nhỏ thành số cụ thể: 3/3 → ~0.44, và trượt ở ngưỡng mặc định.

    Neo con số vì `DEFAULT_MIN_ACCURACY_LB` là knob duy nhất có thể nới. Nếu ai đó
    hạ ngưỡng xuống dưới ~0.44 thì ba ca may là đủ xin quyền — test này bắt đúng
    lúc đó, thay vì để thay đổi trôi qua im lặng.
    """
    rep = _report([_case(diagnosis="CORRECT")] * 3)
    assert rep.accuracy_raw == 1.0
    assert 0.43 < rep.accuracy_lower_bound < 0.45
    assert DEFAULT_MIN_ACCURACY_LB > rep.accuracy_lower_bound
    assert rep.eligible is False
    assert any("cận dưới độ chính xác" in b for b in rep.blockers)
