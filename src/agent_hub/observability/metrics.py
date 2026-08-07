from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class CounterMetric:
    name: str
    help: str
    value: int = 0

    def inc(self, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("counter increment must be non-negative")
        self.value += amount


@dataclass(slots=True)
class GaugeMetric:
    name: str
    help: str
    value: float = 0

    def set(self, value: float) -> None:
        self.value = value


@dataclass(slots=True)
class MetricsRegistry:
    counters: dict[str, CounterMetric] = field(default_factory=dict)
    gauges: dict[str, GaugeMetric] = field(default_factory=dict)

    def counter(self, name: str, help_text: str) -> CounterMetric:
        metric = self.counters.get(name)
        if metric is None:
            metric = CounterMetric(name, help_text)
            self.counters[name] = metric
        return metric

    def gauge(self, name: str, help_text: str) -> GaugeMetric:
        gauge_metric = self.gauges.get(name)
        if gauge_metric is None:
            gauge_metric = GaugeMetric(name, help_text)
            self.gauges[name] = gauge_metric
        return gauge_metric

    def render_prometheus(self) -> str:
        lines: list[str] = []
        for counter_metric in sorted(self.counters.values(), key=lambda item: item.name):
            lines.extend(
                [
                    f"# HELP {counter_metric.name} {counter_metric.help}",
                    f"# TYPE {counter_metric.name} counter",
                    f"{counter_metric.name} {counter_metric.value}",
                ]
            )
        for gauge_metric in sorted(self.gauges.values(), key=lambda item: item.name):
            lines.extend(
                [
                    f"# HELP {gauge_metric.name} {gauge_metric.help}",
                    f"# TYPE {gauge_metric.name} gauge",
                    f"{gauge_metric.name} {gauge_metric.value:g}",
                ]
            )
        return "\n".join(lines) + "\n"


def default_metrics_registry() -> MetricsRegistry:
    registry = MetricsRegistry()
    registry.counter("agent_hub_runs_total", "Total runs accepted by status.")
    registry.counter("agent_hub_model_429_total", "Provider rate-limit responses.")
    registry.gauge("agent_hub_queue_depth", "Current run queue depth.")
    registry.gauge("agent_hub_scheduler_lag_seconds", "Scheduler lag in seconds.")
    registry.gauge("agent_hub_model_capacity_wait_seconds", "Model capacity wait in seconds.")
    return registry


__all__ = ["CounterMetric", "GaugeMetric", "MetricsRegistry", "default_metrics_registry"]
