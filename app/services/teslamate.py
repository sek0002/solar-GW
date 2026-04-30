from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from app.config import Settings, parse_csv
from app.models import EnergyChartPoint, EnergyChartSeries, TeslaMateCard


def _series_key(name: str, suffix: str) -> str:
    normalized = "".join(char.lower() if char.isalnum() else "_" for char in name)
    collapsed = "_".join(part for part in normalized.split("_") if part)
    return f"vehicle_{collapsed}_{suffix}"


def get_teslamate_dashboards_url(settings: Settings) -> str | None:
    if settings.teslamate_dashboards_url:
        return settings.teslamate_dashboards_url.rstrip("/")
    if settings.teslamate_grafana_url:
        return f"{settings.teslamate_grafana_url.rstrip('/')}/dashboards"
    return None


def teslamate_dashboards_enabled(settings: Settings) -> bool:
    return bool(get_teslamate_dashboards_url(settings))


def _build_maps_url(latitude: float | None, longitude: float | None) -> str | None:
    if latitude is None or longitude is None:
        return None
    return f"https://www.google.com/maps?q={latitude},{longitude}"


def _build_map_embed_url(latitude: float | None, longitude: float | None) -> str | None:
    if latitude is None or longitude is None:
        return None
    delta = 0.0035
    bbox = f"{longitude - delta},{latitude - delta},{longitude + delta},{latitude + delta}"
    query = urlencode(
        {
            "bbox": bbox,
            "layer": "mapnik",
            "marker": f"{latitude},{longitude}",
        }
    )
    return f"https://www.openstreetmap.org/export/embed.html?{query}"


