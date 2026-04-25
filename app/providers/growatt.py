from __future__ import annotations

from datetime import date
from typing import Any

from growatt_public_api import GrowattApi

from app.config import Settings
from app.providers.base import ProviderSnapshot


def _as_dict(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return item.model_dump()
    if hasattr(item, "dict"):
        return item.dict()
    if isinstance(item, dict):
        return item
    return {}


def _as_float(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _series_point(timestamp: Any, value: float | None, scale: float = 1.0, state: str | None = None) -> dict[str, Any] | None:
    if timestamp is None:
        return None
    scaled = None if value is None else round(value / scale, 3)
    point = {"timestamp": timestamp, "value": scaled}
    if state:
        point["state"] = state
    return point


async def load_growatt_snapshot(settings: Settings) -> ProviderSnapshot | None:
    if not settings.growatt_token:
        return None

    api = GrowattApi(token=settings.growatt_token, server_url=settings.growatt_server_url)
    plant_list = api.plant.list(page=1, limit=20)
    device_list = api.device.list(page=1)

    plants = []
    devices = []
    if getattr(plant_list, "data", None) and getattr(plant_list.data, "plants", None):
        plants = [_as_dict(plant) for plant in plant_list.data.plants]
    if getattr(device_list, "data", None) and getattr(device_list.data, "data", None):
        devices = [_as_dict(device) for device in device_list.data.data]

    notes = [
        f"Growatt server: {settings.growatt_server_url}",
        f"Growatt platform: {settings.growatt_platform}",
        f"Growatt token visibility: {len(plants)} plant(s), {len(devices)} device(s).",
    ]

    metrics = [
        {"label": "Growatt Plants", "value": len(plants), "unit": "", "tone": "neutral"},
        {"label": "Growatt Devices", "value": len(devices), "unit": "", "tone": "neutral"},
    ]

    batteries = []
    chart_series: list[dict[str, Any]] = []
    latest_solar_kw = 0.0
    latest_load_kw = 0.0
    latest_grid_import_kw = 0.0
    latest_battery_power_kw = 0.0
    plant_statuses = [
        {
            "name": plant.get("plant_name") or plant.get("plantName") or f"Plant {plant.get('plant_id') or plant.get('plantId')}",
            "source": "Growatt Hybrid",
            "plant_id": str(plant.get("plant_id") or plant.get("plantId") or ""),
            "status": str(plant.get("status") or plant.get("plant_status") or "Visible"),
            "plant_type": str(plant.get("plant_type") or plant.get("plantType") or "") or None,
            "timezone": plant.get("timezone") or plant.get("time_zone"),
            "capacity_kw": plant.get("nominal_power") or plant.get("plant_power"),
            "device_count": plant.get("device_count") or plant.get("deviceCount"),
        }
        for plant in plants
    ]

    if plants:
        plant_id = plant_statuses[0]["plant_id"]
        if plant_id:
            try:
                plant_power = api.plant.power(plant_id=int(plant_id))
                power_rows = getattr(getattr(plant_power, "data", None), "powers", None) or []
                solar_points = [
                    point
                    for row in sorted(power_rows, key=lambda item: getattr(item, "time", None) or 0)
                    if _as_float(getattr(row, "power", None)) is not None
                    and (point := _series_point(getattr(row, "time", None), _as_float(getattr(row, "power", None)), scale=1000.0))
                ]
                if solar_points:
                    latest_solar_kw = solar_points[-1]["value"] or 0.0
            except Exception:
                pass

            try:
                dataloggers = api.plant.list_dataloggers(plant_id=int(plant_id))
                datalogger_rows = getattr(getattr(dataloggers, "data", None), "dataloggers", None) or []
                if datalogger_rows:
                    datalogger_sn = getattr(datalogger_rows[0], "datalogger_sn", None)
                    if datalogger_sn:
                        smart_meters = api.datalogger.list_smart_meters(datalogger_sn=datalogger_sn)
                        meter_rows = getattr(getattr(smart_meters, "data", None), "meters", None) or []
                        if meter_rows:
                            meter_address = getattr(meter_rows[0], "address", None)
                            if meter_address is not None:
                                meter_history = api.smart_meter.energy_history(
                                    datalogger_sn=datalogger_sn,
                                    meter_address=meter_address,
                                    start_date=date.today(),
                                    end_date=date.today(),
                                    page=1,
                                    limit=100,
                                )
                                history_rows = getattr(getattr(meter_history, "data", None), "meter_data", None) or []
                                grid_points = [
                                    point
                                    for row in sorted(history_rows, key=lambda item: getattr(item, "time_text", None) or 0)
                                    if (
                                        point := _series_point(
                                            getattr(row, "time_text", None),
                                            max(_as_float(getattr(row, "active_power", None)) or 0.0, 0.0),
                                            scale=1000.0,
                                        )
                                    )
                                ]
                                if grid_points:
                                    latest_grid_import_kw = grid_points[-1]["value"] or 0.0
                                    chart_series.append(
                                        {
                                            "key": "grid_import_kw",
                                            "label": "Grid import",
                                            "unit": "kW",
                                            "color": "#ff8d7d",
                                            "axis": "power",
                                            "points": grid_points[-240:],
                                        }
                                    )
            except Exception:
                pass

    for device in devices:
        device_sn = device.get("device_sn") or device.get("sn")
        device_type = str(device.get("device_type") or device.get("type") or "")
        if not device_sn:
            continue
        if device_type not in {"2", "5", "sph", "storage"}:
            continue

        detail_payload = {}
        try:
            if device_type in {"5", "sph"}:
                energy = api.sph.energy_v4(device_sn=device_sn)
                energy_rows = getattr(getattr(energy, "data", None), "devices", None) or []
                detail_payload = _as_dict(energy_rows[0]) if energy_rows else {}

                history = api.sph.energy_history_v4(device_sn=device_sn, date_=date.today())
                history_rows = getattr(getattr(history, "data", None), "datas", None) or []
                ordered_rows = sorted(history_rows, key=lambda item: getattr(item, "time", None) or 0)

                load_points = [
                    point
                    for row in ordered_rows
                    if (
                        point := _series_point(
                            getattr(row, "time", None),
                            _as_float(getattr(row, "plocal_load_total", None)),
                            scale=1000.0,
                        )
                    )
                ]
                soc_points = [
                    point
                    for row in ordered_rows
                    if (
                        point := _series_point(
                            getattr(row, "time", None),
                            _as_float(getattr(row, "soc", None)),
                        )
                    )
                ]
                battery_charge_points = []
                battery_discharge_points = []
                for row in ordered_rows:
                    discharge = _as_float(getattr(row, "pdischarge1", None)) or 0.0
                    charge = _as_float(getattr(row, "pcharge1", None)) or 0.0
                    discharge_point = _series_point(
                        getattr(row, "time", None),
                        discharge if discharge > 0 else None,
                        scale=1000.0,
                    )
                    charge_point = _series_point(
                        getattr(row, "time", None),
                        charge if charge > 0 else None,
                        scale=1000.0,
                    )
                    if discharge_point:
                        battery_discharge_points.append(discharge_point)
                    if charge_point:
                        battery_charge_points.append(charge_point)

                if load_points:
                    latest_load_kw = load_points[-1]["value"] or 0.0
                    chart_series.append(
                        {
                            "key": "growatt_load_kw",
                            "label": "Load consumption",
                            "unit": "kW",
                            "color": "#61e6ff",
                            "axis": "power",
                            "points": load_points[-240:],
                        }
                    )
                if soc_points:
                    chart_series.append(
                        {
                            "key": "growatt_soc_pct",
                            "label": "Growatt battery SoC",
                            "unit": "%",
                            "color": "#8bf0b5",
                            "axis": "percent",
                            "points": soc_points[-240:],
                        }
                    )
                if battery_charge_points or battery_discharge_points:
                    latest_charge_kw = battery_charge_points[-1]["value"] if battery_charge_points else 0.0
                    latest_discharge_kw = battery_discharge_points[-1]["value"] if battery_discharge_points else 0.0
                    latest_battery_power_kw = (latest_charge_kw or 0.0) if (latest_charge_kw or 0.0) > 0 else -(latest_discharge_kw or 0.0)
                    chart_series.append(
                        {
                            "key": "growatt_battery_charge_kw",
                            "label": "Growatt battery charge",
                            "unit": "kW",
                            "color": "#4cc9f0",
                            "axis": "power",
                            "points": battery_charge_points[-240:],
                        }
                    )
                    chart_series.append(
                        {
                            "key": "growatt_battery_discharge_kw",
                            "label": "Growatt battery discharge",
                            "unit": "kW",
                            "color": "#ff9cf0",
                            "axis": "power",
                            "points": battery_discharge_points[-240:],
                        }
                    )
            else:
                details = api.storage.details_v4(device_sn=device_sn)
                detail_data = getattr(details, "data", None)
                detail_payload = _as_dict(detail_data)
        except Exception:
            detail_payload = {}

        battery_soc = (
            detail_payload.get("capacity")
            or detail_payload.get("soc")
            or detail_payload.get("SOC")
            or detail_payload.get("batterySoc")
            or detail_payload.get("bms_soc")
        )
        battery_kw = (
            (_as_float(detail_payload.get("pcharge1")) or 0.0) - (_as_float(detail_payload.get("pdischarge1")) or 0.0)
            or detail_payload.get("charge_power")
            or detail_payload.get("pacToUserr")
            or detail_payload.get("ppv")
        )
        battery_state = detail_payload.get("status_text") or detail_payload.get("status") or detail_payload.get("workMode") or detail_payload.get("systemStatus")

        batteries.append(
            {
                "name": device.get("device_name") or device.get("alias") or f"Growatt Hybrid {device_sn[-6:]}",
                "source": "Growatt Hybrid",
                "state_of_charge": _as_float(battery_soc),
                "power_kw": round((_as_float(battery_kw) or 0.0) / 1000.0, 3) if abs(_as_float(battery_kw) or 0.0) > 50 else _as_float(battery_kw),
                "state": battery_state or "Visible",
                "health": "Hybrid inverter token",
            }
        )

    status = "connected" if plants or devices else "disconnected"
    detail = (
        f"Growatt hybrid token can see {len(plants)} plant(s) and {len(devices)} device(s)."
        if plants or devices
        else "Growatt hybrid token is valid, but no plants or devices are visible on this server."
    )

    return ProviderSnapshot(
        name="Growatt Hybrid",
        kind="hybrid",
        status=status,
        detail=detail,
        power_flow={
            "solar_kw": latest_solar_kw,
            "home_kw": latest_load_kw,
            "battery_kw": latest_battery_power_kw,
            "grid_kw": latest_grid_import_kw,
        },
        batteries=batteries,
        plants=plant_statuses,
        metrics=metrics,
        chart_series=chart_series,
        notes=notes,
    )
