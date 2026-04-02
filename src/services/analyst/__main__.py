"""Analyst boundary check: only ``pkg.reasoning`` (no ``pkg.executor``). Full runtime: ``python -m workers``."""

from __future__ import annotations

import pkg.reasoning


def main() -> None:
    print("pkg.reasoning OK — use `python -m workers` for kafka_evidence_loop.")


if __name__ == "__main__":
    main()
