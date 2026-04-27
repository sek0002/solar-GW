from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.config import Settings, parse_csv
from app.models import EnergyChartPoint, EnergyChartSeries


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