def _humanize_status_duration(start_date: datetime | None) -> str | None:
    if start_date is None:
        return None
    seconds = int((datetime.now(timezone.utc) - start_date.astimezone(timezone.utc)).total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 24:
        remainder = minutes % 60
        return f"{hours} h" if remainder == 0 else f"{hours} h {remainder} min"
    days = hours // 24
    remainder_hours = hours % 24
    return f"{days} d" if remainder_hours == 0 else f"{days} d {remainder_hours} h"


def load_teslamate_chart_history(
    settings: Settings,
    preferred_vehicle_names: dict[str, str] | None = None,
) -> tuple[list[EnergyChartSeries], list[str]]:
    if not settings.teslamate_postgres_dsn:
        return ([], [])

    try:
        import psycopg
    except ImportError:
        return ([], ["TeslaMate history is configured, but the psycopg dependency is not installed."])

    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.chart_history_retention_hours)
    preferred_vehicle_names = preferred_vehicle_names or {}
    vins = parse_csv(settings.tesla_vehicle_vins)
    series_points: dict[str, list[EnergyChartPoint]] = defaultdict(list)
    series_meta: dict[str, dict[str, str]] = {}
    aggregate_charge_points: dict[datetime, float] = defaultdict(float)

    def resolve_vehicle_name(vin: str | None, fallback: str | None, car_id: int) -> str:
        if vin and preferred_vehicle_names.get(vin):
            return preferred_vehicle_names[vin]
        if fallback:
            return fallback
        if vin:
            return vin[-6:]
        return f"Tesla {car_id}"

    def register_point(
        vehicle_name: str,
        suffix: str,
        label_suffix: str,
        unit: str,
        color: str,
        axis: str,
        timestamp: datetime,
        value: float | None,
    ) -> None:
        if value is None:
            return
        key = _series_key(vehicle_name, suffix)
        if key not in series_meta:
            series_meta[key] = {
                "label": f"{vehicle_name} {label_suffix}",
                "unit": unit,
                "color": color,
                "axis": axis,
            }
        series_points[key].append(
            EnergyChartPoint(
                timestamp=timestamp.astimezone(timezone.utc),
                value=round(float(value), 3),
            )
        )

    vin_filter_sql = ""
    vin_params: list[object] = []
    if vins:
        vin_filter_sql = " AND c.vin = ANY(%s)"
        vin_params.append(vins)

    soc_sql = f"""
        SELECT
            c.id,
            c.vin,
            c.name,
            date_bin(INTERVAL '2 minutes', timezone('UTC', p.date), TIMESTAMPTZ '2001-01-01 00:00:00+00') AS bucket,
            avg(p.battery_level)::float AS battery_level
        FROM positions p
        INNER JOIN cars c ON c.id = p.car_id
        WHERE p.date >= %s
          AND p.battery_level IS NOT NULL
          AND p.ideal_battery_range_km IS NOT NULL
          {vin_filter_sql}
        GROUP BY c.id, c.vin, c.name, bucket
        ORDER BY c.id, bucket
    """
    charge_sql = f"""
        SELECT
            c.id,
            c.vin,
            c.name,
            date_bin(INTERVAL '2 minutes', timezone('UTC', ch.date), TIMESTAMPTZ '2001-01-01 00:00:00+00') AS bucket,
            avg(ch.charger_power)::float AS charger_power
        FROM charges ch
        INNER JOIN charging_processes cp ON cp.id = ch.charging_process_id
        INNER JOIN cars c ON c.id = cp.car_id
        WHERE ch.date >= %s
          AND ch.charger_power IS NOT NULL
          {vin_filter_sql}
        GROUP BY c.id, c.vin, c.name, bucket
        ORDER BY c.id, bucket
    """

    try:
        with psycopg.connect(settings.teslamate_postgres_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(soc_sql, [cutoff, *vin_params])
                for car_id, vin, fallback_name, bucket, battery_level in cursor.fetchall():
                    vehicle_name = resolve_vehicle_name(vin, fallback_name, car_id)
                    register_point(vehicle_name, "soc_pct", "SoC", "%", "#7db0ff", "percent", bucket, battery_level)

                cursor.execute(charge_sql, [cutoff, *vin_params])
                for car_id, vin, fallback_name, bucket, charger_power in cursor.fetchall():
                    vehicle_name = resolve_vehicle_name(vin, fallback_name, car_id)
                    register_point(vehicle_name, "charge_kw", "charge rate", "kW", "#2bd9a0", "power", bucket, charger_power)
                    if charger_power is not None:
                        aggregate_charge_points[bucket.astimezone(timezone.utc)] += float(charger_power)
    except Exception as exc:
        return ([], [f"TeslaMate history is temporarily unavailable: {exc}"])

    if aggregate_charge_points:
        series_meta["tesla_ev_charge_kw"] = {
            "label": "Tesla EV charging",
            "unit": "kW",
            "color": "#ff5fa2",
            "axis": "power",
        }
        series_points["tesla_ev_charge_kw"] = [
            EnergyChartPoint(
                timestamp=timestamp,
                value=round(value, 3),
            )
            for timestamp, value in sorted(aggregate_charge_points.items())
        ]

    series_list = [
        EnergyChartSeries(
            key=key,
            label=meta["label"],
            unit=meta["unit"],
            color=meta["color"],
            axis=meta["axis"],
            points=sorted(series_points[key], key=lambda point: point.timestamp),
        )
        for key, meta in series_meta.items()
        if series_points.get(key)
    ]
    notes = []
    if series_list:
        notes.append("TeslaMate charge and SoC history is being merged into the dashboard charts.")
    return (series_list, notes)


def load_teslamate_dashboard_cards(
    settings: Settings,
    preferred_vehicle_names: dict[str, str] | None = None,
) -> tuple[list[TeslaMateCard], list[str]]:
    if not settings.teslamate_postgres_dsn:
        return ([], [])

    try:
        import psycopg
    except ImportError:
        return ([], ["TeslaMate cards are configured, but the psycopg dependency is not installed."])

    preferred_vehicle_names = preferred_vehicle_names or {}
    vins = parse_csv(settings.tesla_vehicle_vins)

    vin_filter_sql = ""
    vin_params: list[object] = []
    if vins:
        vin_filter_sql = " WHERE c.vin = ANY(%s)"
        vin_params.append(vins)

    sql = f"""
        WITH selected_cars AS (
            SELECT
                c.id,
                c.vin,
                c.name,
                c.model,
                c.trim_badging,
                c.marketing_name,
                c.display_priority
            FROM cars c
            {vin_filter_sql}
        ),
        latest_positions AS (
            SELECT DISTINCT ON (p.car_id)
                p.car_id,
                timezone('UTC', p.date) AS date,
                p.latitude::float8 AS latitude,
                p.longitude::float8 AS longitude,
                p.battery_level::float8 AS battery_level,
                p.usable_battery_level::float8 AS usable_battery_level,
                p.rated_battery_range_km::float8 AS rated_battery_range_km,
                p.est_battery_range_km::float8 AS est_battery_range_km,
                p.outside_temp::float8 AS outside_temp,
                p.inside_temp::float8 AS inside_temp,
                p.odometer::float8 AS odometer_km
            FROM positions p
            INNER JOIN selected_cars sc ON sc.id = p.car_id
            ORDER BY p.car_id, p.date DESC
        ),
        current_states AS (
            SELECT DISTINCT ON (s.car_id)
                s.car_id,
                s.state::text AS state,
                timezone('UTC', s.start_date) AS start_date
            FROM states s
            INNER JOIN selected_cars sc ON sc.id = s.car_id
            ORDER BY s.car_id, COALESCE(s.end_date, s.start_date) DESC, s.start_date DESC
        ),
        latest_updates AS (
            SELECT DISTINCT ON (u.car_id)
                u.car_id,
                u.version
            FROM updates u
            INNER JOIN selected_cars sc ON sc.id = u.car_id
            ORDER BY u.car_id, COALESCE(u.end_date, u.start_date) DESC, u.start_date DESC
        ),
        latest_vehicle_lock AS (
            SELECT DISTINCT ON (p.car_id)
                p.car_id,
                p.date,
                NULL::boolean AS locked
            FROM positions p
            INNER JOIN selected_cars sc ON sc.id = p.car_id
            ORDER BY p.car_id, p.date DESC
        ),
        latest_charges AS (
            SELECT DISTINCT ON (cp.car_id)
                cp.car_id,
                timezone('UTC', ch.date) AS date,
                ch.charger_power::float8 AS charger_power,
                ch.conn_charge_cable
            FROM charging_processes cp
            INNER JOIN charges ch ON ch.charging_process_id = cp.id
            INNER JOIN selected_cars sc ON sc.id = cp.car_id
            ORDER BY cp.car_id, ch.date DESC
        )
        SELECT
            sc.id,
            sc.vin,
            sc.name,
            sc.model,
            sc.trim_badging,
            sc.marketing_name,
            lp.date,
            lp.latitude,
            lp.longitude,
            lp.battery_level,
            lp.usable_battery_level,
            lp.rated_battery_range_km,
            lp.est_battery_range_km,
            lp.outside_temp,
            lp.inside_temp,
            lp.odometer_km,
            cs.state,
            cs.start_date,
            lu.version,
            lvl.locked,
            lc.date,
            lc.charger_power,
            lc.conn_charge_cable
        FROM selected_cars sc
        LEFT JOIN latest_positions lp ON lp.car_id = sc.id
        LEFT JOIN current_states cs ON cs.car_id = sc.id
        LEFT JOIN latest_updates lu ON lu.car_id = sc.id
        LEFT JOIN latest_vehicle_lock lvl ON lvl.car_id = sc.id
        LEFT JOIN latest_charges lc ON lc.car_id = sc.id
        ORDER BY sc.display_priority NULLS LAST, sc.id
    """

    cards: list[TeslaMateCard] = []
    try:
        with psycopg.connect(settings.teslamate_postgres_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, vin_params)
                for (
                    _car_id,
                    vin,
                    name,
                    model,
                    trim_badging,
                    marketing_name,
                    updated_at,
                    latitude,
                    longitude,
                    battery_level,
                    usable_battery_level,
                    rated_battery_range_km,
                    est_battery_range_km,
                    outside_temp,
                    inside_temp,
                    odometer_km,
                    state,
                    state_start,
                    version,
                    locked,
                    charge_date,
                    charger_power,
                    conn_charge_cable,
                ) in cursor.fetchall():
                    display_name = preferred_vehicle_names.get(vin or "", name or (vin[-6:] if vin else "Tesla"))
                    plugged_in = None
                    if conn_charge_cable is not None:
                        plugged_in = conn_charge_cable != "<invalid>" and conn_charge_cable.lower() != "disconnected"
                    if charge_date and updated_at and charge_date > updated_at:
                        updated_at = charge_date
                    cards.append(
                        TeslaMateCard(
                            name=display_name,
                            model=model,
                            trim_badging=trim_badging,
                            marketing_name=marketing_name,
                            status=state,
                            status_duration=_humanize_status_duration(state_start),
                            latitude=latitude,
                            longitude=longitude,
                            battery_level=battery_level,
                            usable_battery_level=usable_battery_level,
                            rated_range_km=rated_battery_range_km,
                            est_range_km=est_battery_range_km,
                            outside_temp_c=outside_temp,
                            inside_temp_c=inside_temp,
                            odometer_km=odometer_km,
                            version=version,
                            plugged_in=plugged_in,
                            locked=locked,
                            charger_power_kw=charger_power,
                            updated_at=updated_at,
                            maps_url=_build_maps_url(latitude, longitude),
                            map_embed_url=_build_map_embed_url(latitude, longitude),
                        )
                    )
    except Exception as exc:
        return ([], [f"TeslaMate summary cards are temporarily unavailable: {exc}"])

    notes = []
    if cards:
        notes.append("TeslaMate vehicle summary cards are available in the dashboard.")
    return (cards, notes)
