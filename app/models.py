from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class SourceStatus(BaseModel):
    name: str
    kind: Literal["battery", "solar", "energy", "vehicle", "hybrid"]
    status: Literal["connected", "degraded", "disconnected", "demo"]
    detail: str


class BatteryStatus(BaseModel):
    name: str
    source: str
    state_of_charge: float | None = None
    power_kw: float | None = None
    state: str | None = None
    health: str | None = None


class VehicleStatus(BaseModel):
    name: str
    source: str
    vin: str | None = None
    battery_level: int | None = None
    charging_state: str | None = None
    charge_current_a: float | None = None
    charge_power_kw: float | None = None
    range_km: float | None = None
    plugged_in: bool | None = None
    location: str | None = None


class ChargerStatus(BaseModel):
    name: str
    source: str
    status: str | None = None
    active_sessions: int = 0
    connected_vehicles: int = 0
    power_kw: float | None = None
    max_power_kw: float | None = None
    circuit_amps: int | None = None
    location: str | None = None
    vehicle_names: list[str] = Field(default_factory=list)


class PlantStatus(BaseModel):
    name: str
    source: str
    plant_id: str | None = None
    status: str | None = None
    plant_type: str | None = None
    timezone: str | None = None
    capacity_kw: float | None = None
    device_count: int | None = None


class PowerFlow(BaseModel):
    solar_kw: float = 0.0
    home_kw: float = 0.0
    battery_kw: float = 0.0
    grid_kw: float = 0.0


class SummaryMetric(BaseModel):
    label: str
    value: float | str
    unit: str = ""
    tone: Literal["neutral", "good", "warn", "accent"] = "neutral"


class EnergyChartPoint(BaseModel):
    timestamp: datetime
    value: float | None = None
    state: str | None = None


class EnergyChartSeries(BaseModel):
    key: str
    label: str
    unit: str
    color: str
    axis: Literal["power", "percent"] = "power"
    points: list[EnergyChartPoint] = Field(default_factory=list)


class AutomationRule(BaseModel):
    id: str
    label: str
    description: str
    enabled: bool = True
    active: bool = False
    detail: str | None = None
    target_amps: int | None = None
    target_kw: float | None = None


class ManualChargeControl(BaseModel):
    enabled: bool = False
    target_amps: int = 10
    target_kw: float = 2.3
    status: str = "idle"
    detail: str | None = None
    notes: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class AutomationPanel(BaseModel):
    global_enabled: bool = True
    rules: list[AutomationRule] = Field(default_factory=list)
    manual_charge: ManualChargeControl = Field(default_factory=ManualChargeControl)
    effective_mode: str = "Idle"
    effective_detail: str = "Automation waiting for an active rule."
    effective_target_amps: int | None = None
    effective_target_kw: float | None = None


class DashboardData(BaseModel):
    site_name: str = "Home Energy"
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    refresh_interval_seconds: int = 30
    power_flow: PowerFlow = Field(default_factory=PowerFlow)
    summary_metrics: list[SummaryMetric] = Field(default_factory=list)
    energy_chart: list[EnergyChartSeries] = Field(default_factory=list)
    automation_panel: AutomationPanel = Field(default_factory=AutomationPanel)
    batteries: list[BatteryStatus] = Field(default_factory=list)
    chargers: list[ChargerStatus] = Field(default_factory=list)
    plants: list[PlantStatus] = Field(default_factory=list)
    vehicles: list[VehicleStatus] = Field(default_factory=list)
    sources: list[SourceStatus] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
