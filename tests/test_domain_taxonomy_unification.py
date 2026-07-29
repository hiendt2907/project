"""Từ vựng domain hợp nhất + tách `track` khỏi `domain` (migration 0013).

Bối cảnh đo runtime 2026-07-29/30:
    Redis  omni:playbook:grad:default:k8s:PB-K8S-CPU-RESTART   ← playbook_governor
    PG     playbook_graduation.domain = 'advisory'  (3 hàng)   ← advisory_promoter
`advisory` không phải domain — nó là NGUỒN HỌC. Hai writer không biết nhau, nên
`list_playbook_graduations()` (tier_loops/capacity_loops đọc để đề xuất NÂNG TIER) trả
hỗn hợp hai loại bản ghi khác bản chất. Con số dùng để trao quyền tự chủ đếm gộp.

Các test dưới đây khoá lại đúng bốn điều dễ trôi lại nhất.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, get_args

import pytest
from fakeredis.aioredis import FakeRedis

from pkg.domain.taxonomy import (
    ALL_TRACKS,
    CANONICAL_DOMAINS,
    TRACK_ADVISORY,
    TRACK_PLAYBOOK,
    UNKNOWN,
    normalize_domain,
    require_domain,
    split_legacy_graduation_domain,
)

# ---------------------------------------------------------------------------
# (a) Mọi giá trị của cả BA từ vựng cũ map về canonical
# ---------------------------------------------------------------------------

# Nguyên văn ba danh sách cũ, giữ lại ở đây làm bằng chứng lịch sử: nếu ai xoá một alias
# khỏi taxonomy, test này chỉ đúng cái tên bị xoá.
_LEGACY_AOIP = {
    "linux": "os_host",
    "kubernetes": "kubernetes",
    "database": "database",
    "network": "network",
}
_LEGACY_DOMAIN_SIGNALS = {
    "os_system": "os_host",
    "network": "network",
    "storage": "storage",
    "services": "service",
    "container_logs": "kubernetes",
    "database": "database",
    "application": "application",
    "security": "security",
}
_LEGACY_PLAYBOOK = {
    "k8s": "kubernetes",
    "os": "os_host",
    "network": "network",
    "service": "service",
    "application": "application",
    "api": "application",
    "hardware": "hardware",
}


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        *_LEGACY_AOIP.items(),
        *_LEGACY_DOMAIN_SIGNALS.items(),
        *_LEGACY_PLAYBOOK.items(),
    ],
)
def test_every_legacy_domain_value_maps_to_canonical(legacy: str, canonical: str) -> None:
    assert normalize_domain(legacy) == canonical
    assert canonical in CANONICAL_DOMAINS


def test_legacy_vocabularies_cover_every_canonical_domain_except_storage_gaps() -> None:
    """Ba từ vựng cũ gộp lại phải phủ đủ CANONICAL_DOMAINS — nếu không, có domain nào
    được sinh ra từ hư không mà không ai từng gọi tên."""
    covered = {
        normalize_domain(k)
        for k in (*_LEGACY_AOIP, *_LEGACY_DOMAIN_SIGNALS, *_LEGACY_PLAYBOOK)
    }
    assert covered == set(CANONICAL_DOMAINS)


def test_domain_signals_constants_are_canonical() -> None:
    """Đổi GIÁ TRỊ trả về, không đổi hành vi phân loại: 8 hằng vẫn phải đôi một khác nhau
    (trùng nhau ⇒ hai nhánh cascade bị trộn) và đều canonical."""
    from pkg.reasoning import domain_signals as ds

    assert len(set(ds.ALL_DOMAINS)) == 8
    assert set(ds.ALL_DOMAINS) <= set(CANONICAL_DOMAINS)
    assert ds.DOMAIN_OS == "os_host"
    assert ds.DOMAIN_CONTAINER == "kubernetes"
    assert ds.DOMAIN_SERVICES == "service"
    assert ds.DOMAIN_UNKNOWN == UNKNOWN


def test_domain_signals_classification_behaviour_unchanged() -> None:
    """Cascade phân loại giữ nguyên: cùng input → cùng NHÁNH, chỉ khác tên trả về."""
    from pkg.reasoning.domain_signals import (
        DOMAIN_CONTAINER,
        DOMAIN_DATABASE,
        DOMAIN_OS,
        DOMAIN_SERVICES,
        detect_domain,
    )

    assert detect_domain("remote_system_metrics", "", "", "SYS_RESOURCE") == DOMAIN_OS
    assert detect_domain("container_log_nginx", "", "", "APP_HTTP") == DOMAIN_CONTAINER
    assert detect_domain("mysql_status", "", "", "SYS_HARD_FAIL") == DOMAIN_DATABASE
    assert detect_domain("systemd_unit_check", "", "", "SYS_HARD_FAIL") == DOMAIN_SERVICES


def test_aoip_default_registry_uses_canonical_domains() -> None:
    from aoip.domain_adapters import default_registry

    reg = default_registry()
    assert {d.domain for d in reg.list_adapters()} <= set(CANONICAL_DOMAINS)
    # `linux` cũ nay tra được bằng canonical...
    assert reg.get("os_host") is not None
    # ...và vẫn tra được bằng tên cũ, vì `get` chuẩn hoá khi ĐỌC là điều kiện để
    # payload/API caller phiên bản cũ không chết.
    assert reg.get("linux") is None or reg.get("linux").domain == "os_host"


# ---------------------------------------------------------------------------
# (c) require_domain ném lỗi, normalize_domain trả unknown
# ---------------------------------------------------------------------------

def test_normalize_domain_bogus_returns_unknown() -> None:
    assert normalize_domain("bogus") == UNKNOWN
    assert normalize_domain(None) == UNKNOWN
    assert normalize_domain("") == UNKNOWN


def test_require_domain_bogus_raises() -> None:
    with pytest.raises(ValueError, match="khong nhan ra"):
        require_domain("bogus")
    # 'unknown' tường minh KHÔNG phải lỗi: backfill hàng advisory buộc phải ghi nó.
    assert require_domain("unknown") == UNKNOWN


def test_split_legacy_graduation_domain_matches_migration() -> None:
    """Logic Python phải KHỚP backfill trong migrations/omni_admin/0013_*.sql."""
    assert split_legacy_graduation_domain("advisory") == (UNKNOWN, TRACK_ADVISORY)
    assert split_legacy_graduation_domain("k8s") == ("kubernetes", TRACK_PLAYBOOK)
    assert split_legacy_graduation_domain("bogus") == (UNKNOWN, TRACK_PLAYBOOK)


# ---------------------------------------------------------------------------
# (d) PlaybookDomain và CANONICAL_DOMAINS không thể lệch nhau
# ---------------------------------------------------------------------------

def test_playbook_domain_literal_cannot_drift_from_canonical() -> None:
    """Thêm domain vào một chỗ mà quên chỗ kia ⇒ test này đỏ (và import cũng vỡ)."""
    from workers.schemas.playbook import PlaybookDomain

    assert set(get_args(PlaybookDomain)) == set(CANONICAL_DOMAINS)


def test_playbook_spec_reads_legacy_domain_value() -> None:
    """Spec đã lưu trong Redis từ trước dùng 'k8s' — phải vẫn validate được."""
    from workers.schemas.playbook import PlaybookSpec

    spec = PlaybookSpec.model_validate({
        "playbook_id": "PB-K8S-CPU-RESTART",
        "name": "restart",
        "domain": "k8s",
        "steps": [{"step_order": 1, "action": "k8s_rollout_restart"}],
    })
    assert spec.domain == "kubernetes"


def test_playbook_spec_rejects_bogus_domain() -> None:
    from workers.schemas.playbook import PlaybookSpec

    with pytest.raises(Exception):
        PlaybookSpec.model_validate({
            "playbook_id": "PB-X", "name": "x", "domain": "bogus",
            "steps": [{"step_order": 1, "action": "k8s_rollout_restart"}],
        })


# ---------------------------------------------------------------------------
# (b) list_playbook_graduations phân biệt được track
# ---------------------------------------------------------------------------

class _FakeConn:
    """Ghi lại SQL + args, và trả hàng đã lọc theo đúng WHERE mà repo dựng.

    Không mock ở tầng repo: cái cần chứng minh là repo TRUYỀN được điều kiện track
    xuống SQL, nên phải kiểm chính chuỗi SQL và tham số.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append((sql, args))
        out = [r for r in self._rows if r["tenant_id"] == args[0]]
        if "track=$" in sql:
            idx = int(sql.split("track=$")[1][0]) - 1
            out = [r for r in out if r["track"] == args[idx]]
        if "state=$" in sql:
            idx = int(sql.split("state=$")[1][0]) - 1
            out = [r for r in out if r["state"] == args[idx]]
        return out

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        self.queries.append((sql, args))
        return {
            "tenant_id": args[0], "domain": args[1], "playbook_id": args[2],
            "track": args[4] if len(args) > 4 else TRACK_PLAYBOOK,
            "state": "DRAFT", "success_count": 1, "fail_count": 0,
        }

    async def execute(self, sql: str, *args: Any) -> None:
        self.queries.append((sql, args))


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


