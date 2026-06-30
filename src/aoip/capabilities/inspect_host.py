"""Capability: Inspect a Host — vòng REASONING của Senior SRE.

Vì sao tồn tại: nâng Remote Agent từ "collector" lên "người suy nghĩ khi khám
phá". Thay vì chỉ thu thập rồi nối, Agent hình thành KỲ VỌNG từ tri thức tiên
nghiệm, probe, so sánh thực-tế-vs-kỳ-vọng, kết luận bằng Finding, và CHỈ hỏi
người ở chỗ kỳ vọng hụt (never assume).

Object kiến trúc hiện thực: Observation → Hypothesis (Expectation) → Finding
(Compare) → Fact, Communication (interview), CapabilityState chiều K. KHÔNG noun
mới (INV_NO_NEW_NOUNS): Expectation = Hypothesis.predicted_evidence; Compare =
Finding. Luật: INV_MINIMAL_PRIMITIVES (toàn composition), INV_HUMAN_ACCOUNTABILITY,
INV_CAPABILITY_IS_PRODUCT.

Runtime capability mở khóa: Observe → Generate Expectation → Probe → Compare →
Finding — phân biệt "discovery tool" với "Senior đang onboard".
"""
from __future__ import annotations

from aoip.algebra import Sequence
from aoip.understanding import (
    assess_expectations,
    compare_expectations,
    expect_services,
    interview,
    model_host,
    observe_host,
)

inspect_host = Sequence(
    observe_host,          # Observe: discover inventory
    expect_services,       # Generate Expectation: tri thức tiên nghiệm → Hypothesis
    compare_expectations,  # Probe + Compare: thực-tế-vs-kỳ-vọng → Finding (+Fact / +Question)
    model_host,            # Map: fold Fact đã verify vào SystemModel
    interview,             # Ask: Unknown discovery → Communication
    assess_expectations,   # Assess: met/total → K↑
)
