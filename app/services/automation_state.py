from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.models import AutomationPanel, AutomationRule, DashboardData, ManualChargeControl

AUTOMATION_FILE = Path(".data/automation_state.json")
LOCAL_TZ = ZoneInfo("Australia/Melbourne")
VOLTAGE = 230.0
MIN_AMPS = 5
MAX_AMPS = 30


class PersistedAutomationRule(BaseModel):
    enabled: bool = True


class PersistedManualCharge(BaseModel):
    enabled: bool = False
    target_amps: int = 10


class PersistedAutomationState(BaseModel):
    global_enabled: bool = True
    rules: dict[str, PersistedAutomationRule] = Field(
        default_factory=lambda: {
            "off_peak_midday": PersistedAutomationRule(enabled=True),
            "solar_match": PersistedAutomationRule(enabled=True),
            "battery_floor_pause": PersistedAutomationRule(enabled=True),
            "night_trickle": PersistedAutomationRule(enabled=True),
        }
    )
    manual_charge: PersistedManualCharge = Field(default_factory=PersistedManualCharge)


class RuleTogglePayload(BaseModel):
    rule_id: str
    enabled: bool


class GlobalAutomationPayload(BaseModel):
    enabled: bool


class ManualChargePayload(BaseModel):
    enabled: bool
    target_amps: int


def _ensure_parent() -> None:
    AUTOMATION_FILE.parent.mkdir(parents=True, exist_ok=True)


def clamp_amps(value: int) -> int:
    return max(MIN_AMPS, min(MAX_AMPS, int(value)))


def amps_to_kw(amps: int) -> float:
    return round((amps * VOLTAGE) / 1000.0, 2)


def load_persisted_state() -> PersistedAutomationState:
    if not AUTOMATION_FILE.exists():
        state = PersistedAutomationState()
        save_persisted_state(state)
        return state
    try:
        return PersistedAutomationState.model_validate_json(AUTOMATION_FILE.read_text())
    except Exception:
        state = PersistedAutomationState()
        save_persisted_state(state)
        return state


def save_persisted_state(state: PersistedAutomationState) -> None:
    _ensure_parent()
    AUTOMATION_FILE.write_text(json.dumps(state.model_dump(mode="json"), indent=2))


def update_rule(rule_id: str, enabled: bool) -> PersistedAutomationState:
    state = load_persisted_state()
    if rule_id not in state.rules:
        state.rules[rule_id] = PersistedAutomationRule(enabled=enabled)
    else:
        state.rules[rule_id].enabled = enabled
    save_persisted_state(state)
    return state


def update_global_automation(enabled: bool) -> PersistedAutomationState:
    state = load_persisted_state()
    state.global_enabled = enabled
    save_persisted_state(state)
    return state


def update_manual_charge(enabled: bool, target_amps: int) -> PersistedAutomationState:
    state = load_persisted_state()
    state.manual_charge.enabled = enabled
    state.manual_charge.target_amps = clamp_amps(target_amps)
    save_persisted_state(state)
    return state


def _series_values(data: DashboardData, key: str) -> list[tuple[datetime, float]]:
    for series in data.energy_chart:
        if series.key != key:
            continue
        rows: list[tuple[datetime, float]] = []
        for point in series.points:
            if point.value is None:
                continue
            rows.append((point.timestamp, point.value))
        return rows
    return []


def _latest_value(data: DashboardData, key: str) -> float | None:
    rows = _series_values(data, key)
    return rows[-1][1] if rows else None


def _sustained_threshold(data: DashboardData, key: str, threshold: float, minutes: int, above: bool = True) -> bool:
    rows = _series_values(data, key)
    if not rows:
        return False
    cutoff = datetime.now(LOCAL_TZ) - timedelta(minutes=minutes)
    recent = [(ts, value) for ts, value in rows if ts.astimezone(LOCAL_TZ) >= cutoff]
    if not recent:
        return False
    if above:
        return all(value >= threshold for _, value in recent)
    return all(value < threshold for _, value in recent)