def _repo(rows: list[dict[str, Any]]):
    from services.admin_config.repo import AdminConfigRepo

    conn = _FakeConn(rows)
    return AdminConfigRepo(_FakePool(conn)), conn


_ROWS = [
    # advisory: người vận hành đồng ý với một CHẨN ĐOÁN (không phải uỷ quyền thực thi)
    {"tenant_id": "default", "domain": "unknown", "track": TRACK_ADVISORY,
     "playbook_id": "pat-1", "state": "GRADUATED", "success_count": 3, "fail_count": 0},
    {"tenant_id": "default", "domain": "unknown", "track": TRACK_ADVISORY,
     "playbook_id": "pat-2", "state": "GRADUATED", "success_count": 5, "fail_count": 1},
    # playbook: mutation đã chạy và được hậu kiểm
    {"tenant_id": "default", "domain": "kubernetes", "track": TRACK_PLAYBOOK,
     "playbook_id": "PB-K8S-CPU-RESTART", "state": "GRADUATED",
     "success_count": 4, "fail_count": 0},
]


async def test_list_graduations_separates_advisory_from_playbook() -> None:
    repo, _ = _repo(_ROWS)

    advisory = await repo.list_playbook_graduations("default", track=TRACK_ADVISORY)
    playbook = await repo.list_playbook_graduations("default", track=TRACK_PLAYBOOK)
    mixed = await repo.list_playbook_graduations("default")

    assert [r["playbook_id"] for r in advisory] == ["pat-1", "pat-2"]
    assert [r["playbook_id"] for r in playbook] == ["PB-K8S-CPU-RESTART"]
    # Đây chính là con số cũ: 3 = 2 advisory + 1 playbook, đếm gộp.
    assert len(mixed) == 3


