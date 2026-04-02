"""Analyst boundary check: only ``pkg.reasoning`` (no ``pkg.executor``). Full runtime: ``python -m workers``."""

from __future__ import annotations

from pkg import reasoning


def main() -> None:
    print(
        f"pkg.reasoning OK ({reasoning.__name__}) — use `python -m workers` for kafka_evidence_loop."
    )


if __name__ == "__main__":
    main()
