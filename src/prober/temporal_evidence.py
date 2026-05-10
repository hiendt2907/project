"""Temporal evidence fetcher — historical metrics + rate-of-change for forecasting."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)


class TemporalMetric:
    """Single metric time series with rate-of-change calculation."""

    def __init__(self, name: str, values: list[tuple[float, float]]):
        """
        Args:
            name: metric name (e.g., 'cpu_percent', 'memory_bytes')
            values: [(timestamp, value), ...] sorted by timestamp
        """
        self.name = name
        self.values = sorted(values, key=lambda x: x[0])

    def current_value(self) -> float | None:
        """Latest value."""
        return self.values[-1][1] if self.values else None

    def rate_of_change(self) -> float | None:
        """Change per minute (linear extrapolation)."""
        if len(self.values) < 2:
            return None
        t0, v0 = self.values[0]
        t1, v1 = self.values[-1]
        if t1 <= t0:
            return None
        minutes = (t1 - t0) / 60.0
        if minutes == 0:
            return None
        return (v1 - v0) / minutes

    def forecast_at(self, minutes_ahead: int) -> float | None:
        """Linear extrapolation N minutes ahead."""
        current = self.current_value()
        rate = self.rate_of_change()
        if current is None or rate is None:
            return None
        return current + (rate * minutes_ahead)

    @property
    def sample_points(self) -> int:
        """Number of data points in this metric."""
        return len(self.values)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for prompt injection."""
        return {
            "name": self.name,
            "current_value": self.current_value(),
            "rate_per_minute": self.rate_of_change(),
            "sample_points": self.sample_points,
            "time_span_minutes": (
                (self.values[-1][0] - self.values[0][0]) / 60.0
                if len(self.values) > 1
                else 0
            ),
        }


