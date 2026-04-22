from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings, parse_csv
from app.services.automation_state import clamp_amps
from app.services.tesla_oauth import get_valid_access_token

MIN_VEHICLE_AMPS = 5
MAX_VEHICLE_AMPS = 30


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


async def _get_json(client: httpx.AsyncClient, url: str, token: str) -> Any:
    response = await client.get(url, headers=_auth_headers(token))
    response.raise_for_status()
    return response.json()


async def _post_command(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.post(url, headers=_auth_headers(token), json=payload or {})
    response.raise_for_status()
    body = response.json()
    if isinstance(body, dict):
        return body.get("response", body)
    return {"response": body}


def _allocate_combined_amps(total_amps: int, vehicle_count: int) -> tuple[list[int], int]:
    requested = max(MIN_VEHICLE_AMPS, min(MAX_VEHICLE_AMPS, int(total_amps)))
    if vehicle_count <= 0:
        return ([], 0)

    active_vehicle_count = min(vehicle_count, max(1, requested // MIN_VEHICLE_AMPS))
    base, remainder = divmod(requested, active_vehicle_count)
    allocations = [base + (1 if index < remainder else 0) for index in range(active_vehicle_count)]
    return (allocations, active_vehicle_count)


async def apply_manual_charge_request(settings: Settings, enabled: bool, target_amps: int) -> dict[str, Any]:
    vins = parse_csv(settings.tesla_vehicle_vins)
    token = await get_valid_access_token(settings)
    requested_amps = clamp_amps(target_amps)

    if not enabled:
        return {
            "applied": False,
            "target_amps": requested_amps,
            "detail": "Manual charge override was disabled, so no Tesla charge command was sent.",
        }

    if not token:
        return {
            "applied": False,
            "target_amps": requested_amps,
            "detail": "Tesla OAuth token is unavailable, so charge amps could not be applied.",
        }

    if not vins:
        return {
            "applied": False,
            "target_amps": requested_amps,
            "detail": "No Tesla vehicle VINs are configured.",
        }

    timeout = httpx.Timeout(settings.request_timeout_seconds, connect=settings.request_timeout_seconds)
    controllable: list[dict[str, Any]] = []
    notes: list[str] = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        for vin in vins:
            try:
                meta_payload = await _get_json(client, f"{settings.tesla_api_base_url}/api/1/vehicles/{vin}", token)
            except httpx.HTTPError as exc:
                notes.append(f"Tesla lookup failed for {vin}: {exc}")
                continue

            meta = meta_payload.get("response", {})
            vehicle_name = meta.get("display_name") or vin[-6:]
            vehicle_state_name = str(meta.get("state", "unknown")).lower()

            if vehicle_state_name != "online":
                notes.append(f"{vehicle_name} is {vehicle_state_name}; skipping manual charge command.")
                continue

            try:
                payload = await _get_json(client, f"{settings.tesla_api_base_url}/api/1/vehicles/{vin}/vehicle_data", token)
            except httpx.HTTPError as exc:
                notes.append(f"Tesla live data failed for {vehicle_name}: {exc}")
                continue

            response = payload.get("response", {})
            charge_state = response.get("charge_state", {}) or {}
            plugged_in_raw = charge_state.get("conn_charge_cable")
            plugged_in = plugged_in_raw is not None and plugged_in_raw != "<invalid>"
            charging_state = str(charge_state.get("charging_state") or "").lower()
            if plugged_in or charging_state in {"charging", "stopped", "complete"}:
                controllable.append(
                    {
                        "vin": vin,
                        "name": vehicle_name,
                    }
                )
            else:
                notes.append(f"{vehicle_name} is not connected to a charger.")

        if not controllable:
            return {
                "applied": False,
                "target_amps": requested_amps,
                "detail": "No plugged-in Tesla vehicles were available for manual charge control.",
                "notes": notes,
            }

        allocations, active_vehicle_count = _allocate_combined_amps(requested_amps, len(controllable))
        active_vehicles = controllable[:active_vehicle_count]
        parked_vehicles = controllable[active_vehicle_count:]
        command_results: list[dict[str, Any]] = []

        for vehicle, amps in zip(active_vehicles, allocations):
            try:
                set_result = await _post_command(
                    client,
                    f"{settings.tesla_api_base_url}/api/1/vehicles/{vehicle['vin']}/command/set_charging_amps",
                    token,
                    {"charging_amps": amps},
                )
                start_result = await _post_command(
                    client,
                    f"{settings.tesla_api_base_url}/api/1/vehicles/{vehicle['vin']}/command/charge_start",
                    token,
                )
            except httpx.HTTPError as exc:
                notes.append(f"Tesla command failed for {vehicle['name']}: {exc}")
                command_results.append(
                    {
                        "vehicle": vehicle["name"],
                        "amps": amps,
                        "error": str(exc),
                    }
                )
                continue
            command_results.append(
                {
                    "vehicle": vehicle["name"],
                    "amps": amps,
                    "set_charging_amps": set_result,
                    "charge_start": start_result,
                }
            )

        for vehicle in parked_vehicles:
            try:
                stop_result = await _post_command(
                    client,
                    f"{settings.tesla_api_base_url}/api/1/vehicles/{vehicle['vin']}/command/charge_stop",
                    token,
                )
            except httpx.HTTPError as exc:
                notes.append(f"Tesla charge stop failed for {vehicle['name']}: {exc}")
                command_results.append(
                    {
                        "vehicle": vehicle["name"],
                        "amps": 0,
                        "error": str(exc),
                    }
                )
                continue
            command_results.append(
                {
                    "vehicle": vehicle["name"],
                    "amps": 0,
                    "charge_stop": stop_result,
                }
            )

    return {
        "applied": True,
        "target_amps": requested_amps,
        "active_vehicle_count": active_vehicle_count,
        "detail": f"Applied a combined {requested_amps}A target across {active_vehicle_count} Tesla vehicle(s).",
        "commands": command_results,
        "notes": notes,
    }