async def test_tier_gate_counts_only_playbook_track() -> None:
    """Hạn mức nâng tier chỉ được đếm track=playbook.

    Nếu đếm gộp, tenant này có 3 "quy trình tốt nghiệp" trong khi thực tế chỉ có 1 quy
    trình từng chạy và được verify — hai cái còn lại chỉ là người đồng ý với chẩn đoán.
    """
    repo, _ = _repo(_ROWS)
    grads = await repo.list_playbook_graduations(
        "default", state="GRADUATED", track=TRACK_PLAYBOOK
    )
    assert len(grads) == 1


async def test_bump_graduation_writes_canonical_domain_and_track() -> None:
    repo, conn = _repo([])
    row = await repo.bump_playbook_graduation(
        tenant_id="default", domain="k8s", playbook_id="PB-1", success=True,
    )
    sql, args = conn.queries[-1]
    assert "ON CONFLICT (tenant_id, track, domain, playbook_id)" in sql
    assert args[1] == "kubernetes"      # ghi canonical, không phải 'k8s'
    assert args[4] == TRACK_PLAYBOOK
    assert row["domain"] == "kubernetes"


async def test_bump_graduation_rejects_bogus_domain_and_track() -> None:
    repo, _ = _repo([])
    with pytest.raises(ValueError):
        await repo.bump_playbook_graduation(
            tenant_id="default", domain="bogus", playbook_id="PB-1", success=True,
        )
    with pytest.raises(ValueError):
        await repo.bump_playbook_graduation(
            tenant_id="default", domain="kubernetes", playbook_id="PB-1",
            success=True, track="advisories",
        )


