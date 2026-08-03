"""Omni tự xin quyền — các phanh phải giữ được dưới áp lực.

Thiết kế: `plans/case-ledger-design-2026-07-30.md`, mục "Xin quyền" + "Cận dưới".

Ba tính chất được tấn công trực tiếp ở đây, không phải kiểm tra rằng hàm chạy:
1. Hồ sơ đẹp tuyệt đối nhưng ít mẫu (3/3) **không** mở được quyền.
2. Bị từ chối rồi thì trong thời gian khoá **không** xin lại được.
3. ``frozen`` chặn xin quyền, và không có đường nào trong code tự gỡ nó.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone

import pytest

from services.case_ledger.advocacy import (
    SKIP_COOLDOWN,
    SKIP_FROZEN,
    SKIP_NOT_ELIGIBLE,
    SKIP_PENDING,
    ScopeAdvocate,
    approve_request,
    reject_request,
)
from services.case_ledger.store import CaseLedgerStore
from services.case_ledger.store_scope import ScopeStore

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


# ── Pool asyncpg giả — đủ để chạy đúng ngữ nghĩa các câu lệnh module dùng ────
# Cố ý bám theo SQL thật (kể cả mệnh đề WHERE tenant_id) thay vì mock trả sẵn:
# nếu ai bỏ mất `AND tenant_id=$2` trong câu UPDATE thì test cách ly tenant phải
# đỏ, mà một mock trả sẵn thì không bao giờ đỏ.
class FakeConn:
    def __init__(self, db: "FakeDB") -> None:
        self.db = db

    @contextlib.asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, sql: str, *args):
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetch(self, sql: str, *args):  # noqa: C901 — dispatcher, phẳng là dễ đọc nhất
        s = " ".join(sql.split())

        if "DISTINCT pattern_key" in s:
            seen = {c["pattern_key"] for c in self.db.cases if c["tenant_id"] == args[0]}
            return [{"pattern_key": p} for p in sorted(seen)]

        # Cửa sổ chuyển tiếp lane→domain (migration 0014): đường đọc khớp CẢ
        # `pattern_key` VÀ `pattern_key_domain`. Fake bám theo vế WHERE thật để nếu
        # ai đó bỏ mất vế thứ hai thì test dual-key phải đỏ.
        if "FROM omni_admin.case_ledger" in s and "pattern_key=$2" in s:
            dual = "pattern_key_domain=$2" in s
            return [
                c for c in self.db.cases
                if c["tenant_id"] == args[0] and (
                    c["pattern_key"] == args[1]
                    or (dual and c.get("pattern_key_domain") == args[1])
                )
            ]

        # ── scope_grant ──
        if s.startswith("SELECT * FROM omni_admin.scope_grant") and "pattern_key=$2" in s:
            dual = "pattern_key_legacy=$2" in s
            g = self.db.grants.get((args[0], args[1]))
            if g is None and dual:
                g = next(
                    (x for k, x in sorted(self.db.grants.items())
                     if k[0] == args[0] and x.get("pattern_key_legacy") == args[1]),
                    None,
                )
            return [dict(g)] if g else []
        if s.startswith("SELECT * FROM omni_admin.scope_grant"):
            return [dict(g) for k, g in sorted(self.db.grants.items()) if k[0] == args[0]]
        if s.startswith("INSERT INTO omni_admin.scope_grant") and "frozen" in s and "TRUE" in s:
            key = (args[0], args[1])
            g = self.db.grants.setdefault(
                key,
                {"tenant_id": args[0], "pattern_key": args[1],
                 "granted_scope": "SUGGEST_ONLY", "granted_by": "", "frozen": False,
                 "frozen_reason": None},
            )
            g.update(frozen=True, frozen_reason=args[2])
            return [dict(g)]
        if s.startswith("INSERT INTO omni_admin.scope_grant"):
            key = (args[0], args[1])
            g = self.db.grants.get(key)
            if g is not None and g.get("frozen"):
                return []  # WHERE NOT frozen — không nâng quyền cho pattern bị đóng băng
            g = self.db.grants.setdefault(
                key,
                {"tenant_id": args[0], "pattern_key": args[1], "frozen": False,
                 "frozen_reason": None},
            )
            g.update(granted_scope=args[2], granted_by=args[3], granted_at=NOW())
            return [dict(g)]

        # ── scope_request ──
        if "FROM omni_admin.scope_request" in s and "state='PENDING'" in s:
            return [dict(r) for r in self.db.requests
                    if r["tenant_id"] == args[0] and r["pattern_key"] == args[1]
                    and r["state"] == "PENDING"]
        if "FROM omni_admin.scope_request" in s and "cooldown_until > now()" in s:
            return [dict(r) for r in self.db.requests
                    if r["tenant_id"] == args[0] and r["pattern_key"] == args[1]
                    and r.get("cooldown_until") and r["cooldown_until"] > NOW()]
        if s.startswith("INSERT INTO omni_admin.scope_request"):
            self.db.seq += 1
            row = {"id": self.db.seq, "tenant_id": args[0], "pattern_key": args[1],
                   "requested_scope": args[2], "evidence": args[3], "state": "PENDING",
                   "decided_by": None, "decided_at": None, "decision_note": None,
                   "cooldown_until": None, "crat_ref": args[4], "created_at": NOW()}
            self.db.requests.append(row)
            return [dict(row)]
        if s.startswith("SELECT * FROM omni_admin.scope_request"):
            out = [dict(r) for r in reversed(self.db.requests)
                   if r["tenant_id"] == args[0] and (args[1] is None or r["state"] == args[1])]
            return out[: args[2]]
        if s.startswith("UPDATE omni_admin.scope_request"):
            rid, tenant, decision, actor, note, cooldown = args
            for r in self.db.requests:
                if r["id"] == rid and r["tenant_id"] == tenant and r["state"] == "PENDING":
                    r.update(state=decision, decided_by=actor, decided_at=NOW(),
                             decision_note=note)
                    if decision == "REJECTED":
                        r["cooldown_until"] = NOW() + timedelta(days=cooldown)
                    return [dict(r)]
            return []

        raise AssertionError(f"SQL khong duoc fake ho tro: {s[:120]}")


class FakeDB:
    def __init__(self) -> None:
        self.cases: list[dict] = []
        self.requests: list[dict] = []
        self.grants: dict[tuple[str, str], dict] = {}
        self.seq = 0

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield FakeConn(self)

    def add_cases(self, tenant, pattern, *, correct=0, incorrect=0, refused=0):
        for kind, n in (("CORRECT", correct), ("INCORRECT", incorrect)):
            for _ in range(n):
                self.cases.append({
                    "tenant_id": tenant, "pattern_key": pattern, "posture": "DIAGNOSED",
                    "diagnosis_verdict": kind, "recurred": False,
                })
        for _ in range(refused):
            self.cases.append({
                "tenant_id": tenant, "pattern_key": pattern, "posture": "REFUSED",
                "diagnosis_verdict": "UNJUDGED", "recurred": False,
            })


def _advocate(db: FakeDB) -> ScopeAdvocate:
    return ScopeAdvocate(CaseLedgerStore(db), ScopeStore(db))


# ── (a) Cận dưới Wilson: 3/3 hoàn hảo vẫn không đủ ───────────────────────────


async def test_three_of_three_perfect_is_not_enough_to_request():
    """3/3 đúng = 100% thô, nhưng cận dưới Wilson chỉ ~0.29 → không xin được.

    Đây là lý do tồn tại của cận dưới: nếu dùng tỉ lệ thô thì ba ca may mắn đủ để
    mở quyền tự thực thi, và không có ngưỡng ``n`` nào không bị tranh cãi hoặc nới
    dần theo thời gian.
    """
    db = FakeDB()
    db.add_cases("acme", "pod_oom", correct=3)

    outcomes = await _advocate(db).run(tenant_id="acme")

    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.requested is False
    assert o.skip_reason == SKIP_NOT_ELIGIBLE
    assert o.report.accuracy_raw == 1.0, "tỉ lệ thô vẫn là 100% — đúng như kỳ vọng"
    assert o.report.accuracy_lower_bound < 0.70
    assert db.requests == [], "đã nộp đơn dù chưa đủ bằng chứng"


async def test_large_clean_sample_does_request_and_freezes_evidence():
    """Mẫu lớn và sạch thì xin được — và evidence phải là số liệu, không phải lời kể."""
    db = FakeDB()
    db.add_cases("acme", "pod_oom", correct=40, incorrect=2)

    outcomes = await _advocate(db).run(tenant_id="acme")

    assert outcomes[0].requested is True
    # Xin BẬC KẾ TIẾP, không nhảy thẳng lên quyền cao nhất.
    assert outcomes[0].requested_scope == "HITL_REQUIRED"
    ev = db.requests[0]["evidence"]
    import json

    ev = json.loads(ev) if isinstance(ev, str) else ev
    assert ev["correct"] == 40 and ev["incorrect"] == 2
    assert ev["pattern_key"] == "pod_oom"
    # Không có trường tự do nào cho văn kể chuyện chui vào bằng chứng.
    assert set(ev) == {
        "pattern_key", "tenant_id", "total_cases", "diagnosed", "refused",
        "out_of_scope", "correct", "incorrect", "partial", "unjudged",
        "accuracy_lower_bound", "accuracy_raw", "coverage", "unjudged_ratio",
        "recurrence_rate", "eligible", "blockers",
    }


async def test_pending_request_is_not_duplicated():
    db = FakeDB()
    db.add_cases("acme", "pod_oom", correct=40, incorrect=2)
    adv = _advocate(db)

    await adv.run(tenant_id="acme")
    second = await adv.run(tenant_id="acme")

    assert second[0].requested is False
    assert second[0].skip_reason == SKIP_PENDING
    assert len(db.requests) == 1


# ── (b) Cooldown chặn xin lại ────────────────────────────────────────────────


async def test_rejection_blocks_re_request_during_cooldown():
    """Bị từ chối → khoá. Không có phanh này thì chiến lược tối ưu là xin liên tục
    tới lúc admin mệt mà bấm duyệt — lỗ hổng con người, không phải lỗ hổng kỹ thuật."""
    db = FakeDB()
    db.add_cases("acme", "pod_oom", correct=40, incorrect=2)
    adv = _advocate(db)
    scope = ScopeStore(db)

    first = await adv.run(tenant_id="acme")
    rid = first[0].request_id
    rejected = await reject_request(
        scope, request_id=rid, tenant_id="acme", actor="sre@acme", note="chưa tin"
    )
    assert rejected["state"] == "REJECTED"
    assert rejected["cooldown_until"] > NOW()

    again = await adv.run(tenant_id="acme")
    assert again[0].requested is False
    assert again[0].skip_reason == SKIP_COOLDOWN
    assert len(db.requests) == 1, "xin lại được ngay sau khi bị từ chối"


async def test_request_allowed_again_after_cooldown_expires():
    """Khoá là tạm thời, không phải án chung thân — hết hạn thì xin lại được."""
    db = FakeDB()
    db.add_cases("acme", "pod_oom", correct=40, incorrect=2)
    adv = _advocate(db)
    first = await adv.run(tenant_id="acme")
    await reject_request(
        ScopeStore(db), request_id=first[0].request_id, tenant_id="acme", actor="sre"
    )
    db.requests[0]["cooldown_until"] = NOW() - timedelta(days=1)  # hết hạn

    again = await adv.run(tenant_id="acme")
    assert again[0].requested is True
    assert len(db.requests) == 2


async def test_approval_writes_grant_and_next_round_climbs_one_step():
    db = FakeDB()
    db.add_cases("acme", "pod_oom", correct=40, incorrect=2)
    adv = _advocate(db)
    scope = ScopeStore(db)

    first = await adv.run(tenant_id="acme")
    out = await approve_request(
        scope, request_id=first[0].request_id, tenant_id="acme", actor="cto@acme"
    )
    assert out["state"] == "APPROVED"
    assert out["grant"]["granted_scope"] == "HITL_REQUIRED"

    second = await adv.run(tenant_id="acme")
    assert second[0].requested_scope == "AUTO_EXECUTE"


# ── frozen: bất đối xứng có chủ đích ─────────────────────────────────────────


async def test_frozen_pattern_cannot_be_requested_and_has_no_self_unfreeze():
    """Omni tự lên bậc được, không tự gỡ án được."""
    db = FakeDB()
    db.add_cases("acme", "pod_oom", correct=40, incorrect=2)
    scope = ScopeStore(db)
    await scope.freeze_grant(
        tenant_id="acme", pattern_key="pod_oom", reason="xoá nhầm dữ liệu"
    )

    outcomes = await _advocate(db).run(tenant_id="acme")

    assert outcomes[0].requested is False
    assert outcomes[0].skip_reason == SKIP_FROZEN
    assert db.requests == []

    import services.case_ledger.advocacy as advocacy_mod
    import services.case_ledger.store_scope as store_scope_mod

    for mod in (advocacy_mod, store_scope_mod):
        src = open(mod.__file__, encoding="utf-8").read()
        normalised = src.replace(" ", "").upper()
        assert "FROZEN=FALSE" not in normalised, f"{mod.__name__} có đường tự gỡ frozen"


async def test_approving_a_frozen_pattern_still_grants_nothing():
    """Duyệt nhầm cũng không cấp được quyền — ``WHERE NOT frozen`` nằm trong SQL."""
    db = FakeDB()
    db.add_cases("acme", "pod_oom", correct=40, incorrect=2)
    adv = _advocate(db)
    scope = ScopeStore(db)
    first = await adv.run(tenant_id="acme")

    await scope.freeze_grant(tenant_id="acme", pattern_key="pod_oom", reason="điều tra")
    out = await approve_request(
        scope, request_id=first[0].request_id, tenant_id="acme", actor="admin"
    )

    assert out["grant"] == {}
    assert db.grants[("acme", "pod_oom")]["granted_scope"] == "SUGGEST_ONLY"


async def test_decide_is_scoped_by_tenant_in_the_where_clause():
    """``request_id`` đoán được cũng không chạm sang tenant khác."""
    db = FakeDB()
    db.add_cases("acme", "pod_oom", correct=40, incorrect=2)
    first = await _advocate(db).run(tenant_id="acme")

    stolen = await reject_request(
        ScopeStore(db), request_id=first[0].request_id, tenant_id="globex", actor="kẻ tấn công"
    )

    assert stolen is None
    assert db.requests[0]["state"] == "PENDING"


async def test_refusing_hard_cases_does_not_buy_eligibility():
    """Từ chối mọi ca khó để giữ hồ sơ 100% thì độ phủ sập — hai số kéo ngược nhau."""
    db = FakeDB()
    db.add_cases("acme", "disk_full", correct=40, incorrect=2, refused=200)

    outcomes = await _advocate(db).run(tenant_id="acme")

    assert outcomes[0].requested is False
    assert any("độ phủ" in b for b in outcomes[0].report.blockers)


@pytest.mark.parametrize("bad", ["MAYBE", "approved", ""])
async def test_invalid_decision_is_rejected_by_the_store(bad):
    with pytest.raises(ValueError):
        await ScopeStore(FakeDB()).decide_request(
            request_id=1, tenant_id="acme", decision=bad, actor="x"
        )
