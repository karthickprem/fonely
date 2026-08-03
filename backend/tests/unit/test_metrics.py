"""Tests for MetricsCollector and instrumentation."""

import threading

from fonely.core.metrics import MetricsCollector, normalize_path


class TestCounter:
    def test_increment_and_value(self) -> None:
        m = MetricsCollector()
        m.increment("test_counter")
        m.increment("test_counter")
        assert m.counter_value("test_counter") == 2

    def test_labeled_counters_are_isolated(self) -> None:
        m = MetricsCollector()
        m.increment("req", {"method": "GET"})
        m.increment("req", {"method": "POST"})
        m.increment("req", {"method": "POST"})
        assert m.counter_value("req", {"method": "GET"}) == 1
        assert m.counter_value("req", {"method": "POST"}) == 2

    def test_missing_counter_returns_zero(self) -> None:
        m = MetricsCollector()
        assert m.counter_value("nonexistent") == 0


class TestHistogram:
    def test_observe_and_summary(self) -> None:
        m = MetricsCollector()
        for v in [10, 20, 30, 40, 50]:
            m.observe("latency", v)
        summary = m.histogram_summary("latency")
        assert summary["count"] == 5
        assert summary["sum"] == 150
        assert summary["min"] == 10
        assert summary["max"] == 50
        assert summary["p50"] == 30

    def test_p95_and_p99(self) -> None:
        m = MetricsCollector()
        for v in range(1, 101):
            m.observe("perf", float(v))
        summary = m.histogram_summary("perf")
        assert summary["count"] == 100
        assert summary["p95"] >= 95
        assert summary["p99"] >= 99

    def test_empty_histogram(self) -> None:
        m = MetricsCollector()
        summary = m.histogram_summary("empty")
        assert summary["count"] == 0

    def test_labeled_histograms(self) -> None:
        m = MetricsCollector()
        m.observe("dur", 100, {"path": "/a"})
        m.observe("dur", 200, {"path": "/b"})
        assert m.histogram_summary("dur", {"path": "/a"})["count"] == 1
        assert m.histogram_summary("dur", {"path": "/b"})["count"] == 1


class TestGauge:
    def test_set_and_value(self) -> None:
        m = MetricsCollector()
        m.set_gauge("active", 5)
        assert m.gauge_value("active") == 5
        m.set_gauge("active", 3)
        assert m.gauge_value("active") == 3

    def test_missing_gauge_returns_zero(self) -> None:
        m = MetricsCollector()
        assert m.gauge_value("missing") == 0.0


class TestExport:
    def test_export_produces_valid_structure(self) -> None:
        m = MetricsCollector()
        m.increment("requests", {"status": "200"})
        m.observe("latency", 42)
        m.set_gauge("active", 7)
        data = m.export()
        assert "counters" in data
        assert "histograms" in data
        assert "gauges" in data
        assert "process_uptime_seconds" in data["gauges"]
        assert data["counters"]["requests"]["{status=200}"] == 1
        assert data["histograms"]["latency"]["{}"]["count"] == 1
        assert data["gauges"]["active"]["{}"] == 7


class TestThreadSafety:
    def test_concurrent_increments(self) -> None:
        m = MetricsCollector()
        count = 1000

        def worker() -> None:
            for _ in range(count):
                m.increment("concurrent")

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert m.counter_value("concurrent") == 4000


class TestReset:
    def test_reset_clears_all(self) -> None:
        m = MetricsCollector()
        m.increment("a")
        m.observe("b", 1)
        m.set_gauge("c", 1)
        m.reset()
        assert m.counter_value("a") == 0
        assert m.histogram_summary("b")["count"] == 0
        assert m.gauge_value("c") == 0.0


class TestPathNormalization:
    def test_uuid_normalized(self) -> None:
        assert (
            normalize_path("/conversations/abc12345-1234-5678-9012-abcdef012345/messages")
            == "/conversations/{id}/messages"
        )

    def test_integer_id_normalized(self) -> None:
        result = normalize_path("/onboarding/drafts/42/activate")
        assert result == "/onboarding/drafts/{id}/activate"

    def test_no_id_unchanged(self) -> None:
        assert normalize_path("/health/live") == "/health/live"
