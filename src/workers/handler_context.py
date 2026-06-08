"""Worker request context — **no** pkg.executor / handlers (import-safe for analyst-only paths)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as redis
from ingest.telegram import TelegramClient
from llm.protocol import LlmClient
from messaging.kafka_bus import KafkaBus
from rag.error_ledger import ErrorLedger
from rag.redis_vector_store import RedisVectorStore

from workers.llm_semaphore import LLMSemaphore
from workers.settings import WorkerSettings


@dataclass
class WorkerHandlerContext:
    settings: WorkerSettings
    redis: redis.Redis
    llm: LlmClient
    vector_store: RedisVectorStore
    ledger: ErrorLedger
    semaphore: LLMSemaphore
    telegram: TelegramClient | None
    kafka: KafkaBus | None = None
    telegram_chat_id: int | None = None
    inbound_source: str = ""
    inbound_user_text: str = ""
    restart_rollout_explicit: bool = False
    pod_discovery_pairs: list[tuple[str, str]] = field(default_factory=list)
    scout_ready: asyncio.Event = field(default_factory=asyncio.Event)
    # Same id as Kafka omni-alerts / evidence / actions when set by consumer loops (ContextVar mirrors this).
    inbound_trace_id: str = "unknown"
    llm_slot_held: bool = False
    inbound_proactive: bool = False
    k8s_mutated: bool = False
    fallback_inline_commands: list[str] | None = None
    # Admin config store (Postgres omni_admin) — None khi OMNI_ADMIN_PG_DSN chưa set.
    admin_pool: Any = None
    admin_repo: Any = None
