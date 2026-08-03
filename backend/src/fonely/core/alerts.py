"""Alerting thresholds and health check."""

from typing import Any

from fonely.core.metrics import MetricsCollector

ALERT_THRESHOLDS: dict[str, dict[str, Any]] = {
    "http_request_p95_ms": {
        "metric": "http_request_duration_ms",
        "field": "p95",
        "threshold": 2000,
        "severity": "warning",
    },
    "llm_request_p95_ms": {
        "metric": "llm_request_duration_ms",
        "field": "p95",
        "threshold": 5000,
        "severity": "warning",
    },
    "conversations_active_max": {
        "metric": "conversations_in_memory",
        "type": "gauge",
        "threshold": 500,
        "severity": "warning",
    },
}


def check_alerts(collector: MetricsCollector) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for name, config in ALERT_THRESHOLDS.items():
        metric_name = config["metric"]
        threshold = config["threshold"]
        severity = config["severity"]

        if config.get("type") == "gauge":
            value = collector.gauge_value(metric_name)
            if value > threshold:
                alerts.append(
                    {
                        "metric": name,
                        "value": value,
                        "threshold": threshold,
                        "severity": severity,
                    }
                )
        else:
            field = config.get("field", "p95")
            summary = collector.histogram_summary(metric_name)
            value = summary.get(field, 0)
            if value > threshold:
                alerts.append(
                    {
                        "metric": name,
                        "value": value,
                        "threshold": threshold,
                        "severity": severity,
                    }
                )
    return alerts
