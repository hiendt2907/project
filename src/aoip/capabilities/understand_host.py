"""Capability: Understand a Host — vertical slice thứ hai (Stage 1+3+4 roadmap).

Vì sao tồn tại: hiện thực Day-1 SRE ("Observe. Read. Map. Ask. Never assume").
Object kiến trúc hiện thực: Observation→Hypothesis→Fact (Cognitive), SystemModel
(Knowledge §5), Communication node (interview người), CapabilityState chiều K.
Luật tuân theo: INV_NO_NEW_NOUNS, INV_MINIMAL_PRIMITIVES (toàn composition qua
Behavior Algebra), INV_FAIL_CLOSED (bound), INV_HUMAN_ACCOUNTABILITY (interview),
INV_CAPABILITY_IS_PRODUCT. Runtime capability mở khóa: tự xây SystemModel của một
tenant từ quan sát + tự sinh câu hỏi cho vùng chưa biết.
"""
from __future__ import annotations

from aoip.algebra import Sequence
from aoip.understanding import (
    assess_understanding,
    hypothesize_services,
    interview,
    model_host,
    observe_host,
    verify_services,
)

# Toàn bộ là COMPOSITION (Sequence) trên verb đã có — KHÔNG primitive mới.
understand_host = Sequence(
    observe_host,          # Observe: discover inventory
    hypothesize_services,  # Hypothesize: mỗi service một tuyên bố cổng
    verify_services,       # Verify: probe thật → Fact
    model_host,            # Map: fold Fact vào SystemModel
    interview,             # Ask: Unknown → Communication (never assume)
    assess_understanding,  # Assess: đóng vòng → K↑
)
