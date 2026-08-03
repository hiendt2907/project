"""Duration-string → VictoriaMetrics/Prometheus range-query window.

Pure stdlib, no framework coupling — lives under src/pkg/observability/ (see
that package's __init__.py: dependency-light helpers shared between gateway,
workers, and anomaly/) so anomaly/sigma_calibrator.py does not need to import
workers/ just for this.
"""

from __future__ import annotations


def duration_to_vm_window(duration: str) -> tuple[str, str]:
    """duration '1h'|'24h'|'30m' -> (start, step)."""
    d = duration.strip().lower()
    if d.endswith("h") and len(d) > 1 and d[:-1].replace(".", "").isdigit():
        step = "30s" if float(d[:-1]) <= 6 else "5m"
        return f"now-{d}", step
    if d.endswith("m") and len(d) > 1 and d[:-1].isdigit():
        return f"now-{d}", "15s"
    return "now-1h", "30s"
