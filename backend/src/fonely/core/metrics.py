"""Thread-safe in-process metrics with JSON export."""

import re
import threading
import time
from typing import Any

_DEFAULT_HISTOGRAM_WINDOW = 1000


class Counter:
    def __init__(self) -> None:
        self._values: dict[str, int] = {}
        self._lock = threading.Lock()

    def increment(self, labels: dict[str, str] | None = None) -> None:
        key = _label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0) + 1

    def value(self, labels: dict[str, str] | None = None) -> int:
        return self._values.get(_label_key(labels), 0)

    def export(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)


class Histogram:
    def __init__(self, max_size: int = _DEFAULT_HISTOGRAM_WINDOW) -> None:
        self._data: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._max_size = max_size

    def observe(self, value: float, labels: dict[str, str] | None = None) -> None:
        key = _label_key(labels)
        with self._lock:
            bucket = self._data.setdefault(key, [])
            bucket.append(value)
            if len(bucket) > self._max_size:
                del bucket[: len(bucket) - self._max_size]

    def summary(self, labels: dict[str, str] | None = None) -> dict[str, float]:
        key = _label_key(labels)
        with self._lock:
            values = list(self._data.get(key, []))
        if not values:
            return {"count": 0, "sum": 0, "min": 0, "max": 0, "p50": 0, "p95": 0, "p99": 0}
        sorted_vals = sorted(values)
        return {
            "count": len(sorted_vals),
            "sum": round(sum(sorted_vals), 2),
            "min": round(sorted_vals[0], 2),
            "max": round(sorted_vals[-1], 2),
            "p50": round(_percentile(sorted_vals, 50), 2),
            "p95": round(_percentile(sorted_vals, 95), 2),
            "p99": round(_percentile(sorted_vals, 99), 2),
        }

    def window_size(self, labels: dict[str, str] | None = None) -> int:
        key = _label_key(labels)
        with self._lock:
            return len(self._data.get(key, []))

    def export(self) -> dict[str, dict[str, float]]:
        with self._lock:
            keys = list(self._data.keys())
        return {key: self.summary(_parse_label_key(key)) for key in keys}


class Gauge:
    def __init__(self) -> None:
        self._values: dict[str, float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, labels: dict[str, str] | None = None) -> None:
        key = _label_key(labels)
        with self._lock:
            self._values[key] = value

    def increment(self, labels: dict[str, str] | None = None, amount: float = 1.0) -> None:
        key = _label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def decrement(self, labels: dict[str, str] | None = None, amount: float = 1.0) -> None:
        key = _label_key(labels)
        with self._lock:
            self._values[key] = max(0, self._values.get(key, 0.0) - amount)

    def value(self, labels: dict[str, str] | None = None) -> float:
        return self._values.get(_label_key(labels), 0.0)

    def export(self) -> dict[str, float]:
        with self._lock:
            return dict(self._values)


class MetricsCollector:
    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        self._gauges: dict[str, Gauge] = {}
        self._lock = threading.Lock()
        self._start_time = time.monotonic()

    def increment(self, name: str, labels: dict[str, str] | None = None) -> None:
        counter = self._get_counter(name)
        counter.increment(labels)

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        histogram = self._get_histogram(name)
        histogram.observe(value, labels)

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        gauge = self._get_gauge(name)
        gauge.set(value, labels)

    def increment_gauge(
        self, name: str, labels: dict[str, str] | None = None, amount: float = 1.0
    ) -> None:
        self._get_gauge(name).increment(labels, amount)

    def decrement_gauge(
        self, name: str, labels: dict[str, str] | None = None, amount: float = 1.0
    ) -> None:
        self._get_gauge(name).decrement(labels, amount)

    def counter_value(self, name: str, labels: dict[str, str] | None = None) -> int:
        counter = self._counters.get(name)
        return counter.value(labels) if counter else 0

    def histogram_summary(
        self, name: str, labels: dict[str, str] | None = None
    ) -> dict[str, float]:
        histogram = self._histograms.get(name)
        if histogram is None:
            return {"count": 0, "sum": 0, "min": 0, "max": 0, "p50": 0, "p95": 0, "p99": 0}
        return histogram.summary(labels)

    def gauge_value(self, name: str, labels: dict[str, str] | None = None) -> float:
        gauge = self._gauges.get(name)
        return gauge.value(labels) if gauge else 0.0

    def export(self) -> dict[str, Any]:
        counters = {name: c.export() for name, c in self._counters.items()}
        histograms = {name: h.export() for name, h in self._histograms.items()}
        gauges = {name: g.export() for name, g in self._gauges.items()}
        gauges.setdefault("process_uptime_seconds", {})["{}"] = round(
            time.monotonic() - self._start_time, 1
        )
        return {"counters": counters, "histograms": histograms, "gauges": gauges}

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()
            self._gauges.clear()
            self._start_time = time.monotonic()

    def _get_counter(self, name: str) -> Counter:
        counter = self._counters.get(name)
        if counter is None:
            with self._lock:
                counter = self._counters.get(name)
                if counter is None:
                    counter = Counter()
                    self._counters[name] = counter
        return counter

    def _get_histogram(self, name: str) -> Histogram:
        histogram = self._histograms.get(name)
        if histogram is None:
            with self._lock:
                histogram = self._histograms.get(name)
                if histogram is None:
                    histogram = Histogram()
                    self._histograms[name] = histogram
        return histogram

    def _get_gauge(self, name: str) -> Gauge:
        gauge = self._gauges.get(name)
        if gauge is None:
            with self._lock:
                gauge = self._gauges.get(name)
                if gauge is None:
                    gauge = Gauge()
                    self._gauges[name] = gauge
        return gauge


def _label_key(labels: dict[str, str] | None) -> str:
    if not labels:
        return "{}"
    parts = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{{{parts}}}"


def _parse_label_key(key: str) -> dict[str, str] | None:
    if key == "{}":
        return None
    inner = key.strip("{}")
    if not inner:
        return None
    return dict(pair.split("=", 1) for pair in inner.split(","))


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * pct / 100
    f = int(k)
    c = f + 1
    if c >= len(sorted_values):
        return sorted_values[-1]
    return sorted_values[f] + (k - f) * (sorted_values[c] - sorted_values[f])


def normalize_path(path: str) -> str:
    path = re.sub(
        r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "/{id}",
        path,
    )
    path = re.sub(r"/(\d+)(?=/|$)", "/{id}", path)
    return path


metrics = MetricsCollector()
