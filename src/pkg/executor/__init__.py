"""Mutation / K8s write paths — must not be imported by analyst-only code."""

from execution.promotion import execute_write_pending_from_redis

__all__ = [
    "execute_write_pending_from_redis",
]
