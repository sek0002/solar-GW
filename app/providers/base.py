from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderSnapshot:
    name: str
    kind: str
    status: str
    detail: str
    power_flow: dict[str, float] = field(default_factory=dict)
    batteries: list[dict[str, Any]] = field(default_factory=list)
    chargers: list[dict[str, Any]] = field(default_factory=list)
    plants: list[dict[str, Any]] = field(default_factory=list)
    vehicles: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    chart_series: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class ProviderError(RuntimeError):
    """Raised when a provider cannot be loaded."""
