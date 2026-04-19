from __future__ import annotations

from typing import Any

import httpx

from app.providers.base import ProviderSnapshot


def _pick_number(payload: Any, *keys: str) -> float | None:
    for key in keys:
        current = payload
        found = True
        for part in key.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                found = False
                break
        if found and isinstance(current, (int, float)):
            return float(current)
    return None


def _pick_text(payload: Any, *keys: str) -> str | None:
    for key in keys:
        current = payload
        found = True
        for part in key.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                found = False
                break
        if found and current is not None:
            return str(current)
    return None


async def _fetch_json(client: httpx.AsyncClient, url: str, token: str | None) -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


async def load_vendor_snapshot(
    client: httpx.AsyncClient,
    *,
    name: str,
    kind: str,
    overview_url: str | None,
    battery_url: str | None,
    token: str | None,
    notes: list[str] | None = None,
) -> ProviderSnapshot | None:
    if not overview_url and not battery_url:
        return None

    overview_payload = {}
    battery_payload = {}

    if overview_url:
        overview_payload = await _fetch_json(client, overview_url, token)
    if battery_url:
        battery_payload = await _fetch_json(client, battery_url, token)

    merged: dict[str, Any] = {}
    if isinstance(overview_payload, dict):
        merged.update(overview_payload)
    if isinstance(battery_payload, dict):
        merged["battery"] = battery_payload

    solar_kw = _pick_number(
        merged,
        "solar_kw",
        "ppv",
        "power.solar",
        "data.solarPower",
        "data.ppv",
        "overview.solar_kw",
    )
    home_kw = _pick_number(
        merged,
        "home_kw",
        "load_kw",
        "power.load",
        "data.loadPower",
        "data.housePower",
        "overview.home_kw",
    )
    grid_kw = _pick_number(
        merged,
        "grid_kw",
        "power.grid",
        "data.gridPower",
        "data.gridActivePower",
        "overview.grid_kw",
    )
    battery_soc = _pick_number(
        merged,
        "battery.soc",
        "battery.soc_percent",
        "soc",
        "data.batterySoc",
        "data.soc",
    )
    battery_kw = _pick_number(
        merged,
        "battery.power_kw",
        "battery_kw",
        "power.battery",
        "data.batteryPower",
    )
    battery_state = _pick_text(
        merged,
        "battery.status",
        "battery.state",
        "data.batteryStatus",
        "status",
    )

    metrics = []
    if solar_kw is not None:
        metrics.append({"label": f"{name} Solar", "value": round(solar_kw, 2), "unit": "kW", "tone": "good"})
    if battery_soc is not None:
        metrics.append({"label": f"{name} Battery", "value": round(battery_soc, 0), "unit": "%", "tone": "accent"})

    batteries = []
    if battery_soc is not None or battery_kw is not None:
        batteries.append(
            {
                "name": f"{name} Battery",
                "source": name,
                "state_of_charge": battery_soc,
                "power_kw": battery_kw,
                "state": battery_state,
                "health": "Live",
            }
        )

    return ProviderSnapshot(
        name=name,
        kind=kind,
        status="connected",
        detail=f"{name} data loaded from configured vendor endpoint.",
        power_flow={
            "solar_kw": solar_kw or 0.0,
            "home_kw": home_kw or 0.0,
            "battery_kw": battery_kw or 0.0,
            "grid_kw": grid_kw or 0.0,
        },
        batteries=batteries,
        metrics=metrics,
        notes=notes or [],
    )
