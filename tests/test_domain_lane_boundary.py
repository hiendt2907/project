"""Hàng rào giữa ba trục cùng tên "lane" và taxonomy domain.

Bối cảnh: `plans/lane-to-domain-and-omni-decides-2026-07-30.md` §0. "lane" trong repo
này là BA khái niệm khác nhau. Chỉ trục A (`envelope.lane`) map sang domain. Trục B
(`proof_lane`) lái cổng chống-bịa `ERR_REA_NO_PHYSICAL_PROOF`; trục C
(`proactive`/`reactive`) là pool đồng thời LLM.

Các test dưới đây tồn tại để một lần refactor "cho gọn" trong tương lai không lặng lẽ
hoà tan ba trục vào nhau. Chúng là hàng rào, không phải test tính năng.
"""
from __future__ import annotations

from pathlib import Path

from pkg.domain.taxonomy import (
    APPLICATION,
    CANONICAL_DOMAINS,
    LANE_TO_DOMAIN,
    OS_HOST,
    SECURITY,
    UNKNOWN,
    lane_to_domain,
    normalize_domain,
)
from pkg.reasoning.incident_matrix_profile import VALID_PROOF_LANES

_SRC = Path(__file__).resolve().parents[1] / "src"


# ── Trục A → domain ──────────────────────────────────────────────────────────


def test_lane_to_domain_covers_every_axis_a_value() -> None:
    """Cả 5 giá trị lane trục A đang tồn tại trong code phải có câu trả lời."""
    for lane in (
        "SYS_RESOURCE",
        "SYS_HARD_FAIL",
        "APP_HTTP",
        "SIEM_SECURITY",
        "ONBOARDING_DISCOVERY",
    ):
        assert lane.lower() in LANE_TO_DOMAIN, f"lane truc A thieu map: {lane}"


def test_lane_to_domain_known_mappings() -> None:
    assert lane_to_domain("SYS_RESOURCE") == OS_HOST
    assert lane_to_domain("APP_HTTP") == APPLICATION
    assert lane_to_domain("SIEM_SECURITY") == SECURITY


def test_sys_hard_fail_stays_unknown() -> None:
    """SYS_HARD_FAIL gánh 4 domain (database/storage/service/kubernetes).

    Suy nó thành một domain cụ thể là mất thông tin, và domain đó được dùng để cấp
    quyền. `unknown` là câu trả lời trung thực — đừng "sửa cho đẹp".
    """
    assert lane_to_domain("SYS_HARD_FAIL") == UNKNOWN
    assert lane_to_domain("ONBOARDING_DISCOVERY") == UNKNOWN


def test_lane_to_domain_is_case_and_dash_insensitive() -> None:
    assert lane_to_domain("sys-resource") == OS_HOST
    assert lane_to_domain("  Sys_Resource  ") == OS_HOST


def test_lane_to_domain_unrecognised_is_unknown_not_raise() -> None:
    """Đường đọc dữ liệu lịch sử — không được ném lỗi vì một nhãn lạ."""
    assert lane_to_domain("WHATEVER_NEW_LANE") == UNKNOWN
    assert lane_to_domain(None) == UNKNOWN
    assert lane_to_domain("") == UNKNOWN


# ── Trục B (proof_lane) phải nằm ngoài taxonomy domain ───────────────────────


def test_proof_lanes_are_not_domains() -> None:
    """Không giá trị proof_lane nào được là một domain canonical.

    Nếu ai đó thêm alias `resource`/`state`/`app_log` vào taxonomy, cổng chống-bịa
    và taxonomy domain bắt đầu dùng chung từ vựng — rồi một `proof_lane` sẽ lặng lẽ
    đi qua `normalize_domain` thành domain hợp lệ.
    """
    for pl in VALID_PROOF_LANES:
        assert pl not in CANONICAL_DOMAINS, f"proof_lane {pl!r} tro thanh domain canonical"
        assert normalize_domain(pl) == UNKNOWN, (
            f"proof_lane {pl!r} da co alias domain — xem canh bao trong taxonomy.py"
        )


def test_proof_lane_resolution_does_not_depend_on_taxonomy() -> None:
    """`resolve_proof_lane` không được lấy giá trị từ taxonomy domain.

    Test đọc source thay vì gọi hàm: điều cần bảo vệ là *sự độc lập của hai module*,
    và nó chỉ vỡ khi có người thêm import — thứ chỉ thấy được ở tầng source.
    """
    src = (_SRC / "pkg" / "reasoning" / "incident_matrix_profile.py").read_text(encoding="utf-8")
    assert "pkg.domain.taxonomy" not in src, (
        "incident_matrix_profile (proof_lane, truc B) da import taxonomy domain — "
        "hai truc phai doc lap"
    )


def test_siem_is_a_domain_alias_but_not_a_proof_lane() -> None:
    """`siem` là ngoại lệ có chủ đích: alias domain, nhưng không phải proof_lane."""
    assert normalize_domain("siem") == SECURITY
    assert "siem" not in VALID_PROOF_LANES


# ── Trục C (semaphore) phải nằm ngoài taxonomy domain ────────────────────────


def test_semaphore_lanes_are_not_domains() -> None:
    for word in ("proactive", "reactive"):
        assert normalize_domain(word) == UNKNOWN, (
            f"{word!r} (pool dong thoi LLM, truc C) da co alias domain"
        )
