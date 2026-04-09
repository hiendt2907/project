"""Đọc seed YAML → ExpandedSopEntry, round-robin + cap OMNI_MAX_SOP_CONTEXTS."""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import yaml
from pydantic import BaseModel, Field, field_validator

from rag.sop_ledger import canonical_variant_key, sop_point_id
from workers.routing_policy import READ_ONLY_FAST_PATH_TOOLS, fast_path_auto_execute_allowlist
from workers.settings import WorkerSettings
from workers.tools import TOOL_REGISTRY

# Backward compat tests / import từ module cũ
READ_ONLY_AUTO_EXECUTE = READ_ONLY_FAST_PATH_TOOLS

logger = logging.getLogger(__name__)


def _substitute(template: str, mapping: dict[str, str]) -> str:
    out = template
    for k, v in mapping.items():
        out = out.replace("{" + k + "}", v)
    return out


def _substitute_args(obj: Any, mapping: dict[str, str]) -> Any:
    if isinstance(obj, str):
        return _substitute(obj, mapping)
    if isinstance(obj, dict):
        return {k: _substitute_args(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_args(x, mapping) for x in obj]
    return obj


class SopTemplateModel(BaseModel):
    template_id: str
    allow_auto_execute: bool = False
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    match_text_template: str
    slots: dict[str, list[str]]

    @field_validator("slots")
    @classmethod
    def _non_empty_slots(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for key, vals in v.items():
            if not vals:
                raise ValueError(f"slot {key!r} empty")
        return v


class SopSeedFile(BaseModel):
    version: int = 1
    templates: list[SopTemplateModel]


@dataclass(frozen=True)
class ExpandedSopEntry:
    template_id: str
    variant_key: str
    point_id: str
    match_text: str
    tool: str
    args: dict[str, Any]
    auto_execute: bool


def _validate_tool(name: str) -> None:
    if name not in TOOL_REGISTRY:
        raise ValueError(f"unknown tool {name!r} — not in TOOL_REGISTRY")


def _expand_one_template(t: SopTemplateModel, *, allowlist: frozenset[str]) -> Iterator[ExpandedSopEntry]:
    _validate_tool(t.tool)
    keys = sorted(t.slots.keys())
    for combo in itertools.product(*(t.slots[k] for k in keys)):
        mapping = {k: str(combo[i]) for i, k in enumerate(keys)}
        match_text = _substitute(t.match_text_template, mapping).strip()
        args_out = _substitute_args(t.args, mapping)
        if not isinstance(args_out, dict):
            args_out = {}
        vk = canonical_variant_key(mapping)
        ae = bool(t.allow_auto_execute and t.tool in allowlist)
        yield ExpandedSopEntry(
            template_id=t.template_id,
            variant_key=vk,
            point_id=sop_point_id(template_id=t.template_id, variant_key=vk),
            match_text=match_text,
            tool=t.tool,
            args=args_out,
            auto_execute=ae,
        )


def _round_robin_take(iterators: list[Iterator[ExpandedSopEntry]], max_total: int) -> list[ExpandedSopEntry]:
    out: list[ExpandedSopEntry] = []
    active: list[Iterator[ExpandedSopEntry]] = list(iterators)
    while len(out) < max_total and active:
        nxt: list[Iterator[ExpandedSopEntry]] = []
        for it in active:
            try:
                out.append(next(it))
                nxt.append(it)
                if len(out) >= max_total:
                    return out
            except StopIteration:
                pass
        active = nxt
    return out


def load_seed_path(path: Path | str) -> SopSeedFile:
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("seed root must be a mapping")
    return SopSeedFile.model_validate(data)


def expand_entries(
    seed: SopSeedFile,
    *,
    max_total: int,
    shuffle_seed: int | None = None,
    god_mode: bool = False,
) -> list[ExpandedSopEntry]:
    # Prod validator strips god_mode; expand "god" branch must model dev/lab settings.
    allowlist = fast_path_auto_execute_allowlist(
        WorkerSettings(god_mode=god_mode, env_mode="dev" if god_mode else "prod")
    )
    iters = [iter(_expand_one_template(t, allowlist=allowlist)) for t in seed.templates]
    merged = _round_robin_take(iters, max_total)
    if shuffle_seed is not None:
        import random

        rng = random.Random(shuffle_seed)
        rng.shuffle(merged)
    logger.info(
        "sop_expand: %s templates → %s entries (cap=%s, shuffle_seed=%s)",
        len(seed.templates),
        len(merged),
        max_total,
        shuffle_seed,
    )
    return merged
