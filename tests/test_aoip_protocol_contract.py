"""Contract tests cho aoip.protocol (ADR-002) — chặn drift giữa 3 nơi dùng state machine.

Ba bản chép của vocabulary trước đây: gateway/routes/agent_runtime.py, aoip/agent/delivery.py,
và bảng TERMINAL hardcode TRONG Lua script. Hai bản Python nay import chung aoip.protocol;
Lua không import được nên test này parse Lua source và đối chiếu.
"""
from __future__ import annotations

import re

from aoip import protocol
from aoip.agent import delivery
from gateway.routes import agent_runtime


class TestSingleSourceOfTruth:
    def test_gateway_terminal_is_protocol_terminal(self):
        assert agent_runtime.TERMINAL is protocol.TERMINAL_STATES

    def test_gateway_progress_is_protocol_progress(self):
        assert agent_runtime._PROGRESS is protocol.PROGRESS_STATES

    def test_delivery_reexports_protocol_terminal(self):
        assert delivery.TERMINAL_STATES is protocol.TERMINAL_STATES

    def test_lua_terminal_table_matches_protocol(self):
        # _CLAIM_SCRIPT: local TERMINAL = {COMPLETED=true, FAILED=true, ...}
        m = re.search(r"local TERMINAL = \{([^}]*)\}", agent_runtime._CLAIM_SCRIPT)
        assert m, "Lua TERMINAL table không tìm thấy trong _CLAIM_SCRIPT"
        lua_states = set(re.findall(r"(\w+)=true", m.group(1)))
        assert lua_states == set(protocol.TERMINAL_STATES)

    def test_lua_claimable_states_exist_in_protocol(self):
        # Lua chỉ tham chiếu state literal QUEUED/DELIVERED/EXPIRED ngoài bảng TERMINAL
        for literal in ("'QUEUED'", "'DELIVERED'", "'EXPIRED'"):
            assert literal in agent_runtime._CLAIM_SCRIPT
        assert {"QUEUED", "DELIVERED", "EXPIRED"} <= protocol.ALL_STATES


class TestTransitionInvariants:
    def test_terminal_is_absorbing(self):
        for t in protocol.TERMINAL_STATES:
            for target in protocol.ALL_STATES:
                assert not protocol.is_legal_transition(t, target)

    def test_any_nonterminal_can_reach_terminal(self):
        nonterminal = protocol.ALL_STATES - protocol.TERMINAL_STATES
        for s in nonterminal:
            for t in protocol.TERMINAL_STATES:
                assert protocol.is_legal_transition(s, t)

    def test_redelivery_always_legal_from_nonterminal(self):
        nonterminal = protocol.ALL_STATES - protocol.TERMINAL_STATES
        for s in nonterminal:
            assert protocol.is_legal_transition(s, protocol.ST_DELIVERED)

    def test_no_backward_progress_except_redelivery(self):
        assert not protocol.is_legal_transition(protocol.ST_RUNNING, protocol.ST_ACCEPTED)
        assert not protocol.is_legal_transition(protocol.ST_RECONCILING, protocol.ST_RUNNING)
        assert not protocol.is_legal_transition(protocol.ST_ACCEPTED, protocol.ST_QUEUED)

    def test_unknown_state_rejected(self):
        assert not protocol.is_legal_transition("CANCELLED", protocol.ST_COMPLETED)
        assert not protocol.is_legal_transition(protocol.ST_QUEUED, "CANCELLED")

    def test_progress_states_subset_and_order_consistent(self):
        assert protocol.PROGRESS_STATES <= protocol.ALL_STATES - protocol.TERMINAL_STATES
        assert set(protocol.PROGRESS_ORDER) == protocol.ALL_STATES - protocol.TERMINAL_STATES
