from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings, parse_csv
from app.providers.base import ProviderSnapshot
from app.services.tesla_oauth import get_valid_access_token


def _series_key(name: str, suffix: str) -> str:
    normalized = "".join(char.lower() if char.isalnum() else "_" for char in name)
    collapsed = "_".join(part for part in normalized.split("_") if part)
    return f"vehicle_{collapsed}_{suffix}"


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


async def _get_json(client: httpx.AsyncClient, url: str, token: str) -> Any:
    response = await client.get(url, headers=_auth_headers(token))
    response.raise_for_status()
    return response.json()


async def load_tesla_vehicle_snapshot(client: httpx.AsyncClient, settings: Settings) -> ProviderSnapshot | None:
    vins = parse_csv(settings.tesla_vehicle_vins)
    token = await get_valid_access_token(settings)
    if not token or not vins:
        return None

    vehicles: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    connected_vehicle_names: list[str] = []
    total_charge_power_kw = 0.0
    active_sessions = 0
    notes: list[str] = []
    any_vehicle_visible = False

    for vin in vins:
        meta_payload = await _get_json(
            client,
            f"{settings.tesla_api_base_url}/api/1/vehicles/{vin}",
            token,
        )
        meta = meta_payload.get("response", {})
        any_vehicle_visible = True
        vehicle_name = meta.get("display_name") or vin[-6:]
        vehicle_state_name = str(meta.get("state", "unknown")).lower()

        charge_state: dict[str, Any] = {}
        vehicle_state: dict[str, Any] = {"vehicle_name": vehicle_name}
        drive_state: dict[str, Any] = {}

        if vehicle_state_name == "online":
            try:
                payload = await _get_json(
                    client,
                    f"{settings.tesla_api_base_url}/api/1/vehicles/{vin}/vehicle_data",
                    token,
                )
                response = payload.get("response", {})
                charge_state = response.get("charge_state", {})
                vehicle_state = response.get("vehicle_state", {}) or {"vehicle_name": vehicle_name}
                drive_state = response.get("drive_state", {})
            except (httpx.HTTPStatusError, httpx.ReadTimeout) as exc:
                notes.append(f"Tesla live data timed out for {vehicle_name}; showing basic vehicle status only.")
        else:
            notes.append(f"{vehicle_name} is currently {vehicle_state_name}; Tesla did not provide live vehicle_data.")

        charger_power_kw = round(float(charge_state.get("charger_power", 0.0)), 2)
        charging_state = charge_state.get("charging_state") or vehicle_state_name.capitalize()
        plugged_in_raw = charge_state.get("conn_charge_cable")
        plugged_in = None if plugged_in_raw is None else plugged_in_raw != "<invalid>"

        vehicles.append(
            {
                "name": vehicle_name,
                "source": "Tesla Vehicle",
                "vin": vin,
                "battery_level": charge_state.get("battery_level"),
                "charging_state": charging_state,
                "charge_power_kw": charger_power_kw,
                "range_km": round(float(charge_state.get("battery_range", 0.0)) * 1.60934, 1)
                if charge_state.get("battery_range") is not None
                else None,
                "plugged_in": plugged_in,
                "location": "Home" if drive_state.get("native_latitude") else None,
            }
        )

        if plugged_in:
            connected_vehicle_names.append(vehicle_name)
        if charging_state == "Charging":
            active_sessions += 1
            total_charge_power_kw += charger_power_kw

        if charge_state.get("battery_level") is not None:
            metrics.append(
                {
                    "label": vehicle_state.get("vehicle_name", vin[-4:]),
                    "value": charge_state["battery_level"],
                    "unit": "%",
                    "tone": "warn" if charge_state.get("charging_state") == "Charging" else "neutral",
                }
            )

    timestamp = datetime.now(timezone.utc)
    vehicle_chart_series: list[dict[str, Any]] = []
    vehicle_palette = [
        ("#7db0ff", "#2bd9a0"),
        ("#f7c66b", "#ff7a90"),
        ("#c995ff", "#61e6ff"),
        ("#8bf0b5", "#ffa96b"),
    ]
    for index, vehicle in enumerate(vehicles):
        color_soc, color_charge = vehicle_palette[index % len(vehicle_palette)]
        vehicle_key = vehicle.get("name") or vehicle.get("vin") or f"tesla_{index + 1}"
        if vehicle.get("battery_level") is not None:
            vehicle_chart_series.append(
                {
                    "key": _series_key(vehicle_key, "soc_pct"),
                    "label": f"{vehicle['name']} SoC",
                    "unit": "%",
                    "color": color_soc,
                    "axis": "percent",
                    "points": [
                        {
                            "timestamp": timestamp,
                            "value": round(float(vehicle["battery_level"]), 2),
                        }
                    ],
                }
            )
        if vehicle.get("charge_power_kw") is not None:
            vehicle_chart_series.append(
                {
                    "key": _series_key(vehicle_key, "charge_kw"),
                    "label": f"{vehicle['name']} charge rate",
                    "unit": "kW",
                    "color": color_charge,
                    "axis": "power",
                    "points": [
                        {
                            "timestamp": timestamp,
                            "value": round(float(vehicle["charge_power_kw"]), 3),
                        }
                    ],
                }
            )

    charger_status = "Idle"
    if active_sessions:
        charger_status = "Charging"
    elif connected_vehicle_names:
        charger_status = "Connected"
    elif any_vehicle_visible:
        charger_status = "Disconnected"

    return ProviderSnapshot(
        name="Tesla Charging",
        kind="hybrid",
        status="connected" if any_vehicle_visible else "disconnected",
        detail=(
            f"Loaded {len(vehicles)} Tesla vehicle(s); Wall Connector state is inferred from vehicle availability and charge data."
            if any_vehicle_visible
            else "Tesla vehicle data is unavailable."
        ),
        chargers=[
            {
                "name": settings.wall_connector_name,
                "source": "Tesla Charging",
                "status": charger_status,
                "active_sessions": active_sessions,
                "connected_vehicles": len(connected_vehicle_names),
                "power_kw": round(total_charge_power_kw, 2),
                "max_power_kw": settings.wall_connector_max_kw,
                "circuit_amps": settings.wall_connector_circuit_amps,
                "location": settings.wall_connector_location,
                "vehicle_names": connected_vehicle_names,
            }
        ],
        vehicles=vehicles,
        metrics=metrics
        + [
            {
                "label": settings.wall_connector_name,
                "value": round(total_charge_power_kw, 2),
                "unit": "kW",
                "tone": "warn" if active_sessions else "neutral",
            }
        ],
        chart_series=[
            {
                "key": "tesla_ev_charge_kw",
                "label": "Tesla EV charging",
                "unit": "kW",
                "color": "#ff5fa2",
                "axis": "power",
                "points": [
                    {
                        "timestamp": timestamp,
                        "value": round(total_charge_power_kw, 3),
                    }
                ],
            }
        ]
        + vehicle_chart_series,
        notes=[
            "Wall Connector status is inferred from Tesla vehicle state and charge data.",
            "Tesla recommends Fleet Telemetry for efficient live monitoring instead of frequent vehicle_data polling.",
            *notes,
        ],
    )
