from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.config import Settings, resolve_path
from app.models import EnergyChartPoint, EnergyChartSeries


def _db_path(settings: Settings):
    path = resolve_path(settings.chart_history_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect(settings: Settings) -> sqlite3.Connection:
    connection = sqlite3.connect(_db_path(settings))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def _initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS chart_series_meta (
            series_key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            unit TEXT NOT NULL,
            color TEXT NOT NULL,
            axis TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chart_points (
            series_key TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            value REAL NULL,
            state TEXT NULL,
            PRIMARY KEY (series_key, timestamp)
        );

        CREATE INDEX IF NOT EXISTS idx_chart_points_timestamp
            ON chart_points (timestamp);
        """
    )


def store_chart_history(settings: Settings, series_list: list[EnergyChartSeries]) -> None:
    if not series_list:
        return

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=settings.chart_history_retention_hours)

    with _connect(settings) as connection:
        _initialize(connection)
        connection.executemany(
            """
            INSERT INTO chart_series_meta (series_key, label, unit, color, axis, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(series_key) DO UPDATE SET
                label = excluded.label,
                unit = excluded.unit,
                color = excluded.color,
                axis = excluded.axis,
                updated_at = excluded.updated_at
            """,
            [
                (
                    series.key,
                    series.label,
                    series.unit,
                    series.color,
                    series.axis,
                    now.isoformat(),
                )
                for series in series_list
            ],
        )

        point_rows: list[tuple[str, str, float | None, str | None]] = []
        for series in series_list:
            for point in series.points:
                timestamp = point.timestamp.astimezone(timezone.utc).isoformat()
                point_rows.append((series.key, timestamp, point.value, point.state))

        if point_rows:
            connection.executemany(
                """
                INSERT INTO chart_points (series_key, timestamp, value, state)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(series_key, timestamp) DO UPDATE SET
                    value = excluded.value,
                    state = excluded.state
                """,
                point_rows,
            )

        connection.execute(
            "DELETE FROM chart_points WHERE timestamp < ?",
            (cutoff.isoformat(),),
        )


def load_chart_history(settings: Settings) -> list[EnergyChartSeries]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.chart_history_retention_hours)

    with _connect(settings) as connection:
        _initialize(connection)
        meta_rows = connection.execute(
            """
            SELECT series_key, label, unit, color, axis
            FROM chart_series_meta
            ORDER BY series_key
            """
        ).fetchall()
        point_rows = connection.execute(
            """
            SELECT series_key, timestamp, value, state
            FROM chart_points
            WHERE timestamp >= ?
            ORDER BY series_key, timestamp
            """,
            (cutoff.isoformat(),),
        ).fetchall()

    meta_by_key = {
        row["series_key"]: {
            "label": row["label"],
            "unit": row["unit"],
            "color": row["color"],
            "axis": row["axis"],
        }
        for row in meta_rows
    }
    points_by_key: dict[str, list[EnergyChartPoint]] = defaultdict(list)
    for row in point_rows:
        points_by_key[row["series_key"]].append(
            EnergyChartPoint(
                timestamp=datetime.fromisoformat(row["timestamp"]),
                value=row["value"],
                state=row["state"],
            )
        )

    series_list: list[EnergyChartSeries] = []
    for series_key, meta in meta_by_key.items():
        points = points_by_key.get(series_key, [])
        if not points:
            continue
        series_list.append(
            EnergyChartSeries(
                key=series_key,
                label=meta["label"],
                unit=meta["unit"],
                color=meta["color"],
                axis=meta["axis"],
                points=points,
            )
        )

    return series_list
