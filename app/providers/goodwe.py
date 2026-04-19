from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

from pygoodwe import SingleInverter

from app.config import Settings
from app.providers.base import ProviderSnapshot


def _safe_float(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_inverters(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _parse_labeled_number(value: Any) -> float | None:
    if not isinstance(value, str):
        return _safe_float(value)
    match = re.search(r"(-?\d+(?:\.\d+)?)", value)
    if not match:
        return None
    return _safe_float(match.group(1))


def _load_goodwe_sync(settings: Settings) -> ProviderSnapshot:
    inverter = SingleInverter(
        system_id=settings.goodwe_plant_id or "",
        account=settings.goodwe_username or "",
        password=settings.goodwe_password or "",
        api_url=settings.goodwe_api_url,
        skipload=True,
    )

    if not inverter.do_login():
        return ProviderSnapshot(
            name="GoodWe",
            kind="solar",
            status="disconnected",
            detail="GoodWe SEMS login failed. Check username, password, Plant ID, or server region.",
            notes=[
                f"GoodWe API URL: {settings.goodwe_api_url}",
                "SEMS login failed before any plant data could be loaded.",
            ],
        )

    try:
        data = inverter.get_current_readings(raw=True, retry=1, maxretries=1, delay=1)
    except SystemExit:
        return ProviderSnapshot(
            name="GoodWe",
            kind="solar",
            status="disconnected",
            detail="GoodWe SEMS login succeeded, but no inverter data was returned for this Plant ID.",
            notes=[
                f"GoodWe API URL: {settings.goodwe_api_url}",
                "SEMS accepted the credentials, but the requested plant returned no inverter payload.",
            ],
        )
    info = data.get("info", {}) if isinstance(data, dict) else {}
    kpi = data.get("kpi", {}) if isinstance(data, dict) and isinstance(data.get("kpi"), dict) else {}
    equipment_rows = data.get("equipment", []) if isinstance(data, dict) and isinstance(data.get("equipment"), list) else []
    inverter_rows = _normalize_inverters(data.get("inverter")) if isinstance(data, dict) else []
    first_inverter = inverter_rows[0] if inverter_rows else {}
    first_equipment = equipment_rows[0] if equipment_rows else {}

    current_power = _safe_float(info.get("current_power") or info.get("currentPower"))
    if current_power is None:
        current_power = _parse_labeled_number(first_equipment.get("powerGeneration"))

    day_generation = _safe_float(info.get("day_power_generation") or info.get("eday"))
    if day_generation is None:
        day_generation = _safe_float(kpi.get("power"))

    total_generation = _safe_float(info.get("total_power_generation") or info.get("etotal"))
    if total_generation is None:
        total_generation = _safe_float(kpi.get("total_power"))

    battery_soc = _safe_float(
        first_inverter.get("invert_full", {}).get("soc")
        or first_inverter.get("soc")
        or first_inverter.get("battery")
        or _parse_labeled_number(first_equipment.get("soc"))
    )
    load_kw = None
    grid_kw = None
    battery_kw = _safe_float(first_inverter.get("pbattery") or first_inverter.get("battery_power"))
    battery_capacity = _safe_float(info.get("battery_capacity"))

    metrics = []
    if day_generation is not None:
        metrics.append({"label": "GoodWe Today", "value": round(day_generation, 2), "unit": "kWh", "tone": "good"})
    if total_generation is not None:
        metrics.append({"label": "GoodWe Total", "value": round(total_generation, 1), "unit": "kWh", "tone": "neutral"})
    if (battery_capacity or 0) > 0 and battery_soc is not None:
        metrics.append({"label": "GoodWe Battery", "value": round(battery_soc, 0), "unit": "%", "tone": "accent"})

    batteries = []
    if (battery_capacity or 0) > 0 or (battery_kw not in (None, 0, 0.0)):
        batteries.append(
            {
                "name": info.get("stationname") or "GoodWe Battery",
                "source": "GoodWe",
                "state_of_charge": battery_soc,
                "power_kw": battery_kw,
                "state": first_inverter.get("status") or info.get("status"),
                "health": "SEMS login",
            }
        )

    plants = [
        {
            "name": info.get("stationname") or "GoodWe Plant",
            "source": "GoodWe",
            "plant_id": settings.goodwe_plant_id,
            "status": str(info.get("status") or "Visible"),
            "plant_type": "SEMS",
            "timezone": None,
            "capacity_kw": _safe_float(info.get("nominal_power") or info.get("capacity")),
            "device_count": len(inverter_rows),
        }
    ]

    notes = [
        f"GoodWe API URL: {settings.goodwe_api_url}",
        "GoodWe data loaded via SEMS username/password login.",
    ]

    return ProviderSnapshot(
        name="GoodWe",
        kind="solar",
        status="connected",
        detail=f"Loaded GoodWe SEMS plant {settings.goodwe_plant_id}.",
        power_flow={
            "solar_kw": current_power or 0.0,
            "home_kw": load_kw or 0.0,
            "battery_kw": battery_kw or 0.0,
            "grid_kw": grid_kw or 0.0,
        },
        chart_series=[
            {
                "key": "solar_input_kw",
                "label": "Solar input",
                "unit": "kW",
                "color": "#f7c66b",
                "axis": "power",
                "points": [
                    {
                        "timestamp": datetime.now(timezone.utc),
                        "value": round(current_power or 0.0, 3),
                    }
                ],
            }
        ],
        batteries=batteries,
        plants=plants,
        metrics=metrics,
        notes=notes,
    )


async def load_goodwe_snapshot(settings: Settings) -> ProviderSnapshot | None:
    if not (settings.goodwe_username and settings.goodwe_password and settings.goodwe_plant_id):
        return None
    return await asyncio.to_thread(_load_goodwe_sync, settings)
