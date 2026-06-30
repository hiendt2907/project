"""Capability: Map System Graph — Slice 3 (Fact → topology/dependency graph).

Vì sao tồn tại: AI bắt đầu HIỂU HỆ THỐNG, không chỉ liệt kê service. Từ discovery
nó dựng đồ thị quan hệ (nginx proxies_to payment-api; payment depends_on redis),
và tự đánh dấu Unknown Edge — service được nhắc nhưng chưa quan sát được — làm hạt
giống cho câu hỏi kiến trúc (Interview Feedback Loop ở slice sau).

Object kiến trúc hiện thực: Observation → Fact (thuộc tính + QUAN HỆ) → SystemModel
(graph view derived) → CapabilityState chiều K. KHÔNG noun mới: edge = Fact quan
hệ; graph = view trên Fact. Luật: INV_NO_NEW_NOUNS, INV_MINIMAL_PRIMITIVES,
INV_NO_DATA_EXFIL (builder nhận hint metadata, không đọc nội dung), INV_HUMAN_
ACCOUNTABILITY (Unknown Edge → câu hỏi, never assume).

Runtime capability mở khóa: Discovery → System Graph → Understanding — bước phân
biệt "discovery tool" với "Senior hiểu kiến trúc".
"""
from __future__ import annotations

from aoip.algebra import Sequence
from aoip.understanding import (
    assess_graph,
    hypothesize_services,
    infer_topology,
    model_host,
    observe_host,
    verify_services,
)

map_system_graph = Sequence(
    observe_host,          # Observe: discover inventory + topology hints
    hypothesize_services,  # mỗi service → tuyên bố cổng
    verify_services,       # probe thật → Fact (node service quan sát được)
    infer_topology,        # suy ra edge quan hệ (proxies_to/depends_on/connects_to)
    model_host,            # fold node + edge → SystemModel (graph)
    assess_graph,          # Assess: độ phân giải graph (unknown edge) → K↑
)
