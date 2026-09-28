"""Progress-aware execution budgets with an absolute safety fuse."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


def _positive_finite(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a positive finite number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return normalized


@dataclass(slots=True)
class AdaptiveDeadline:
    """Extend a soft deadline only when durable progress increases."""

    deadline: float
    absolute_deadline: float
    extension_per_unit: float
    progress_units: int = 0

    @classmethod
    def create(
        cls,
        *,
        now: float,
        initial_seconds: float,
        soft_seconds: float,
        absolute_seconds: float,
        complexity_units: int,
        restored_absolute_seconds: float | None = None,
        progress_units: int = 0,
    ) -> AdaptiveDeadline:
        current = float(now)
        if not math.isfinite(current):
            raise ValueError("now must be finite")
        initial = _positive_finite(initial_seconds, name="initial_seconds")
        soft = _positive_finite(soft_seconds, name="soft_seconds")
        absolute = _positive_finite(absolute_seconds, name="absolute_seconds")
        if restored_absolute_seconds is not None:
            absolute = min(
                absolute,
                _positive_finite(
                    restored_absolute_seconds,
                    name="restored_absolute_seconds",
                ),
            )
        if type(complexity_units) is not int or complexity_units <= 0:
            raise ValueError("complexity_units must be a positive integer")
        if type(progress_units) is not int or progress_units < 0:
            raise ValueError("progress_units must be a non-negative integer")
        absolute_deadline = current + absolute
        return cls(
            deadline=min(current + initial, absolute_deadline),
            absolute_deadline=absolute_deadline,
            extension_per_unit=soft / complexity_units,
            progress_units=progress_units,
        )

    def observe(self, *, progress_units: int, now: float) -> bool:
        if type(progress_units) is not int or progress_units < 0:
            raise ValueError("progress_units must be a non-negative integer")
        current = float(now)
        if not math.isfinite(current):
            raise ValueError("now must be finite")
        if progress_units <= self.progress_units or current >= self.absolute_deadline:
            return False
        delta = progress_units - self.progress_units
        self.progress_units = progress_units
        self.deadline = min(
            self.absolute_deadline,
            self.deadline + self.extension_per_unit * delta,
        )
        return True

    def remaining(self, *, now: float) -> float:
        return max(0.0, self.deadline - float(now))

    def absolute_remaining(self, *, now: float) -> float:
        return max(0.0, self.absolute_deadline - float(now))


def deadline_from_routing(
    routing_decision: Mapping[str, Any],
    *,
    now: float,
    initial_seconds: float,
    restored_absolute_seconds: float | None = None,
    progress_units: int = 0,
) -> AdaptiveDeadline:
    """Build an adaptive deadline from the project-scale routing contract."""

    initial = _positive_finite(initial_seconds, name="initial_seconds")
    if routing_decision.get("runtime_timeout_source") != "project_scale_soft_budget":
        return AdaptiveDeadline.create(
            now=now,
            initial_seconds=initial,
            soft_seconds=initial,
            absolute_seconds=initial,
            complexity_units=1,
            restored_absolute_seconds=restored_absolute_seconds,
            progress_units=progress_units,
        )

    soft_value = routing_decision.get("runtime_timeout_soft_seconds", initial)
    absolute_value = routing_decision.get("runtime_timeout_absolute_seconds", initial)
    complexity_value = routing_decision.get("critical_path_complexity_units", 1)
    soft = (
        float(soft_value)
        if isinstance(soft_value, int | float) and not isinstance(soft_value, bool)
        else initial
    )
    absolute = (
        float(absolute_value)
        if isinstance(absolute_value, int | float) and not isinstance(absolute_value, bool)
        else initial
    )
    complexity = complexity_value if type(complexity_value) is int and complexity_value > 0 else 1
    return AdaptiveDeadline.create(
        now=now,
        initial_seconds=initial,
        soft_seconds=soft,
        absolute_seconds=max(initial, absolute),
        complexity_units=complexity,
        restored_absolute_seconds=restored_absolute_seconds,
        progress_units=progress_units,
    )
