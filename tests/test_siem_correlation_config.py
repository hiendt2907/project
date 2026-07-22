"""TDD — services.siem_correlation.config: mọi tham số qua _ENV_*/_DEFAULT_*
đọc os.environ tại call time (KHÔNG hardcode), mirror semantics Go setDefaults
(giá trị <=0 / parse lỗi → default).
"""

from __future__ import annotations

from services.siem_correlation import config as cfg


class TestIntFloatEnvFuncs:
    def test_defaults_match_brain_go_deployment(self):
        env: dict[str, str] = {}
        assert cfg.corr_window_seconds(env) == 600
        assert cfg.corr_threshold(env) == 3
        assert cfg.corr_dedup_seconds(env) == 900
        assert cfg.corr_min_entity_span(env) == 1
        assert cfg.corr_min_confidence(env) == 0.5
        assert cfg.corr_weight_entity(env) == 0.4
        assert cfg.corr_weight_sequence(env) == 0.35
        assert cfg.corr_weight_volume(env) == 0.25

    def test_env_overrides(self):
        env = {
            "OMNI_SIEM_CORR_WINDOW_SECONDS": "300",
            "OMNI_SIEM_CORR_THRESHOLD": "5",
            "OMNI_SIEM_CORR_DEDUP_SECONDS": "60",
            "OMNI_SIEM_CORR_MIN_ENTITY_SPAN": "2",
            "OMNI_SIEM_CORR_MIN_CONFIDENCE": "0.7",
            "OMNI_SIEM_CORR_WEIGHT_ENTITY": "0.5",
            "OMNI_SIEM_CORR_WEIGHT_SEQUENCE": "0.3",
            "OMNI_SIEM_CORR_WEIGHT_VOLUME": "0.2",
        }
        assert cfg.corr_window_seconds(env) == 300
        assert cfg.corr_threshold(env) == 5
        assert cfg.corr_dedup_seconds(env) == 60
        assert cfg.corr_min_entity_span(env) == 2
        assert cfg.corr_min_confidence(env) == 0.7
        assert cfg.corr_weight_entity(env) == 0.5
        assert cfg.corr_weight_sequence(env) == 0.3
        assert cfg.corr_weight_volume(env) == 0.2

    def test_garbage_and_nonpositive_fall_back(self):
        for name, func, default in [
            ("OMNI_SIEM_CORR_WINDOW_SECONDS", cfg.corr_window_seconds, 600),
            ("OMNI_SIEM_CORR_THRESHOLD", cfg.corr_threshold, 3),
            ("OMNI_SIEM_CORR_MIN_CONFIDENCE", cfg.corr_min_confidence, 0.5),
            ("OMNI_SIEM_CORR_WEIGHT_ENTITY", cfg.corr_weight_entity, 0.4),
        ]:
            assert func({name: "abc"}) == default
            assert func({name: "0"}) == default
            assert func({name: "-1"}) == default
            assert func({name: ""}) == default


class TestStringEnvFuncs:
    def test_topic_and_group_defaults(self):
        env: dict[str, str] = {}
        assert cfg.topic_siem_raw(env) == "omni-siem-raw"
        assert cfg.topic_siem_incidents(env) == "omni-siem-incidents"
        assert cfg.topic_siem_chains(env) == "omni-siem-chains"
        assert cfg.consumer_group(env) == "omni-siem-correlation"
        assert cfg.key_prefix(env) == "corr:"

    def test_topic_and_group_overrides(self):
        env = {
            "OMNI_SIEM_CORR_TOPIC_RAW": "raw-x",
            "OMNI_SIEM_CORR_TOPIC_INCIDENTS": "inc-x",
            "OMNI_SIEM_CORR_TOPIC_CHAINS": "chain-x",
            "OMNI_SIEM_CORR_CONSUMER_GROUP": "grp-x",
            "OMNI_SIEM_CORR_KEY_PREFIX": "pycorr:",
        }
        assert cfg.topic_siem_raw(env) == "raw-x"
        assert cfg.topic_siem_incidents(env) == "inc-x"
        assert cfg.topic_siem_chains(env) == "chain-x"
        assert cfg.consumer_group(env) == "grp-x"
        assert cfg.key_prefix(env) == "pycorr:"

    def test_blank_falls_back(self):
        assert cfg.topic_siem_raw({"OMNI_SIEM_CORR_TOPIC_RAW": "  "}) == "omni-siem-raw"
