from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone

import httpx

from app.config import Settings, parse_csv
from app.models import BatteryStatus, ChargerStatus, DashboardData, EnergyChartSeries, PlantStatus, PowerFlow, SourceStatus, SummaryMetric, TeslaMateCard, VehicleStatus
from app.providers.goodwe import load_goodwe_snapshot
from app.providers.growatt import load_growatt_snapshot
from app.providers.generic_vendor import load_vendor_snapshot
from app.providers.tesla import load_tesla_vehicle_snapshot
from app.services.chart_history import load_chart_history, store_chart_history
from app.services.automation_state import build_automation_panel
from app.services.teslamate import load_teslamate_chart_history, load_teslamate_dashboard_cards

NON_TESLA_SNAPSHOT_TTL = timedelta(minutes=1)
_NON_TESLA_SNAPSHOT_CACHE: dict[str, object | None] = {
    "captured_at": None,
    "cache_key": None,
    "snapshots": None,
}


def _build_non_tesla_cache_key(settings: Settings) -> str:
    return "|".join(
        [
            settings.growatt_overview_url or "",
            settings.growatt_battery_url or "",
            settings.growatt_token or "",
            settings.growatt_server_url,
            settings.growatt_platform,
            settings.goodwe_overview_url or "",
            settings.goodwe_battery_url or "",
            settings.goodwe_token or "",
            settings.goodwe_username or "",
            settings.goodwe_password or "",
            settings.goodwe_plant_id or "",
            settings.goodwe_api_url,
        ]
    )


def _get_cached_non_tesla_snapshots(cache_key: str):
    captured_at = _NON_TESLA_SNAPSHOT_CACHE.get("captured_at")
    snapshots = _NON_TESLA_SNAPSHOT_CACHE.get("snapshots")
    if (
        _NON_TESLA_SNAPSHOT_CACHE.get("cache_key") != cache_key
        or captured_at is None
        or snapshots is None
        or datetime.now(timezone.utc) - captured_at > NON_TESLA_SNAPSHOT_TTL
    ):
        return None
    return copy.deepcopy(snapshots)


def _store_cached_non_tesla_snapshots(cache_key: str, snapshots) -> None:
    _NON_TESLA_SNAPSHOT_CACHE["cache_key"] = cache_key
    _NON_TESLA_SNAPSHOT_CACHE["captured_at"] = datetime.now(timezone.utc)
    _NON_TESLA_SNAPSHOT_CACHE["snapshots"] = copy.deepcopy(snapshots)


def _build_teslamate_vehicle_fallbacks(series_list: list[EnergyChartSeries]) -> dict[str, dict[str, float]]:
    vehicle_states: dict[str, dict[str, float]] = {}
    for series in series_list:
        if not series.key.startswith("vehicle_") or not series.points:
            continue
        latest_point = max(series.points, key=lambda point: point.timestamp)
        if latest_point.value is None:
            continue
        if series.label.endswith(" SoC"):
            vehicle_name = series.label.removesuffix(" SoC")
            vehicle_states.setdefault(vehicle_name, {})["battery_level"] = float(latest_point.value)
        elif series.label.endswith(" charge rate"):
            vehicle_name = series.label.removesuffix(" charge rate")
            vehicle_states.setdefault(vehicle_name, {})["charge_power_kw"] = float(latest_point.value)
    return vehicle_states


def _merge_teslamate_vehicle_fallbacks(
    vehicles: list[VehicleStatus],
    teslamate_series: list[EnergyChartSeries],
) -> list[VehicleStatus]:
    teslamate_fallbacks = _build_teslamate_vehicle_fallbacks(teslamate_series)
    if not teslamate_fallbacks:
        return vehicles

    merged_vehicles: list[VehicleStatus] = []
    seen_names: set[str] = set()
    for vehicle in vehicles:
        fallback = teslamate_fallbacks.get(vehicle.name)
        if fallback:
            seen_names.add(vehicle.name)
            if vehicle.battery_level is None and fallback.get("battery_level") is not None:
                vehicle.battery_level = round(fallback["battery_level"])
            if vehicle.charge_power_kw is None and fallback.get("charge_power_kw") is not None:
                vehicle.charge_power_kw = round(fallback["charge_power_kw"], 2)
        merged_vehicles.append(vehicle)

    for vehicle_name, fallback in teslamate_fallbacks.items():
        if vehicle_name in seen_names:
            continue
        merged_vehicles.append(
            VehicleStatus(
                name=vehicle_name,
                source="Tesla Vehicle",
                battery_level=round(fallback["battery_level"]) if fallback.get("battery_level") is not None else None,
                charging_state="Last known",
                charge_power_kw=round(fallback["charge_power_kw"], 2) if fallback.get("charge_power_kw") is not None else None,
            )
        )
    return merged_vehicles


