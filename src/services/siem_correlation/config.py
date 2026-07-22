"""Env-driven config — KHÔNG hardcode. Mọi tham số đọc os.environ tại call
time theo pattern ``_ENV_<NAME>``/``_DEFAULT_<NAME>`` (chuẩn dự án).

Semantics mirror Go ``config.Load`` + ``GraphConfig.setDefaults``: giá trị
thiếu / parse lỗi / <=0 → default an toàn (khớp deployment brain-go hiện tại).
"""

from __future__ import annotations

import os
from typing import Mapping

_ENV_WINDOW_SECONDS = "OMNI_SIEM_CORR_WINDOW_SECONDS"
_DEFAULT_WINDOW_SECONDS = 600

_ENV_THRESHOLD = "OMNI_SIEM_CORR_THRESHOLD"
_DEFAULT_THRESHOLD = 3

_ENV_DEDUP_SECONDS = "OMNI_SIEM_CORR_DEDUP_SECONDS"
_DEFAULT_DEDUP_SECONDS = 900

_ENV_MIN_ENTITY_SPAN = "OMNI_SIEM_CORR_MIN_ENTITY_SPAN"
_DEFAULT_MIN_ENTITY_SPAN = 1

_ENV_MIN_CONFIDENCE = "OMNI_SIEM_CORR_MIN_CONFIDENCE"
_DEFAULT_MIN_CONFIDENCE = 0.5

_ENV_WEIGHT_ENTITY = "OMNI_SIEM_CORR_WEIGHT_ENTITY"
_DEFAULT_WEIGHT_ENTITY = 0.4

_ENV_WEIGHT_SEQUENCE = "OMNI_SIEM_CORR_WEIGHT_SEQUENCE"
_DEFAULT_WEIGHT_SEQUENCE = 0.35

_ENV_WEIGHT_VOLUME = "OMNI_SIEM_CORR_WEIGHT_VOLUME"
_DEFAULT_WEIGHT_VOLUME = 0.25

_ENV_TOPIC_RAW = "OMNI_SIEM_CORR_TOPIC_RAW"
_DEFAULT_TOPIC_RAW = "omni-siem-raw"

_ENV_TOPIC_INCIDENTS = "OMNI_SIEM_CORR_TOPIC_INCIDENTS"
_DEFAULT_TOPIC_INCIDENTS = "omni-siem-incidents"

_ENV_TOPIC_CHAINS = "OMNI_SIEM_CORR_TOPIC_CHAINS"
_DEFAULT_TOPIC_CHAINS = "omni-siem-chains"

_ENV_CONSUMER_GROUP = "OMNI_SIEM_CORR_CONSUMER_GROUP"
# Distinct from brain-go's "brain-go-kafka" so both engines can consume the
# full topic side-by-side during parity runs.
_DEFAULT_CONSUMER_GROUP = "omni-siem-correlation"

_ENV_KEY_PREFIX = "OMNI_SIEM_CORR_KEY_PREFIX"
# Same Redis key layout as brain-go ("corr:*"); parity runs override this so
# the two engines never share union-find/dedup state.
_DEFAULT_KEY_PREFIX = "corr:"


def _positive_int(env: Mapping[str, str] | None, name: str, default: int) -> int:
    env = os.environ if env is None else env
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_float(env: Mapping[str, str] | None, name: str, default: float) -> float:
    env = os.environ if env is None else env
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _non_blank_str(env: Mapping[str, str] | None, name: str, default: str) -> str:
    env = os.environ if env is None else env
    raw = (env.get(name) or "").strip()
    return raw or default


def corr_window_seconds(env: Mapping[str, str] | None = None) -> int:
    return _positive_int(env, _ENV_WINDOW_SECONDS, _DEFAULT_WINDOW_SECONDS)


def corr_threshold(env: Mapping[str, str] | None = None) -> int:
    return _positive_int(env, _ENV_THRESHOLD, _DEFAULT_THRESHOLD)


def corr_dedup_seconds(env: Mapping[str, str] | None = None) -> int:
    return _positive_int(env, _ENV_DEDUP_SECONDS, _DEFAULT_DEDUP_SECONDS)


def corr_min_entity_span(env: Mapping[str, str] | None = None) -> int:
    return _positive_int(env, _ENV_MIN_ENTITY_SPAN, _DEFAULT_MIN_ENTITY_SPAN)


def corr_min_confidence(env: Mapping[str, str] | None = None) -> float:
    return _positive_float(env, _ENV_MIN_CONFIDENCE, _DEFAULT_MIN_CONFIDENCE)


def corr_weight_entity(env: Mapping[str, str] | None = None) -> float:
    return _positive_float(env, _ENV_WEIGHT_ENTITY, _DEFAULT_WEIGHT_ENTITY)


def corr_weight_sequence(env: Mapping[str, str] | None = None) -> float:
    return _positive_float(env, _ENV_WEIGHT_SEQUENCE, _DEFAULT_WEIGHT_SEQUENCE)


def corr_weight_volume(env: Mapping[str, str] | None = None) -> float:
    return _positive_float(env, _ENV_WEIGHT_VOLUME, _DEFAULT_WEIGHT_VOLUME)


def topic_siem_raw(env: Mapping[str, str] | None = None) -> str:
    return _non_blank_str(env, _ENV_TOPIC_RAW, _DEFAULT_TOPIC_RAW)


def topic_siem_incidents(env: Mapping[str, str] | None = None) -> str:
    return _non_blank_str(env, _ENV_TOPIC_INCIDENTS, _DEFAULT_TOPIC_INCIDENTS)


def topic_siem_chains(env: Mapping[str, str] | None = None) -> str:
    return _non_blank_str(env, _ENV_TOPIC_CHAINS, _DEFAULT_TOPIC_CHAINS)


def consumer_group(env: Mapping[str, str] | None = None) -> str:
    return _non_blank_str(env, _ENV_CONSUMER_GROUP, _DEFAULT_CONSUMER_GROUP)


def key_prefix(env: Mapping[str, str] | None = None) -> str:
    return _non_blank_str(env, _ENV_KEY_PREFIX, _DEFAULT_KEY_PREFIX)
