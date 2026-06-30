"""Knowledge persistence — câu trả lời người thành Fact BỀN, tái dùng mãi.

Vì sao tồn tại: KPI sống còn của AOIP. Một agent thật phải HỌC: hỏi người MỘT lần,
rồi không bao giờ hỏi lại — kể cả sau khi tiến trình restart hay agent được cài lại.
Knowledge phải tồn tại NGOÀI vòng đời tiến trình.

KHÔNG noun/model mới: lưu chính ``Fact`` (đã có provenance + confidence + bitemporal)
ra đĩa, nạp lại, fold vào ``SystemModel``. Câu hỏi = ``Communication`` đã có.

Residency: file đặt phía tenant/host khách (INV_DATA_RESIDENCY) — giá trị nhạy cảm
ở phía khách, không bắt buộc đẩy lên Omni. Backend Redis/PG của Omni thay thế khi
deploy thật yêu cầu (cùng interface, runtime ép — không suy diễn trước).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from aoip.objects import Fact
from aoip.system_model import SystemModel


def _fact_to_dict(f: Fact) -> dict:
    return {
        "subject": f.subject, "predicate": f.predicate, "obj": f.obj,
        "confidence": f.confidence, "provenance": list(f.provenance),
        "observation_time": f.observation_time, "verified_time": f.verified_time,
    }


def _fact_from_dict(d: dict) -> Fact:
    return Fact(
        subject=d["subject"], predicate=d["predicate"], obj=d["obj"],
        confidence=d.get("confidence", 0.9), provenance=tuple(d.get("provenance", ())),
        observation_time=d.get("observation_time", 0.0),
        verified_time=d.get("verified_time", 0.0),
    )


class FileKnowledgeStore:
    """Tri thức bền theo (tenant, scope) lưu JSON trên đĩa — sống sót restart.

    Atomic write (tmp + replace) để không hỏng file khi crash giữa chừng.
    """

    def __init__(self, path: str | os.PathLike) -> None:
        self._path = Path(path)

    def _read_all(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_all(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self._path)

    @staticmethod
    def _key(tenant: str, scope: str) -> str:
        return f"{tenant}::{scope}"

    def save_facts(self, tenant: str, scope: str, facts: list[Fact]) -> None:
        """Thêm Fact mới; supersede theo triple (giữ bản verify mới nhất)."""
        data = self._read_all()
        bucket = {tuple(d["subject"] + "|" + d["predicate"] + "|" + d["obj"] for d in [x])[0]: x
                  for x in data.get(self._key(tenant, scope), [])}
        for f in facts:
            tkey = f"{f.subject}|{f.predicate}|{f.obj}"
            bucket[tkey] = _fact_to_dict(f)
        data[self._key(tenant, scope)] = list(bucket.values())
        self._write_all(data)

    def load_facts(self, tenant: str, scope: str) -> list[Fact]:
        data = self._read_all()
        return [_fact_from_dict(d) for d in data.get(self._key(tenant, scope), [])]


def answer_question(communication, answer: str) -> Fact:
    """Biến câu trả lời của người thành Fact BỀN cho node bị chặn.

    Node = ``communication.blocking_unknown`` (đã là định danh node với câu hỏi tầng
    kiến trúc). Provenance = human → Evidence Graph biết tri thức này do người xác
    nhận. Đây là điểm "học": INV_VERIFY_BEFORE_BELIEVE thỏa vì người là nguồn xác
    minh độc lập, confidence cao.
    """
    node = communication.blocking_unknown
    return Fact(
        subject=node if ":" in node else f"unknown:{node}",
        predicate="resolved_as",
        obj=answer,
        confidence=0.97,
        provenance=("human:interview",),
    )


def seed_model(model: SystemModel, facts: list[Fact]) -> SystemModel:
    """Fold tri thức đã biết (bền) vào model TRƯỚC khi chạy mission.

    Node được người xác nhận trở thành known_node → rời tập Unknown → mission KHÔNG
    hỏi lại. Đây là "Mission Resume sau khi học". Bất biến: trả model mới.
    """
    return model.fold(*facts) if facts else model
