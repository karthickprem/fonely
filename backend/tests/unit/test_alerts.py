"""Tests for alerting thresholds."""

from fonely.core.alerts import check_alerts
from fonely.core.metrics import MetricsCollector


class TestAlerts:
    def test_no_alerts_when_healthy(self) -> None:
        m = MetricsCollector()
        m.observe("http_request_duration_ms", 100)
        m.observe("llm_request_duration_ms", 500)
        m.set_gauge("conversations_in_memory", 10)
        alerts = check_alerts(m)
        assert alerts == []

    def test_llm_latency_alert(self) -> None:
        m = MetricsCollector()
        for _ in range(100):
            m.observe("llm_request_duration_ms", 6000)
        alerts = check_alerts(m)
        llm_alert = [a for a in alerts if a["metric"] == "llm_request_p95_ms"]
        assert len(llm_alert) == 1
        assert llm_alert[0]["value"] >= 5000
        assert llm_alert[0]["threshold"] == 5000
        assert llm_alert[0]["severity"] == "warning"

    def test_conversations_active_alert(self) -> None:
        m = MetricsCollector()
        m.set_gauge("conversations_in_memory", 600)
        alerts = check_alerts(m)
        conv_alert = [a for a in alerts if a["metric"] == "conversations_active_max"]
        assert len(conv_alert) == 1
        assert conv_alert[0]["value"] == 600
        assert conv_alert[0]["threshold"] == 500

    def test_http_latency_alert(self) -> None:
        m = MetricsCollector()
        for _ in range(100):
            m.observe("http_request_duration_ms", 3000)
        alerts = check_alerts(m)
        http_alert = [a for a in alerts if a["metric"] == "http_request_p95_ms"]
        assert len(http_alert) == 1

    def test_empty_metrics_no_alerts(self) -> None:
        m = MetricsCollector()
        alerts = check_alerts(m)
        assert alerts == []