def build_automation_panel(data: DashboardData) -> AutomationPanel:
    state = load_persisted_state()
    now = datetime.now(LOCAL_TZ)
    battery_soc = _latest_value(data, "growatt_soc_pct")
    solar_kw = _latest_value(data, "solar_input_kw") or 0.0

    rule_1_enabled = state.rules["off_peak_midday"].enabled
    rule_2_enabled = state.rules["solar_match"].enabled
    rule_3_enabled = state.rules["battery_floor_pause"].enabled
    rule_4_enabled = state.rules["night_trickle"].enabled

    midday_active = rule_1_enabled and 12 <= now.hour < 14
    solar_match_active = (
        rule_2_enabled
        and (battery_soc or 0.0) > 40
        and _sustained_threshold(data, "solar_input_kw", threshold=1.0, minutes=10, above=True)
    )
    battery_floor_active = rule_3_enabled and battery_soc is not None and battery_soc < 40
    night_trickle_active = rule_4_enabled and 0 <= now.hour < 6 and (battery_soc or 0.0) > 50

    if not state.global_enabled:
        midday_active = False
        solar_match_active = False
        battery_floor_active = False
        night_trickle_active = False

    solar_match_amps = clamp_amps(round((solar_kw * 1000.0) / VOLTAGE)) if solar_kw > 0 else MIN_AMPS
    night_trickle_amps = clamp_amps(round((1.5 * 1000.0) / VOLTAGE))

    rules = [
        AutomationRule(
            id="off_peak_midday",
            label="12pm-2pm off-peak",
            description="Between 12pm and 2pm, charge Tesla at max 30A with no extra conditions.",
            enabled=rule_1_enabled,
            active=midday_active,
            detail="Active during the off-peak midday window." if midday_active else "Waiting for the 12pm-2pm window.",
            target_amps=30,
            target_kw=amps_to_kw(30),
        ),
        AutomationRule(
            id="solar_match",
            label="Solar match",
            description="If battery is above 40% and solar stays above 1kW for 10 minutes, match EV charging to solar. Pause if solar stays below 1kW for 10 minutes.",
            enabled=rule_2_enabled,
            active=solar_match_active,
            detail=(
                "Solar has held above 1kW for 10 minutes."
                if solar_match_active
                else "Needs battery >40% and sustained solar >1kW for 10 minutes."
            ),
            target_amps=solar_match_amps,
            target_kw=amps_to_kw(solar_match_amps),
        ),
        AutomationRule(
            id="battery_floor_pause",
            label="Battery floor pause",
            description="Pause all Tesla charging whenever the Growatt battery drops below 40%.",
            enabled=rule_3_enabled,
            active=battery_floor_active,
            detail="Battery is below 40%, so charging should pause." if battery_floor_active else "Standing by until the battery falls below 40%.",
        ),
        AutomationRule(
            id="night_trickle",
            label="Night trickle",
            description="From midnight overnight, allow a combined 1.5kW trickle charge when battery is above 50%.",
            enabled=rule_4_enabled,
            active=night_trickle_active,
            detail=(
                "Night trickle is armed because battery is above 50%."
                if night_trickle_active
                else "Available between midnight and 6am when battery is above 50%."
            ),
            target_amps=night_trickle_amps,
            target_kw=1.5,
        ),
    ]

    manual = ManualChargeControl(
        enabled=state.manual_charge.enabled,
        target_amps=clamp_amps(state.manual_charge.target_amps),
        target_kw=amps_to_kw(clamp_amps(state.manual_charge.target_amps)),
    )

    effective_mode = "Idle"
    effective_detail = "Automation waiting for an active rule."
    effective_target_amps = None
    effective_target_kw = None

    if manual.enabled:
        effective_mode = "Manual charge"
        effective_detail = "Manual charge override is enabled."
        effective_target_amps = manual.target_amps
        effective_target_kw = manual.target_kw
    elif not state.global_enabled:
        effective_mode = "Automation paused"
        effective_detail = "Combined automation toggle is off."
    elif battery_floor_active:
        effective_mode = "Pause charging"
        effective_detail = "Battery floor rule is pausing all Tesla charging."
    elif midday_active:
        effective_mode = "Off-peak charge"
        effective_detail = "Midday off-peak rule is requesting maximum combined charging."
        effective_target_amps = 30
        effective_target_kw = amps_to_kw(30)
    elif solar_match_active:
        effective_mode = "Solar match"
        effective_detail = "Charging should track current GoodWe solar input."
        effective_target_amps = solar_match_amps
        effective_target_kw = amps_to_kw(solar_match_amps)
    elif night_trickle_active:
        effective_mode = "Night trickle"
        effective_detail = "Overnight trickle rule is active."
        effective_target_amps = night_trickle_amps
        effective_target_kw = 1.5

    return AutomationPanel(
        global_enabled=state.global_enabled,
        rules=rules,
        manual_charge=manual,
        effective_mode=effective_mode,
        effective_detail=effective_detail,
        effective_target_amps=effective_target_amps,
        effective_target_kw=effective_target_kw,
    )
