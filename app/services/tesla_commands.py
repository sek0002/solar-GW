from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import Settings, parse_csv
from app.models import AutomationPanel
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


async def _wait_for_vehicle_online(
    client: httpx.AsyncClient,
    settings: Settings,
    vin: str,
    token: str,
    attempts: int = 9,
    delay_seconds: float = 5.0,
) -> tuple[dict[str, Any] | None, str]:
    last_meta: dict[str, Any] | None = None
    last_state = "unknown"
    for attempt in range(attempts):
        meta_payload = await _get_json(client, f"{settings.tesla_api_base_url}/api/1/vehicles/{vin}", token)
        last_meta = meta_payload.get("response", {})
        last_state = str(last_meta.get("state", "unknown")).lower()
        if last_state == "online":
            return (last_meta, last_state)
        if attempt < attempts - 1:
            await asyncio.sleep(delay_seconds)
    return (last_meta, last_state)


def _allocate_combined_amps(total_amps: int, vehicle_count: int) -> tuple[list[int], int]:
    requested = max(MIN_VEHICLE_AMPS, min(MAX_VEHICLE_AMPS, int(total_amps)))
    if vehicle_count <= 0:
        return ([], 0)

    active_vehicle_count = min(vehicle_count, max(1, requested // MIN_VEHICLE_AMPS))
    base, remainder = divmod(requested, active_vehicle_count)
    allocations = [base + (1 if index < remainder else 0) for index in range(active_vehicle_count)]
    return (allocations, active_vehicle_count)


async def _discover_controllable_vehicles(
    client: httpx.AsyncClient,
    settings: Settings,
    vins: list[str],
    token: str,
    notes: list[str],
) -> list[dict[str, str]]:
    controllable: list[dict[str, str]] = []
    for vin in vins:
        try:
            meta_payload = await _get_json(client, f"{settings.tesla_api_base_url}/api/1/vehicles/{vin}", token)
        except httpx.HTTPError as exc:
            notes.append(f"Tesla lookup failed for {vin}: {exc}")
            continue

        meta = meta_payload.get("response", {})
        vehicle_name = meta.get("display_name") or vin[-6:]
        vehicle_state_name = str(meta.get("state", "unknown")).lower()

        if vehicle_state_name in {"asleep", "offline"}:
            notes.append(f"Waking {vehicle_name} from {vehicle_state_name}.")
            try:
                await _post_command(
                    client,
                    f"{settings.tesla_api_base_url}/api/1/vehicles/{vin}/wake_up",
                    token,
                )
                meta, vehicle_state_name = await _wait_for_vehicle_online(client, settings, vin, token)
                vehicle_name = (meta or {}).get("display_name") or vehicle_name
                if vehicle_state_name != "online":
                    notes.append(f"{vehicle_name} did not come online after wake-up (state: {vehicle_state_name}).")
                    continue
                notes.append(f"{vehicle_name} is online after wake-up.")
            except httpx.HTTPError as exc:
                notes.append(f"Wake-up failed for {vehicle_name}: {exc}")
                continue
        elif vehicle_state_name != "online":
            notes.append(f"{vehicle_name} is {vehicle_state_name}; skipping charge command.")
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

    return controllable


async def _apply_charge_target(settings: Settings, target_amps: int | None, detail: str) -> dict[str, Any]:
    vins = parse_csv(settings.tesla_vehicle_vins)
    token = await get_valid_access_token(settings)
    requested_amps = clamp_amps(target_amps) if target_amps is not None else None
    command_base_url = (settings.tesla_vehicle_command_proxy_url or "").rstrip("/")

    if not token:
        return {
            "applied": False,
            "status": "error",
            "target_amps": requested_amps,
            "detail": "Tesla OAuth token is unavailable, so charge amps could not be applied.",
        }

    if not vins:
        return {
            "applied": False,
            "status": "error",
            "target_amps": requested_amps,
            "detail": "No Tesla vehicle VINs are configured.",
        }

    if not command_base_url:
        return {
            "applied": False,
            "status": "error",
            "target_amps": requested_amps,
            "detail": "Tesla Vehicle Command Proxy is not configured. Set TESLA_VEHICLE_COMMAND_PROXY_URL to send signed vehicle charge commands.",
        }

    timeout = httpx.Timeout(settings.request_timeout_seconds, connect=settings.request_timeout_seconds)
    notes: list[str] = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        controllable = await _discover_controllable_vehicles(client, settings, vins, token, notes)

        if not controllable:
            return {
                "applied": False,
                "status": "idle" if requested_amps is None else "error",
                "target_amps": requested_amps,
                "detail": "No plugged-in Tesla vehicles were available for charge control.",
                "notes": notes,
            }

        command_results: list[dict[str, Any]] = []
        successful_commands = 0
        failed_commands = 0

        if requested_amps is None:
            active_vehicle_count = 0
            for vehicle in controllable:
                try:
                    stop_result = await _post_command(
                        client,
                        f"{command_base_url}/api/1/vehicles/{vehicle['vin']}/command/charge_stop",
                        token,
                    )
                except httpx.HTTPError as exc:
                    notes.append(f"Tesla charge stop failed for {vehicle['name']}: {exc}")
                    failed_commands += 1
                    command_results.append(
                        {
                            "vehicle": vehicle["name"],
                            "amps": 0,
                            "error": str(exc),
                        }
                    )
                    continue
                successful_commands += 1
                command_results.append(
                    {
                        "vehicle": vehicle["name"],
                        "amps": 0,
                        "charge_stop": stop_result,
                    }
                )
        else:
            allocations, active_vehicle_count = _allocate_combined_amps(requested_amps, len(controllable))
            active_vehicles = controllable[:active_vehicle_count]
            parked_vehicles = controllable[active_vehicle_count:]

            for vehicle, amps in zip(active_vehicles, allocations):
                try:
                    set_result = await _post_command(
                        client,
                        f"{command_base_url}/api/1/vehicles/{vehicle['vin']}/command/set_charging_amps",
                        token,
                        {"charging_amps": amps},
                    )
                    start_result = await _post_command(
                        client,
                        f"{command_base_url}/api/1/vehicles/{vehicle['vin']}/command/charge_start",
                        token,
                    )
                except httpx.HTTPError as exc:
                    notes.append(f"Tesla command failed for {vehicle['name']}: {exc}")
                    failed_commands += 1
                    command_results.append(
                        {
                            "vehicle": vehicle["name"],
                            "amps": amps,
                            "error": str(exc),
                        }
                    )
                    continue
                successful_commands += 1
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
                        f"{command_base_url}/api/1/vehicles/{vehicle['vin']}/command/charge_stop",
                        token,
                    )
                except httpx.HTTPError as exc:
                    notes.append(f"Tesla charge stop failed for {vehicle['name']}: {exc}")
                    failed_commands += 1
                    command_results.append(
                        {
                            "vehicle": vehicle["name"],
                            "amps": 0,
                            "error": str(exc),
                        }
                    )
                    continue
                successful_commands += 1
                command_results.append(
                    {
                        "vehicle": vehicle["name"],
                        "amps": 0,
                        "charge_stop": stop_result,
                    }
                )

    status = "success"
    if successful_commands and failed_commands:
        status = "partial"
    elif failed_commands and not successful_commands:
        status = "error"

    return {
        "applied": successful_commands > 0,
        "status": status,
        "target_amps": requested_amps,
        "active_vehicle_count": active_vehicle_count,
        "detail": (
            detail
            if successful_commands > 0
            else "Tesla charge commands did not complete successfully."
        ),
        "commands": command_results,
        "notes": notes,
    }


async def apply_manual_charge_request(settings: Settings, enabled: bool, target_amps: int) -> dict[str, Any]:
    if not enabled:
        return await _apply_charge_target(
            settings,
            None,
            "Manual charge override was disabled and Tesla charging was stopped for connected vehicles.",
        )

    requested_amps = clamp_amps(target_amps)
    return await _apply_charge_target(
        settings,
        requested_amps,
        f"Applied a combined {requested_amps}A manual target through the Tesla Vehicle Command Proxy.",
    )


async def enforce_charge_stop_for_panel(settings: Settings, panel: AutomationPanel) -> dict[str, Any]:
    return await _apply_charge_target(
        settings,
        None,
        f"{panel.effective_mode} requested charging pause for connected Tesla vehicles.",
    )


async def apply_automation_panel(settings: Settings, panel: AutomationPanel) -> dict[str, Any]:
    if panel.effective_target_amps is None:
        return {
            "applied": False,
            "status": "idle",
            "target_amps": None,
            "detail": f"{panel.effective_mode} does not currently require a Tesla charge target.",
        }

    requested_amps = clamp_amps(panel.effective_target_amps)
    return await _apply_charge_target(
        settings,
        requested_amps,
        f"{panel.effective_mode} applied a combined {requested_amps}A target through the Tesla Vehicle Command Proxy.",
    )