def _build_vehicle_chart_series(
    base_series: list[EnergyChartSeries],
    teslamate_series: list[EnergyChartSeries],
) -> list[EnergyChartSeries]:
    teslamate_vehicle_series = [series for series in teslamate_series if series.key.startswith("vehicle_")]
    if teslamate_vehicle_series:
        return teslamate_vehicle_series
    return [series for series in base_series if series.key.startswith("vehicle_")]


async def _load_non_tesla_snapshots(settings: Settings):
    cache_key = _build_non_tesla_cache_key(settings)
    cached_snapshots = _get_cached_non_tesla_snapshots(cache_key)
    if cached_snapshots is not None:
        return cached_snapshots

    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        snapshots = await asyncio.gather(
            load_vendor_snapshot(
                client,
                name="Growatt",
                kind="hybrid",
                overview_url=settings.growatt_overview_url,
                battery_url=settings.growatt_battery_url,
                token=settings.growatt_token,
                notes=[
                    "Growatt installer documentation references API tokens issued from ShineServer/OSS account management.",
                ],
            )
            if settings.growatt_overview_url or settings.growatt_battery_url
            else load_growatt_snapshot(settings),
            load_vendor_snapshot(
                client,
                name="GoodWe",
                kind="solar",
                overview_url=settings.goodwe_overview_url,
                battery_url=settings.goodwe_battery_url,
                token=settings.goodwe_token,
                notes=[
                    "GoodWe SEMS open API access is organization-account based and vendor-enabled.",
                ],
            )
            if settings.goodwe_overview_url or settings.goodwe_battery_url
            else load_goodwe_snapshot(settings),
            return_exceptions=True,
        )

    _store_cached_non_tesla_snapshots(cache_key, snapshots)
    return snapshots


