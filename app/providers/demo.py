from __future__ import annotations

from app.providers.base import ProviderSnapshot


async def load_demo_snapshot() -> ProviderSnapshot:
    return ProviderSnapshot(
        name="Demo",
        kind="hybrid",
        status="demo",
        detail="Demo values are active until live provider credentials are configured.",
        power_flow={
            "solar_kw": 8.4,
            "home_kw": 4.9,
            "battery_kw": -1.4,
            "grid_kw": -1.4,
        },
        batteries=[
            {
                "name": "Growatt Battery",
                "source": "Growatt",
                "state_of_charge": 71.0,
                "power_kw": -1.4,
                "state": "Charging",
                "health": "Normal",
            },
        ],
        chargers=[
            {
                "name": "Wall Connector 3",
                "source": "Tesla Charging",
                "status": "Charging",
                "active_sessions": 1,
                "connected_vehicles": 2,
                "power_kw": 7.2,
                "max_power_kw": 11.0,
                "circuit_amps": 32,
                "location": "Garage",
                "vehicle_names": ["Tesla Model 3", "Tesla Model Y"],
            }
        ],
        vehicles=[
            {
                "name": "Tesla Model Y",
                "source": "Tesla Vehicle",
                "vin": "5YJYGDEE*",
                "battery_level": 58,
                "charging_state": "Stopped",
                "charge_power_kw": 0.0,
                "range_km": 318.0,
                "plugged_in": True,
                "location": "Home",
            },
            {
                "name": "Tesla Model 3",
                "source": "Tesla Vehicle",
                "vin": "5YJ3E1EA*",
                "battery_level": 82,
                "charging_state": "Charging",
                "charge_power_kw": 7.2,
                "range_km": 412.0,
                "plugged_in": True,
                "location": "Home",
            },
        ],
        metrics=[
            {"label": "Today Solar", "value": 39.6, "unit": "kWh", "tone": "good"},
            {"label": "Home Load", "value": 4.9, "unit": "kW", "tone": "neutral"},
            {"label": "Grid Export", "value": 1.4, "unit": "kW", "tone": "accent"},
            {"label": "Wall Connector Load", "value": 7.2, "unit": "kW", "tone": "warn"},
        ],
        notes=[
            "Replace demo mode by adding Tesla OAuth and vendor API tokens in .env.",
            "Wall Connector behavior is inferred from Tesla vehicle charging state because Tesla Fleet API does not expose a separate charger device endpoint.",
            "Growatt and GoodWe adapters accept vendor-issued endpoints so they can match your installer account setup.",
        ],
    )