async def test_advisory_promoter_writes_advisory_track_not_domain() -> None:
    """Regression đúng cái bug: `advisory` phải vào cột track, domain là 'unknown'."""
    from services.learning_promoter import advisory_promoter as ap

    seen: dict[str, Any] = {}

    class _Repo:
        async def bump_playbook_graduation(self, **kw):
            seen.update(kw)
            return {"success_count": 1, "fail_count": 0, "state": "DRAFT"}

        async def set_playbook_graduation_state(self, **kw):
            return None

    ctx = type("C", (), {
        "admin_repo": _Repo(),
        "settings": type("S", (), {
            "omni_advisory_graduation_min_success": 3,
            "omni_advisory_graduation_max_fail_rate": 0.25,
        })(),
    })()

    await ap.record_advisory_verdict(
        ctx, tenant_id="default", trace_id="t1", accepted=True,
        advisory={"lane": "SYS_RESOURCE", "alertname": "PodOOMKilled"},
    )
    assert seen["track"] == TRACK_ADVISORY
    assert seen["domain"] == UNKNOWN
    assert seen["domain"] not in ALL_TRACKS


# ---------------------------------------------------------------------------
# Redis: đổi hình dạng key KHÔNG được làm mất graduation đang sống
# ---------------------------------------------------------------------------

async def test_governor_migrates_legacy_redis_key_shape() -> None:
    """Lab thật có `omni:playbook:grad:default:k8s:PB-K8S-CPU-RESTART`.

    Nếu không di chuyển, playbook đã GRADUATED tụt về chưa-seed và ngừng auto-execute —
    một suy giảm năng lực im lặng, không có lỗi nào bật ra.
    """
    from workers.playbook_governor import PlaybookGovernor

    r = FakeRedis(decode_responses=True)
    legacy = "omni:playbook:grad:default:k8s:PB-K8S-CPU-RESTART"
    await r.hset(legacy, mapping={"state": "GRADUATED", "success_count": "4",
                                  "fail_count": "0"})

    gov = PlaybookGovernor(r)
    state = await gov.get_state("default", "kubernetes", "PB-K8S-CPU-RESTART")

    assert state == "GRADUATED"
    new_key = "omni:playbook:grad:default:playbook:kubernetes:PB-K8S-CPU-RESTART"
    assert await r.hget(new_key, "success_count") == "4"
    # Key cũ CỐ Ý còn lại: rollback image về bản trước vẫn đọc được state của nó.
    assert await r.exists(legacy)


async def test_governor_does_not_cross_contaminate_other_domain() -> None:
    """Key cũ của domain khác không được bị hút sang."""
    from workers.playbook_governor import PlaybookGovernor

    r = FakeRedis(decode_responses=True)
    await r.hset("omni:playbook:grad:default:os:PB-1", mapping={"state": "GRADUATED"})

    gov = PlaybookGovernor(r)
    assert await gov.get_state("default", "kubernetes", "PB-1") == ""
    assert await gov.get_state("default", "os_host", "PB-1") == "GRADUATED"


async def test_governor_seed_uses_canonical_key_for_legacy_domain_arg() -> None:
    """Caller truyền tên cũ vẫn ghi vào key canonical — không sinh key thứ hai."""
    from workers.playbook_governor import PlaybookGovernor

    r = FakeRedis(decode_responses=True)
    gov = PlaybookGovernor(r)
    await gov.ensure_seeded("default", "k8s", "PB-2", "CANDIDATE")

    assert await r.exists("omni:playbook:grad:default:playbook:kubernetes:PB-2")
    keys = sorted(await r.keys("omni:playbook:grad:*"))
    assert keys == ["omni:playbook:grad:default:playbook:kubernetes:PB-2"]