async def build_dashboard_data(settings: Settings) -> DashboardData:
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    tesla_snapshot = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            tesla_snapshot = await load_tesla_vehicle_snapshot(client, settings)
        except Exception as exc:
            tesla_snapshot = exc

    snapshots = [tesla_snapshot, *await _load_non_tesla_snapshots(settings)]

    power_flow = PowerFlow()
    metrics: list[SummaryMetric] = []
    energy_chart: list[EnergyChartSeries] = []
    batteries: list[BatteryStatus] = []
    chargers: list[ChargerStatus] = []
    plants: list[PlantStatus] = []
    vehicles: list[VehicleStatus] = []
    sources: list[SourceStatus] = []
    notes: list[str] = []
    successful_snapshot = False

    for snapshot in snapshots:
        if snapshot is None:
            continue
        if isinstance(snapshot, Exception):
            notes.append(f"One provider failed to load: {snapshot}")
            continue

        successful_snapshot = True
        power_flow.solar_kw += snapshot.power_flow.get("solar_kw", 0.0)
        power_flow.home_kw += snapshot.power_flow.get("home_kw", 0.0)
        power_flow.battery_kw += snapshot.power_flow.get("battery_kw", 0.0)
        power_flow.grid_kw += snapshot.power_flow.get("grid_kw", 0.0)

        metrics.extend(SummaryMetric(**metric) for metric in snapshot.metrics)
        energy_chart.extend(EnergyChartSeries(**series) for series in snapshot.chart_series)
        batteries.extend(BatteryStatus(**battery) for battery in snapshot.batteries)
        chargers.extend(ChargerStatus(**charger) for charger in snapshot.chargers)
        plants.extend(PlantStatus(**plant) for plant in snapshot.plants)
        vehicles.extend(VehicleStatus(**vehicle) for vehicle in snapshot.vehicles)
        sources.append(
            SourceStatus(
                name=snapshot.name,
                kind=snapshot.kind,
                status=snapshot.status,
                detail=snapshot.detail,
            )
        )
        notes.extend(snapshot.notes)

    if not successful_snapshot:
        tesla_configured = bool(settings.tesla_access_token or settings.tesla_client_id)
        growatt_configured = bool(settings.growatt_overview_url or settings.growatt_battery_url)
        goodwe_configured = bool(
            settings.goodwe_overview_url
            or settings.goodwe_battery_url
            or (settings.goodwe_username and settings.goodwe_password and settings.goodwe_plant_id)
        )

        chargers = [
            ChargerStatus(
                name=settings.wall_connector_name,
                source="Tesla Charging",
                status="Disconnected",
                active_sessions=0,
                connected_vehicles=0,
                power_kw=0.0,
                max_power_kw=settings.wall_connector_max_kw,
                circuit_amps=settings.wall_connector_circuit_amps,
                location=settings.wall_connector_location,
                vehicle_names=[],
            )
        ]

        sources = [
            SourceStatus(
                name="Tesla Charging",
                kind="hybrid",
                status="disconnected",
                detail=(
                    "Tesla OAuth token is missing or invalid."
                    if tesla_configured
                    else "Tesla credentials are not configured."
                ),
            ),
            SourceStatus(
                name="Growatt",
                kind="hybrid",
                status="disconnected",
                detail=(
                    "Growatt endpoint did not return live data."
                    if growatt_configured
                    else "Growatt endpoint is not configured."
                ),
            ),
            SourceStatus(
                name="GoodWe",
                kind="solar",
                status="disconnected",
                detail=(
                    "GoodWe endpoint did not return live data."
                    if goodwe_configured
                    else "GoodWe endpoint is not configured."
                ),
            ),
        ]

        notes = [
            "Mock data is disabled. This dashboard now shows only live provider data.",
            "Tesla Wall Connector remains disconnected until OAuth completes and a valid token is stored.",
        ]
        if parse_csv(settings.tesla_vehicle_vins):
            notes.append("Tesla VINs are configured, but vehicle data is unavailable until authentication succeeds.")

    metrics = metrics[:8]

    live_energy_chart = list(energy_chart)
    persisted_energy_chart: list[EnergyChartSeries] = []
    teslamate_series: list[EnergyChartSeries] = []
    teslamate_cards: list[TeslaMateCard] = []
    try:
        store_chart_history(settings, live_energy_chart)
        persisted_energy_chart = load_chart_history(settings)
    except Exception as exc:
        notes.append(f"Chart history is temporarily unavailable: {exc}")

    preferred_vehicle_names = {
        vehicle.vin: vehicle.name
        for vehicle in vehicles
        if vehicle.source == "Tesla Vehicle" and vehicle.vin and vehicle.name
    }
    teslamate_series, teslamate_notes = load_teslamate_chart_history(
        settings,
        preferred_vehicle_names=preferred_vehicle_names,
    )
    notes.extend(teslamate_notes)
    teslamate_cards, teslamate_card_notes = load_teslamate_dashboard_cards(
        settings,
        preferred_vehicle_names=preferred_vehicle_names,
    )
    notes.extend(teslamate_card_notes)
    vehicles = _merge_teslamate_vehicle_fallbacks(vehicles, teslamate_series)
    vehicle_chart = _build_vehicle_chart_series(persisted_energy_chart or live_energy_chart, teslamate_series)

    dashboard = DashboardData(
        site_name=settings.dashboard_title,
        refresh_interval_seconds=settings.refresh_interval_seconds,
        power_flow=power_flow,
        summary_metrics=metrics,
        energy_chart=(persisted_energy_chart or live_energy_chart) + teslamate_series,
        vehicle_chart=vehicle_chart,
        batteries=batteries,
        chargers=chargers,
        plants=plants,
        vehicles=vehicles,
        teslamate_cards=teslamate_cards,
        sources=sources,
        notes=notes,
    )
    automation_dashboard = dashboard.model_copy(deep=True)
    automation_dashboard.energy_chart = live_energy_chart
    dashboard.automation_panel = build_automation_panel(automation_dashboard)
    return dashboard
