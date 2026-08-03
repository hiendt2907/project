#!/usr/bin/env python3
"""Di trú khoá KPI Redis từ lane (trục A) sang domain.

    omni:kpi:detected:{tenant}:{LANE}  →  omni:kpi:detected:{tenant}:{domain}
    omni:kpi:resolved:{tenant}:{LANE}  →  omni:kpi:resolved:{tenant}:{domain}

Phase 3 bước 2 của `plans/lane-to-domain-and-omni-decides-2026-07-30.md`.
Thứ tự chạy / kiểm sau / rollback: `docs/runbooks/lane-to-domain-migration.md`.
Tiền lệ: `scripts/kpi_key_migrate.py` (di trú per-tenant, cùng kiểu ZUNIONSTORE).

MẶC ĐỊNH LÀ DRY-RUN. Phải có `--apply` mới ghi. Lý do không phải khách sáo: đường
ghi (`kpi_metrics.record_detected`) ĐÃ chuẩn hoá sang domain, nên khoá lane còn lại
là dữ liệu lịch sử — gộp sai thì không có nguồn nào tính lại được.

`SYS_HARD_FAIL` và `ONBOARDING_DISCOVERY` gộp vào `unknown` và báo cáo in rõ. KHÔNG
phân bổ đoán sang database/storage/service: `SYS_HARD_FAIL` gánh cả bốn domain đó,
domain thật chỉ collector phát ra mới biết. Bịa ra một tỉ lệ phân bổ để bảng KPI
trông đủ là bùa số — con số đẹp hơn mà không có gì đứng sau.

    .venv/bin/python scripts/kpi_lane_to_domain_migrate.py                 # dry-run
    .venv/bin/python scripts/kpi_lane_to_domain_migrate.py --apply
    .venv/bin/python scripts/kpi_lane_to_domain_migrate.py --url redis://localhost:16379/0
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import redis.asyncio as aioredis  # noqa: E402

from pkg.domain.taxonomy import LANE_TO_DOMAIN, UNKNOWN  # noqa: E402

# Chỉ hai họ khoá này nhúng lane. `omni:kpi:z:{tenant}:{outcome}` không nhúng lane —
# đừng "dọn cho đối xứng".
PREFIXES = ("omni:kpi:detected", "omni:kpi:resolved")

# Lane nào gộp về `unknown` — dùng để in cảnh báo, không để phân bổ.
LOSSY_LANES = tuple(k for k, v in LANE_TO_DOMAIN.items() if v == UNKNOWN)


def parse_key(key: str) -> tuple[str, str, str] | None:
    """``omni:kpi:detected:acme:SYS_RESOURCE`` → ``(prefix, tenant, lane)``.

    Trả None nếu khoá không đúng khuôn: tenant_id có thể chứa dấu ``:``? Không —
    nhưng nếu một ngày nó chứa, thà bỏ qua và báo cáo còn hơn cắt sai rồi ghi đè.
    """
    for prefix in PREFIXES:
        if not key.startswith(prefix + ":"):
            continue
        rest = key[len(prefix) + 1:]
        parts = rest.split(":")
        if len(parts) != 2 or not all(parts):
            return None
        return prefix, parts[0], parts[1]
    return None


def target_domain(lane: str) -> str | None:
    """Domain đích, hoặc None nếu khoá đã là domain (không cần đụng).

    Chỉ nhận lane trục A. Giá trị lạ → None: không đoán, và migration không phải
    chỗ hợp lệ để phát minh ra một domain mới.
    """
    v = lane.strip().lower().replace("-", "_")
    return LANE_TO_DOMAIN.get(v)


async def migrate(url: str, *, apply: bool) -> int:
    """Trả số khoá đã (hoặc sẽ) di trú."""
    r = aioredis.from_url(url, decode_responses=True)
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[{mode}] KPI lane→domain trên {url}")
    moved = 0
    skipped = 0
    lossy: list[tuple[str, int]] = []
    try:
        for prefix in PREFIXES:
            # scan_iter: không KEYS, không block Redis. Redis lab dùng chung với
            # RAG HNSW và audit chain — một lệnh block là chặn cả pipeline.
            async for key in r.scan_iter(f"{prefix}:*", count=100):
                parsed = parse_key(key)
                if parsed is None:
                    print(f"  BỎ QUA  {key} (khuôn khoá không nhận ra)")
                    skipped += 1
                    continue
                _, tenant, lane = parsed
                domain = target_domain(lane)
                if domain is None:
                    # Đã là domain canonical (đường ghi hiện tại) — không đụng.
                    continue
                new_key = f"{prefix}:{tenant}:{domain}"
                if new_key == key:
                    continue
                members = int(await r.zcard(key) or 0)
                note = ""
                if lane.strip().lower().replace("-", "_") in LOSSY_LANES:
                    note = f"  ⚠️ GỘP VÀO '{UNKNOWN}' (mất thông tin domain, cố ý không đoán)"
                    lossy.append((key, members))
                print(f"  {key} → {new_key}  members={members}{note}")
                moved += 1
                if not apply:
                    continue
                # ZUNIONSTORE vào đích: cộng dồn, an toàn khi đích đã có dữ liệu và
                # khi script chạy lại. Score là timestamp nên trùng member giữ điểm
                # lớn hơn — không sai lệch cửa sổ 24h.
                await r.zunionstore(new_key, [new_key, key], aggregate="MAX")
                old_ttl = await r.ttl(key)
                new_ttl = await r.ttl(new_key)
                if new_ttl == -1 and old_ttl and old_ttl > 0:
                    await r.expire(new_key, old_ttl)
                # Xoá khoá cũ SAU khi gộp: nếu để lại, `get_summary()` quét
                # `omni:kpi:detected:*` sẽ đếm hai lần cùng một sự cố.
                await r.delete(key)
    finally:
        await r.aclose()

    print()
    print(f"  khoá di trú: {moved}   bỏ qua: {skipped}")
    if lossy:
        total = sum(n for _, n in lossy)
        print(f"  ⚠️ {len(lossy)} khoá ({total} bản ghi) gộp vào '{UNKNOWN}':")
        for k, n in lossy:
            print(f"       {k}  members={n}")
        print("     Không phân bổ sang database/storage/service — domain thật chỉ")
        print("     collector phát ra mới biết. Số liệu lịch sử của các lane này")
        print("     nằm chung một rổ và KHÔNG tách lại được.")
    if not apply:
        print("\n  DRY-RUN: chưa ghi gì. Thêm --apply để thực hiện.")
    return moved


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--url",
        default=os.getenv("OMNI_REDIS_URL", "redis://localhost:16379/0"),
        help="Redis URL (mặc định $OMNI_REDIS_URL)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="GHI THẬT. Không có cờ này thì chỉ in kế hoạch.",
    )
    args = ap.parse_args()
    asyncio.run(migrate(args.url, apply=args.apply))


if __name__ == "__main__":
    main()