class TemporalEvidenceBlock:
    """Structured evidence with historical + current state."""

    def __init__(self, probe_name: str, namespace: str = "", pod: str = "", deployment: str = ""):
        self.probe_name = probe_name
        self.namespace = namespace
        self.pod = pod
        self.deployment = deployment
        self.metrics: dict[str, TemporalMetric] = {}
        self.current_state: dict[str, Any] = {}
        self.alert_message: str = ""
        self.probe_status: str = "UNKNOWN"

    def add_metric(self, name: str, values: list[tuple[float, float]]) -> None:
        """Ingest a time series."""
        self.metrics[name] = TemporalMetric(name, values)

    def set_current_state(self, state_obj: dict[str, Any]) -> None:
        """Capture current Kubernetes state (pod status, resource state, etc.)."""
        self.current_state = dict(state_obj or {})

    @staticmethod
    async def fetch_from_prometheus(
        prometheus_url: str,
        promql_query: str,
        metric_name: str,
        hours_back: int = 1,
        step: str = "60s",
        timeout: float = 30.0,
    ) -> "TemporalEvidenceBlock | None":
        """
        Fetch 1-hour historical metric data from Prometheus via query_range.

        Args:
            prometheus_url: Base URL (e.g., "http://prometheus:9090")
            promql_query: PromQL query (must return scalar or single series)
            metric_name: Name to assign to the metric
            hours_back: Number of hours of history to fetch (default 1h)
            step: Prometheus step parameter (default 60s = 1 sample per minute)
            timeout: HTTP timeout in seconds

        Returns:
            TemporalEvidenceBlock with populated metrics, or None on error
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=hours_back)
        end = now

        start_str = str(int(start.timestamp()))
        end_str = str(int(end.timestamp()))

        query_params = {
            "query": promql_query,
            "start": start_str,
            "end": end_str,
            "step": step,
        }
        url = f"{prometheus_url.rstrip('/')}/api/v1/query_range?{urlencode(query_params)}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()

                if data.get("status") != "success":
                    logger.warning(
                        "event=prometheus_query_failed query=%s status=%s",
                        promql_query[:100],
                        data.get("status"),
                    )
                    return None

                result = data.get("data", {}).get("result", [])
                if not result:
                    logger.debug(
                        "event=prometheus_query_no_data query=%s",
                        promql_query[:100],
                    )
                    return None

                # Extract first series (user must use sum/avg in PromQL for multiple series)
                series = result[0]
                values_raw = series.get("values", [])

                block = TemporalEvidenceBlock(
                    probe_name="prometheus",
                    namespace="monitoring",
                )

                # Parse [timestamp_int, "value_string"] pairs
                values_tuples = []
                for ts, val_str in values_raw:
                    try:
                        ts_float = float(ts)
                        val_float = float(val_str)
                        values_tuples.append((ts_float, val_float))
                    except (ValueError, TypeError):
                        logger.debug(
                            "event=prometheus_value_parse_error ts=%s val=%s",
                            ts,
                            val_str,
                        )
                        continue

                if values_tuples:
                    block.add_metric(metric_name, values_tuples)
                    logger.info(
                        "event=prometheus_fetch_success query=%s metric=%s samples=%s",
                        promql_query[:80],
                        metric_name,
                        len(values_tuples),
                    )
                    return block
                else:
                    logger.warning(
                        "event=prometheus_no_valid_samples query=%s",
                        promql_query[:100],
                    )
                    return None

        except httpx.HTTPError as e:
            logger.warning(
                "event=prometheus_http_error query=%s err=%s",
                promql_query[:100],
                str(e)[:200],
            )
            return None
        except Exception as e:
            logger.warning(
                "event=prometheus_fetch_error query=%s err=%s",
                promql_query[:100],
                str(e)[:200],
            )
            return None

    def forecast_linearly(self, hours: int = 24) -> dict[int, dict[str, Any]]:
        """
        Project all metrics forward in time.
        Returns: {minutes_ahead: {metric_name: forecasted_value, ...}, ...}
        """
        out = {}
        for h in [1, 3, 6, 12, 24]:
            if h > hours:
                break
            mins = h * 60
            out[mins] = {}
            for metric_name, metric in self.metrics.items():
                fv = metric.forecast_at(mins)
                if fv is not None:
                    out[mins][metric_name] = round(fv, 2)
        return out

    def to_prompt_block(self) -> str:
        """
        Format temporal evidence as a [TEMPORAL_EVIDENCE ...] block for LLM prompts.
        Example:
        [TEMPORAL_EVIDENCE probe=cpu_percent current=78.5 rate_per_min=+2.1 samples=60 forecast_1h=108.5 confidence=high]
        """
        if not self.metrics:
            return ""

        blocks = []
        for metric_name, metric in self.metrics.items():
            current = metric.current_value()
            rate = metric.rate_of_change()
            forecast_1h = metric.forecast_at(60)

            if current is None:
                continue

            confidence = "high" if metric.sample_points >= 30 else "medium" if metric.sample_points >= 10 else "low"

            block = f"[TEMPORAL_EVIDENCE probe={metric_name} current={current:.1f} "
            if rate is not None:
                block += f"rate_per_min={rate:+.2f} "
            block += f"samples={metric.sample_points} "
            if forecast_1h is not None:
                block += f"forecast_1h={forecast_1h:.1f} "
            block += f"confidence={confidence}]"
            blocks.append(block)

        return "\n".join(blocks)

    @property
    def sample_points(self) -> int:
        """Total samples across all metrics."""
        return sum(m.sample_points for m in self.metrics.values())

    def to_prompt_block(self) -> str:
        """Format for LLM consumption (temporal + current state)."""
        lines = [
            f"[TEMPORAL_EVIDENCE probe={self.probe_name}]",
            f"namespace={self.namespace or 'unknown'}",
            f"pod={self.pod or 'unknown'}",
            f"deployment={self.deployment or 'unknown'}",
            f"status={self.probe_status}",
            f"alert_message={self.alert_message}",
        ]
        if self.metrics:
            lines.append("\n[METRICS — Historical + Rate-of-Change]")
            for name, metric in self.metrics.items():
                md = metric.to_dict()
                lines.append(
                    f"  {name}: current={md['current_value']}, "
                    f"rate_per_min={md['rate_per_minute']}, "
                    f"samples={md['sample_points']}"
                )
        if self.current_state:
            lines.append(f"\n[CURRENT_STATE (SDK snapshot)]\n{json.dumps(self.current_state, indent=2)}")
        forecasts = self.forecast_linearly()
        if forecasts:
            lines.append("\n[LINEAR_FORECASTS (if trend continues)]")
            for mins, data in sorted(forecasts.items()):
                h = mins // 60
                lines.append(f"  +{h}h: {json.dumps(data)}")
        lines.append("[END_TEMPORAL_EVIDENCE]")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON for Redis/storage."""
        return {
            "probe_name": self.probe_name,
            "namespace": self.namespace,
            "pod": self.pod,
            "deployment": self.deployment,
            "probe_status": self.probe_status,
            "alert_message": self.alert_message,
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
            "current_state": self.current_state,
            "forecasts": self.forecast_linearly(),
        }
