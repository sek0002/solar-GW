const initialData = window.__INITIAL_DASHBOARD__ || {};
const refreshSeconds = Number(document.body.dataset.refresh || "30");
const chartStore = new Map();
const lastKnownVehicleBatteryLevels = new Map();
const lastKnownTeslaVehicles = new Map();
const MAX_STORED_POINTS = 10000;
let energyChart = null;
let vehicleChart = null;
let activeEnergyChartKey = null;
let activeVehicleChartKey = null;
let energyChartWindowHours = 6;
let vehicleChartWindowHours = 6;
let automationPanelOpen = false;
const MANUAL_CHARGE_STORAGE_KEY = "solar-gw-manual-charge";
const TESLA_VEHICLE_STORAGE_KEY = "solar-gw-tesla-vehicles";
const CHART_HISTORY_STORAGE_KEY = "solar-gw-chart-history";
const CHART_POINT_INTERVAL_MS = 2 * 60 * 1000;
const SERIES_DEFAULTS = {
  solar_input_kw: { label: "Solar input", unit: "kW", color: "#f7c66b", axis: "power" },
  growatt_load_kw: { label: "Load consumption", unit: "kW", color: "#61e6ff", axis: "power" },
  grid_import_kw: { label: "Grid import", unit: "kW", color: "#ff8d7d", axis: "power" },
  growatt_soc_pct: { label: "Growatt battery SoC", unit: "%", color: "#8bf0b5", axis: "percent" },
  growatt_battery_charge_kw: { label: "Growatt battery charge", unit: "kW", color: "#4cc9f0", axis: "power" },
  growatt_battery_discharge_kw: { label: "Growatt battery discharge", unit: "kW", color: "#ff9cf0", axis: "power" },
  tesla_ev_charge_kw: { label: "Tesla EV charging", unit: "kW", color: "#ff5fa2", axis: "power" },
};
const SOURCE_ZERO_SERIES = {
  Growatt: [
    "growatt_load_kw",
    "grid_import_kw",
    "growatt_battery_charge_kw",
    "growatt_battery_discharge_kw",
  ],
  GoodWe: ["solar_input_kw"],
};
const SOURCE_AVERAGED_SERIES = {
  Growatt: ["growatt_soc_pct"],
};
const SOURCE_CONNECTED_SERIES = {
  Growatt: [
    "growatt_load_kw",
    "grid_import_kw",
    "growatt_soc_pct",
    "growatt_battery_charge_kw",
    "growatt_battery_discharge_kw",
  ],
  GoodWe: ["solar_input_kw"],
};

function formatValue(value, unit = "") {
  if (value === null || value === undefined || value === "") return "N/A";
  if (typeof value === "number") {
    return `${value.toFixed(Math.abs(value) < 10 ? 1 : 0)}${unit ? ` ${unit}` : ""}`;
  }
  return `${value}${unit ? ` ${unit}` : ""}`;
}

function formatLegendValue(value, unit = "") {
  if (value === null || value === undefined || value === "") return "N/A";
  if (typeof value !== "number") return formatValue(value, unit);
  const digits = unit === "%" ? 0 : Math.abs(value) < 10 ? 2 : 1;
  return `${value.toFixed(digits)}${unit ? ` ${unit}` : ""}`;
}

function hexToRgba(hex, alpha) {
  if (!hex || !hex.startsWith("#")) return `rgba(255,255,255,${alpha})`;
  const normalized = hex.length === 4 ? hex.split("").map((part, index) => (index === 0 ? "" : part + part)).join("") : hex.slice(1);
  const value = Number.parseInt(normalized, 16);
  const r = (value >> 16) & 255;
  const g = (value >> 8) & 255;
  const b = value & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function renderMetrics(metrics) {
  const root = document.getElementById("metrics");
  if (!root) return;
  root.innerHTML = metrics
    .map(
      (metric) => `
        <article class="metric-item">
          <span class="metric-label">${metric.label}</span>
          <strong class="metric-value tone-${metric.tone || "neutral"}">${formatValue(metric.value, metric.unit)}</strong>
        </article>
      `,
    )
    .join("");
}

function renderBatteries(batteries) {
  const root = document.getElementById("batteries");
  root.innerHTML = batteries
    .map(
      (battery) => `
        <article class="entity-card">
          <div class="entity-meta">
            <span>${battery.name}</span>
            <span class="pill">${battery.source}</span>
          </div>
          <div class="entity-main">
            <strong>${formatValue(battery.state_of_charge, "%")}</strong>
          </div>
          <div class="entity-secondary">
            <span>Power: ${formatValue(battery.power_kw, "kW")}</span>
            <span>State: ${battery.state || "Unknown"}</span>
            <span>Health: ${battery.health || "Unknown"}</span>
          </div>
        </article>
      `,
    )
    .join("");
}

function getVehicleCacheKey(vehicle) {
  return vehicle.vin || vehicle.name;
}

function loadTeslaVehicleCache() {
  try {
    const raw = window.localStorage.getItem(TESLA_VEHICLE_STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return;
    parsed.forEach((vehicle) => {
      if (!vehicle?.name && !vehicle?.vin) return;
      lastKnownTeslaVehicles.set(getVehicleCacheKey(vehicle), vehicle);
      if (vehicle.battery_level !== null && vehicle.battery_level !== undefined) {
        lastKnownVehicleBatteryLevels.set(getVehicleCacheKey(vehicle), Number(vehicle.battery_level));
      }
    });
  } catch (_error) {
    // Ignore cache hydration failures and continue with live data.
  }
}

function saveTeslaVehicleCache() {
  try {
    window.localStorage.setItem(
      TESLA_VEHICLE_STORAGE_KEY,
      JSON.stringify(Array.from(lastKnownTeslaVehicles.values()).slice(-8)),
    );
  } catch (_error) {
    // Ignore storage failures and keep the UI functional.
  }
}

function loadChartHistoryCache() {
  try {
    const raw = window.localStorage.getItem(CHART_HISTORY_STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return;
    parsed.forEach((series) => {
      if (!series?.key || !Array.isArray(series.points)) return;
      chartStore.set(series.key, {
        key: series.key,
        label: series.label || series.key,
        unit: series.unit || "",
        color: series.color || "#ffffff",
        axis: series.axis || "power",
        points: new Map(trimSeriesPoints(new Map(series.points.map((point) => [point.timestamp, {
          value: point.value ?? null,
          state: point.state || null,
        }])))),
      });
    });
  } catch (_error) {
    // Ignore cache hydration failures and continue with live data.
  }
}

function saveChartHistoryCache() {
  try {
    const payload = Array.from(chartStore.values()).map((series) => ({
      key: series.key,
      label: series.label,
      unit: series.unit,
      color: series.color,
      axis: series.axis,
      points: Array.from(series.points.entries()).map(([timestamp, point]) => ({
        timestamp,
        value: point?.value ?? null,
        state: point?.state || null,
      })),
    }));
    window.localStorage.setItem(CHART_HISTORY_STORAGE_KEY, JSON.stringify(payload));
  } catch (_error) {
    // Ignore storage failures and keep the UI functional.
  }
}

function trimSeriesPoints(points) {
  return Array.from(points.entries()).sort((a, b) => new Date(a[0]) - new Date(b[0])).slice(-MAX_STORED_POINTS);
}

function normalizeChartTimestamp(timestamp) {
  const parsed = new Date(timestamp).getTime();
  if (!Number.isFinite(parsed)) return null;
  const bucket = Math.floor(parsed / CHART_POINT_INTERVAL_MS) * CHART_POINT_INTERVAL_MS;
  return new Date(bucket).toISOString();
}

function ensureChartSeries(key, defaults = {}) {
  const existing = chartStore.get(key);
  if (existing) {
    return existing;
  }
  const created = {
    key,
    label: defaults.label || key,
    unit: defaults.unit || "",
    color: defaults.color || "#ffffff",
    axis: defaults.axis || "power",
    points: new Map(),
  };
  chartStore.set(key, created);
  return created;
}

function appendChartPoint(key, timestamp, value, defaults = {}) {
  const normalizedTimestamp = normalizeChartTimestamp(timestamp);
  if (!normalizedTimestamp) return;
  const series = ensureChartSeries(key, defaults);
  series.label = defaults.label || series.label;
  series.unit = defaults.unit || series.unit;
  series.color = defaults.color || series.color;
  series.axis = defaults.axis || series.axis;
  series.points.set(normalizedTimestamp, { value, state: null });
  series.points = new Map(trimSeriesPoints(series.points));
  chartStore.set(key, series);
  saveChartHistoryCache();
}

function appendChartPointWithCarryForward(key, timestamp, value, defaults = {}) {
  const normalizedTimestamp = normalizeChartTimestamp(timestamp);
  if (!normalizedTimestamp) return;
  const normalizedMs = new Date(normalizedTimestamp).getTime();
  if (!Number.isFinite(normalizedMs)) return;

  const series = ensureChartSeries(key, defaults);
  series.label = defaults.label || series.label;
  series.unit = defaults.unit || series.unit;
  series.color = defaults.color || series.color;
  series.axis = defaults.axis || series.axis;

  const sortedPoints = trimSeriesPoints(series.points);
  const lastEntry = sortedPoints.length ? sortedPoints[sortedPoints.length - 1] : null;
  const lastTimestampMs = lastEntry ? new Date(lastEntry[0]).getTime() : null;
  const lastValue = lastEntry?.[1]?.value;
  const carryForwardValue = Number.isFinite(lastValue) ? lastValue : value;

  if (Number.isFinite(lastTimestampMs) && lastTimestampMs < normalizedMs) {
    for (
      let bucketMs = lastTimestampMs + CHART_POINT_INTERVAL_MS;
      bucketMs < normalizedMs;
      bucketMs += CHART_POINT_INTERVAL_MS
    ) {
      series.points.set(new Date(bucketMs).toISOString(), { value: carryForwardValue, state: null });
    }
  }

  series.points.set(normalizedTimestamp, { value, state: null });
  series.points = new Map(trimSeriesPoints(series.points));
  chartStore.set(key, series);
  saveChartHistoryCache();
}

function hydrateTeslaVehicles(vehicles) {
  const liveVehicles = Array.isArray(vehicles) ? vehicles : [];
  const mergedVehicles = liveVehicles.map((vehicle) => {
    if (vehicle.source !== "Tesla Vehicle") {
      return vehicle;
    }

    const cacheKey = getVehicleCacheKey(vehicle);
    const cached = lastKnownTeslaVehicles.get(cacheKey) || {};
    const merged = {
      ...cached,
      ...vehicle,
      battery_level: vehicle.battery_level ?? cached.battery_level ?? null,
      charge_current_a: vehicle.charge_current_a ?? cached.charge_current_a ?? null,
      charge_power_kw: vehicle.charge_power_kw ?? cached.charge_power_kw ?? null,
      range_km: vehicle.range_km ?? cached.range_km ?? null,
      plugged_in: vehicle.plugged_in ?? cached.plugged_in ?? null,
      location: vehicle.location ?? cached.location ?? null,
      charging_state: vehicle.charging_state ?? cached.charging_state ?? null,
      source: vehicle.source,
      stale: false,
    };
    lastKnownTeslaVehicles.set(cacheKey, merged);
    if (merged.battery_level !== null && merged.battery_level !== undefined) {
      lastKnownVehicleBatteryLevels.set(cacheKey, Number(merged.battery_level));
    }
    return merged;
  });

  const liveTeslaKeys = new Set(
    liveVehicles
      .filter((vehicle) => vehicle.source === "Tesla Vehicle")
      .map((vehicle) => getVehicleCacheKey(vehicle)),
  );

  const staleVehicles = Array.from(lastKnownTeslaVehicles.entries())
    .filter(([key]) => !liveTeslaKeys.has(key))
    .map(([, vehicle]) => ({
      ...vehicle,
      stale: true,
      source: vehicle.source || "Tesla Vehicle",
    }));

  if (mergedVehicles.some((vehicle) => vehicle.source === "Tesla Vehicle") || staleVehicles.length) {
    saveTeslaVehicleCache();
  }

  return [
    ...mergedVehicles,
    ...staleVehicles,
  ];
}

function appendDisconnectedSourcePoints(data, timestamp) {
  const sourceStatuses = new Map((data.sources || []).map((source) => [source.name, source.status]));
  Object.entries(SOURCE_ZERO_SERIES).forEach(([sourceName, seriesKeys]) => {
    const status = sourceStatuses.get(sourceName);
    if (status && status !== "disconnected") return;
    seriesKeys.forEach((seriesKey) => {
      appendChartPoint(seriesKey, timestamp, 0, SERIES_DEFAULTS[seriesKey] || {});
    });
  });

  Object.entries(SOURCE_AVERAGED_SERIES).forEach(([sourceName, seriesKeys]) => {
    const status = sourceStatuses.get(sourceName);
    if (status && status !== "disconnected") return;
    seriesKeys.forEach((seriesKey) => {
      const series = chartStore.get(seriesKey);
      if (!series) return;
      const recentValues = Array.from(series.points.values())
        .map((point) => point?.value)
        .filter((value) => Number.isFinite(value))
        .slice(-10);
      if (!recentValues.length) return;
      const averageValue = recentValues.reduce((sum, value) => sum + value, 0) / recentValues.length;
      appendChartPoint(seriesKey, timestamp, averageValue, SERIES_DEFAULTS[seriesKey] || {});
    });
  });
}

function appendConnectedSourceContinuityPoints(data, timestamp) {
  const sourceStatuses = new Map((data.sources || []).map((source) => [source.name, source.status]));
  Object.entries(SOURCE_CONNECTED_SERIES).forEach(([sourceName, seriesKeys]) => {
    const status = sourceStatuses.get(sourceName);
    if (status && status !== "connected" && status !== "degraded") return;
    seriesKeys.forEach((seriesKey) => {
      const series = chartStore.get(seriesKey);
      if (!series) return;
      const latestEntry = trimSeriesPoints(series.points).slice(-1)[0];
      const latestValue = latestEntry?.[1]?.value;
      if (!Number.isFinite(latestValue)) return;
      appendChartPointWithCarryForward(seriesKey, timestamp, latestValue, SERIES_DEFAULTS[seriesKey] || {});
    });
  });
}

function appendTeslaVehicleFallbackPoints(vehicles, timestamp) {
  (vehicles || [])
    .filter((vehicle) => vehicle.source === "Tesla Vehicle")
    .forEach((vehicle) => {
      const vehicleKey = vehicle.name || vehicle.vin || "tesla";
      const socKey = `vehicle_${String(vehicleKey).toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "")}_soc_pct`;
      const chargeKey = `vehicle_${String(vehicleKey).toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "")}_charge_kw`;
      if (vehicle.battery_level !== null && vehicle.battery_level !== undefined) {
        appendChartPointWithCarryForward(
          socKey,
          timestamp,
          Number(vehicle.battery_level),
          { label: `${vehicle.name} SoC`, unit: "%", color: "#7db0ff", axis: "percent" },
        );
      }
      if (vehicle.charge_power_kw !== null && vehicle.charge_power_kw !== undefined) {
        appendChartPointWithCarryForward(
          chargeKey,
          timestamp,
          Number(vehicle.charge_power_kw),
          { label: `${vehicle.name} charge rate`, unit: "kW", color: "#2bd9a0", axis: "power" },
        );
      }
    });
}

function appendTeslaAggregateFallbackPoint(vehicles, data, timestamp) {
  const teslaVehicles = (vehicles || []).filter((vehicle) => vehicle.source === "Tesla Vehicle");
  if (teslaVehicles.length) {
    const totalChargeKw = teslaVehicles.reduce((sum, vehicle) => sum + (Number(vehicle.charge_power_kw) || 0), 0);
    appendChartPointWithCarryForward("tesla_ev_charge_kw", timestamp, totalChargeKw, SERIES_DEFAULTS.tesla_ev_charge_kw);
    return;
  }

  const teslaSource = (data.sources || []).find((source) => source.name === "Tesla Charging");
  if (!teslaSource || teslaSource.status === "disconnected") {
    appendChartPointWithCarryForward("tesla_ev_charge_kw", timestamp, 0, SERIES_DEFAULTS.tesla_ev_charge_kw);
  }
}

function getVehicleStatusMeta(vehicle) {
  const state = String(vehicle.charging_state || "").toLowerCase();
  if (state === "charging") {
    return { className: "connected", label: "Charging" };
  }
  if (vehicle.plugged_in || state === "complete" || state === "stopped") {
    return { className: "connected", label: "Connected" };
  }
  if (state === "disconnected" || state === "offline") {
    return { className: "disconnected", label: "Disconnected" };
  }
  return { className: "degraded", label: vehicle.charging_state || "Unknown" };
}

function formatInlineMetric(value, unit = "", digits = 1) {
  if (!Number.isFinite(value)) return null;
  return `${Number(value).toFixed(digits)}${unit}`;
}

function getChargeSpeedMarkup({ amps = null, kw = null, label = "Live", accent = "accent" } = {}) {
  const parts = [];
  if (Number.isFinite(amps) && Number(amps) > 0) {
    parts.push(formatInlineMetric(Number(amps), "A", Number(amps) < 10 ? 1 : 0));
  }
  if (Number.isFinite(kw) && Number(kw) > 0) {
    parts.push(formatInlineMetric(Number(kw), "kW", Number(kw) < 10 ? 1 : 0));
  }
  if (!parts.length) return "";
  return `<span class="status-metric status-metric-${accent}" title="${label}">${parts.join(" · ")}</span>`;
}

function getBatteryFillStyle(level) {
  const value = Math.max(0, Math.min(100, Number(level || 0)));
  return `width:${value}%;background:linear-gradient(90deg,#ff5a5f 0%,#f5d547 58%,#46d37b 100%);`;
}

function getTrackerFillStyle(value, maxValue, color) {
  const safeMax = Math.max(1, Number(maxValue) || 1);
  const width = Math.max(0, Math.min(100, (Math.max(0, Number(value) || 0) / safeMax) * 100));
  return `width:${width}%;background:${color};`;
}

function getLatestSeriesValue(seriesKey) {
  const series = chartStore.get(seriesKey);
  if (!series) return 0;
  const latestEntry = trimSeriesPoints(series.points).slice(-1)[0];
  const latestValue = latestEntry?.[1]?.value;
  return Number.isFinite(latestValue) ? Number(latestValue) : 0;
}

function renderBatteryRail(vehicles, batteries, powerFlow) {
  const root = document.getElementById("battery-rail");
  if (!root) return;
  const items = [
    ...vehicles
      .map((vehicle) => {
        const cacheKey = getVehicleCacheKey(vehicle);
        if (vehicle.battery_level !== null && vehicle.battery_level !== undefined) {
          lastKnownVehicleBatteryLevels.set(cacheKey, Number(vehicle.battery_level));
        }
        const level =
          vehicle.battery_level !== null && vehicle.battery_level !== undefined
            ? Number(vehicle.battery_level)
            : lastKnownVehicleBatteryLevels.get(cacheKey);
        if (level === null || level === undefined) return null;
        const disconnected = String(vehicle.charging_state || "").toLowerCase() === "disconnected";
        return {
          name: vehicle.name,
          source:
            vehicle.stale || (disconnected && (vehicle.battery_level === null || vehicle.battery_level === undefined))
              ? "Tesla EV • Last known"
              : "Tesla EV",
          level,
          detail: vehicle.charging_state || "Unknown",
        };
      })
      .filter(Boolean),
    ...batteries
      .filter((battery) => battery.source === "Growatt Hybrid" && battery.state_of_charge !== null && battery.state_of_charge !== undefined)
      .map((battery) => ({
        name: battery.name,
        source: battery.source,
        level: battery.state_of_charge,
        detail: battery.state || "Unknown",
      })),
  ];

  root.innerHTML = items
    .map(
      (item) => `
        <article class="battery-bar-card">
          <div class="battery-bar-track">
            <div class="battery-bar-fill" style="${getBatteryFillStyle(item.level)}"></div>
          </div>
          <div class="battery-bar-copy">
            <strong>${item.name} ${Math.round(Number(item.level || 0))}%</strong>
            <span>${item.source}</span>
          </div>
        </article>
      `,
    )
    .join("");

  const trackerItems = [
    {
      name: "Solar input",
      value: Math.max(0, Number(powerFlow?.solar_kw) || 0),
      color: "#f7c66b",
    },
    {
      name: "Load consumption",
      value: Math.max(0, Number(powerFlow?.home_kw) || 0),
      color: "#61e6ff",
    },
    {
      name: "Grid import",
      value: Math.max(0, Number(powerFlow?.grid_kw) || 0),
      color: "#ff8d7d",
    },
    {
      name: "Growatt charge",
      value: Math.max(0, getLatestSeriesValue("growatt_battery_charge_kw")),
      color: "#4cc9f0",
    },
    {
      name: "Growatt discharge",
      value: Math.max(0, getLatestSeriesValue("growatt_battery_discharge_kw")),
      color: "#ff9cf0",
    },
  ];
  const trackerMax = Math.max(1, ...trackerItems.map((item) => item.value));

  root.innerHTML = `
    <div class="battery-bar-row">
      ${barMarkup}
    </div>
    <div class="system-tracker-row">
      ${trackerItems
        .map(
          (item) => `
            <article class="system-tracker-card">
              <div class="system-tracker-copy">
                <strong>${item.name}</strong>
                <span>${formatValue(item.value, "kW")}</span>
              </div>
              <div class="system-tracker-track">
                <div class="system-tracker-fill" style="${getTrackerFillStyle(item.value, trackerMax, item.color)}"></div>
              </div>
            </article>
          `,
        )
        .join("")}
    </div>
  `;
}

function renderChargers(chargers) {
  const root = document.getElementById("chargers");
  root.innerHTML = chargers
    .map(
      (charger) => `
        <article class="entity-card">
          <div class="entity-meta">
            <span>${charger.name}</span>
            <span class="pill">${charger.source}</span>
          </div>
          <div class="entity-main">
            <strong>${formatValue(charger.power_kw, "kW")}</strong>
            <span>${charger.status || "Unknown"}</span>
          </div>
          <div class="entity-secondary">
            <span>Active sessions: ${charger.active_sessions ?? 0}</span>
            <span>Connected vehicles: ${charger.connected_vehicles ?? 0}</span>
            <span>Capacity: ${formatValue(charger.max_power_kw, "kW")} / ${formatValue(charger.circuit_amps, "A")}</span>
            <span>Location: ${charger.location || "Unknown"}</span>
            <span>Vehicles: ${(charger.vehicle_names || []).join(", ") || "None"}</span>
          </div>
        </article>
      `,
    )
    .join("");
}

function renderPlants(plants) {
  const root = document.getElementById("plants");
  if (!plants.length) {
    root.innerHTML = `
      <article class="entity-card">
        <div class="entity-meta">
          <span>Growatt Hybrid</span>
          <span class="pill">Visibility</span>
        </div>
        <div class="entity-main">
          <strong>No visible plants</strong>
        </div>
        <div class="entity-secondary">
          <span>The current Growatt hybrid token does not expose any plants on the selected server.</span>
        </div>
      </article>
    `;
    return;
  }

  root.innerHTML = plants
    .map(
      (plant) => `
        <article class="entity-card">
          <div class="entity-meta">
            <span>${plant.name}</span>
            <span class="pill">${plant.source}</span>
          </div>
          <div class="entity-main">
            <strong>${plant.status || "Visible"}</strong>
          </div>
          <div class="entity-secondary">
            <span>Plant ID: ${plant.plant_id || "Unknown"}</span>
            <span>Type: ${plant.plant_type || "Unknown"}</span>
            <span>Timezone: ${plant.timezone || "Unknown"}</span>
            <span>Capacity: ${formatValue(plant.capacity_kw, "kW")}</span>
            <span>Devices: ${plant.device_count ?? "Unknown"}</span>
          </div>
        </article>
      `,
    )
    .join("");
}

function renderVehicles(vehicles) {
  const root = document.getElementById("vehicles");
  root.innerHTML = vehicles
    .map((vehicle) => {
      const chargeMetric = getChargeSpeedMarkup({
        amps: Number(vehicle.charge_current_a),
        kw: Number(vehicle.charge_power_kw),
        label: `${vehicle.name} charge speed`,
        accent: "accent",
      });
      return `
        <article class="entity-card">
          <div class="entity-meta">
            <span class="entity-meta-title">${vehicle.name}</span>
            <div class="entity-meta-badges">
              ${chargeMetric}
              <span class="pill">${vehicle.stale ? `${vehicle.source} • Last known` : vehicle.source}</span>
            </div>
          </div>
          <div class="entity-main">
            <strong>${formatValue(vehicle.battery_level, "%")}</strong>
            <span>${vehicle.stale ? `Last known ${vehicle.charging_state || "Unknown"}` : vehicle.charging_state || "Unknown"}</span>
          </div>
          <div class="entity-secondary">
            <span>Charge rate: ${formatValue(vehicle.charge_power_kw, "kW")}</span>
            <span>Range: ${formatValue(vehicle.range_km, "km")}</span>
            <span>Plugged in: ${vehicle.plugged_in ? "Yes" : "No"}</span>
            <span>Location: ${vehicle.location || "Unknown"}</span>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderSources(sources, vehicles, chargers, powerFlow) {
  const root = document.getElementById("provider-strip");
  if (!root) return;
  const providerLabelMap = {
    "GoodWe": "Solar",
    "Tesla Charging": "Tesla Wall",
  };
  const wallCharger = (chargers || []).find((charger) => charger.source === "Tesla Charging");
  const totalVehicleAmps = (vehicles || [])
    .filter((vehicle) => vehicle.source === "Tesla Vehicle")
    .reduce((sum, vehicle) => sum + (Number(vehicle.charge_current_a) || 0), 0);

  const providerMarkup = sources
    .map((source) => {
      let metric = "";
      if (source.name === "Tesla Charging") {
        metric = getChargeSpeedMarkup({
          amps: totalVehicleAmps,
          kw: Number(wallCharger?.power_kw),
          label: "Tesla Wall live speed",
          accent: "accent",
        });
      } else if (source.name === "GoodWe") {
        metric = getChargeSpeedMarkup({
          kw: Number(powerFlow?.solar_kw),
          label: "Solar input",
          accent: "good",
        });
      }
      return `
        <div class="provider-pill ${source.status}">
          <span>${providerLabelMap[source.name] || source.name}</span>
          ${metric}
        </div>
      `;
    })
    .join("");

  const vehicleMarkup = (vehicles || [])
    .filter((vehicle) => vehicle.source === "Tesla Vehicle")
    .map((vehicle) => {
      const status = getVehicleStatusMeta(vehicle);
      const metric = getChargeSpeedMarkup({
        amps: Number(vehicle.charge_current_a),
        kw: Number(vehicle.charge_power_kw),
        label: `${vehicle.name} live speed`,
        accent: "accent",
      });
      return `
        <div class="provider-pill vehicle-pill ${status.className}">
          <span>${vehicle.name}</span>
          ${metric}
        </div>
      `;
    })
    .join("");

  root.innerHTML = providerMarkup + vehicleMarkup;
}

function renderNotes(notes) {
  const root = document.getElementById("notes");
  root.innerHTML = notes.map((note) => `<li>${note}</li>`).join("");
}

function renderPowerFlow(flow) {
  return flow;
}

function renderUpdatedAt(timestamp) {
  const value = timestamp ? new Date(timestamp).toLocaleString() : "Waiting for sync";
  document.getElementById("updated-at").textContent = `Last sync ${value}`;
}

function loadManualChargeState() {
  try {
    const raw = window.localStorage.getItem(MANUAL_CHARGE_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return {
      enabled: Boolean(parsed.enabled),
      targetAmps: Math.max(5, Math.min(30, Number(parsed.targetAmps) || 10)),
    };
  } catch (_error) {
    return null;
  }
}

function saveManualChargeState(enabled, targetAmps) {
  try {
    window.localStorage.setItem(
      MANUAL_CHARGE_STORAGE_KEY,
      JSON.stringify({
        enabled: Boolean(enabled),
        targetAmps: Math.max(5, Math.min(30, Number(targetAmps) || 10)),
      }),
    );
  } catch (_error) {
    // Ignore storage failures and keep the UI functional.
  }
}

function updateManualChargeReadout(amps) {
  const numericAmps = Number(amps);
  const kw = ((numericAmps * 230) / 1000).toFixed(2);
  document.getElementById("manual-charge-amps").textContent = `${numericAmps}A`;
  document.getElementById("manual-charge-kw").textContent = `${kw} kW`;
}

function renderAutomationPanel(panel) {
  const toggle = document.getElementById("automation-toggle");
  const chargeToggle = document.getElementById("charge-toggle");
  const root = document.getElementById("automation-panel");
  if (!toggle || !root || !panel) return;

  toggle.setAttribute("aria-expanded", automationPanelOpen ? "true" : "false");
  if (chargeToggle) {
    chargeToggle.setAttribute("aria-expanded", automationPanelOpen ? "true" : "false");
  }
  root.hidden = !automationPanelOpen;
  const globalToggle = document.getElementById("automation-global-enabled");
  globalToggle.checked = Boolean(panel.global_enabled);

  document.getElementById("automation-mode").textContent = panel.effective_mode || "Idle";
  document.getElementById("automation-detail").textContent = panel.effective_detail || "Automation waiting for an active rule.";
  document.getElementById("automation-target").textContent =
    panel.effective_target_amps && panel.effective_target_kw
      ? `${panel.effective_target_amps}A / ${panel.effective_target_kw.toFixed(2)} kW`
      : "No target";
  document.getElementById("automation-icon-title").textContent = panel.global_enabled ? panel.effective_mode || "Automation active" : "Automation paused";
  document.getElementById("automation-icon-detail").textContent = panel.global_enabled
    ? panel.effective_detail || "Automation is monitoring live conditions."
    : "Combined automation toggle is off.";

  document.getElementById("automation-rules").innerHTML = (panel.rules || [])
    .map(
      (rule) => `
        <article class="automation-rule ${rule.active ? "is-active" : ""}">
          <label class="switch">
            <input type="checkbox" data-rule-id="${rule.id}" ${rule.enabled ? "checked" : ""} />
            <span class="slider"></span>
          </label>
          <div class="automation-rule-copy">
            <h5>${rule.label}</h5>
            <p>${rule.description}</p>
          </div>
          <div class="automation-rule-meta">
            <div>${rule.active ? "Active" : "Armed"}</div>
            <div>${rule.target_amps && rule.target_kw ? `${rule.target_amps}A / ${rule.target_kw.toFixed(2)} kW` : rule.detail || ""}</div>
          </div>
        </article>
      `,
    )
    .join("");

  const manualEnabled = document.getElementById("manual-charge-enabled");
  const manualSlider = document.getElementById("manual-charge-slider");
  const savedManualCharge = loadManualChargeState();
  const targetAmps = panel.manual_charge?.target_amps ?? savedManualCharge?.targetAmps ?? 10;
  const manualChargeEnabled = panel.manual_charge?.enabled ?? savedManualCharge?.enabled ?? false;
  manualEnabled.checked = Boolean(manualChargeEnabled);
  manualSlider.value = String(targetAmps);
  saveManualChargeState(manualChargeEnabled, targetAmps);
  updateManualChargeReadout(targetAmps);
  const manualStatus = panel.manual_charge?.status || "idle";
  const manualNotes = Array.isArray(panel.manual_charge?.notes) ? panel.manual_charge.notes : [];
  document.getElementById("manual-charge-title").textContent = manualEnabled.checked
    ? manualStatus === "error"
      ? "Manual charge issue"
      : manualStatus === "partial"
        ? "Manual charge partial"
        : "Manual charge on"
    : "Manual charge off";
  document.getElementById("manual-charge-detail").textContent =
    panel.manual_charge?.detail ||
    (manualEnabled.checked
      ? `${targetAmps}A combined target active`
      : `Ready at ${targetAmps}A / ${((targetAmps * 230) / 1000).toFixed(2)} kW`);
  if (manualNotes.length) {
    document.getElementById("manual-charge-detail").textContent += ` ${manualNotes[0]}`;
  }

  root.querySelectorAll("[data-rule-id]").forEach((input) => {
    input.addEventListener("change", async (event) => {
      const target = event.currentTarget;
      await fetch("/api/automation/rule", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          rule_id: target.getAttribute("data-rule-id"),
          enabled: target.checked,
        }),
      });
      await refreshDashboard();
    });
  });

  globalToggle.onchange = async () => {
    await fetch("/api/automation/global", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: globalToggle.checked,
      }),
    });
    await refreshDashboard();
  };

  manualEnabled.onchange = async () => {
    document.getElementById("manual-charge-title").textContent = manualEnabled.checked ? "Manual charge on" : "Manual charge off";
    saveManualChargeState(manualEnabled.checked, manualSlider.value);
    const response = await fetch("/api/automation/manual-charge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: manualEnabled.checked,
        target_amps: Number(manualSlider.value),
      }),
    });
    const payload = await response.json().catch(() => null);
    if (payload?.tesla?.detail) {
      document.getElementById("manual-charge-detail").textContent = payload.tesla.detail;
    }
    await refreshDashboard();
  };

  manualSlider.oninput = () => {
    updateManualChargeReadout(manualSlider.value);
    document.getElementById("manual-charge-detail").textContent = `${manualSlider.value}A / ${((Number(manualSlider.value) * 230) / 1000).toFixed(2)} kW`;
    saveManualChargeState(manualEnabled.checked, manualSlider.value);
  };
  manualSlider.onchange = async () => {
    saveManualChargeState(manualEnabled.checked, manualSlider.value);
    const response = await fetch("/api/automation/manual-charge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: manualEnabled.checked,
        target_amps: Number(manualSlider.value),
      }),
    });
    const payload = await response.json().catch(() => null);
    if (payload?.tesla?.detail) {
      document.getElementById("manual-charge-detail").textContent = payload.tesla.detail;
    }
    await refreshDashboard();
  };
}

function mergeEnergySeries(seriesList) {
  (seriesList || []).forEach((series) => {
    if (!series?.key) return;
    const existing = chartStore.get(series.key) || {
      key: series.key,
      label: series.label,
      unit: series.unit,
      color: series.color,
      axis: series.axis || "power",
      points: new Map(),
    };

    existing.label = series.label;
    existing.unit = series.unit;
    existing.color = series.color;
    existing.axis = series.axis || "power";

    (series.points || []).forEach((point) => {
      if (!point?.timestamp) return;
      existing.points.set(point.timestamp, {
        value: point.value ?? null,
        state: point.state || null,
      });
    });

    existing.points = new Map(trimSeriesPoints(existing.points));
    chartStore.set(series.key, existing);
  });
  saveChartHistoryCache();
}

function getChartWindow(seriesList, hours) {
  const timestamps = seriesList.flatMap((series) =>
    Array.from(series.points.keys()).map((timestamp) => new Date(timestamp).getTime()),
  );
  const windowEnd = timestamps.length ? Math.max(...timestamps) : Date.now();
  return {
    windowStart: windowEnd - hours * 60 * 60 * 1000,
    windowEnd,
  };
}

function getXAxisStepHours(hours) {
  if (hours <= 6) return 1;
  if (hours <= 12) return 2;
  if (hours <= 24) return 4;
  if (hours <= 48) return 8;
  return Math.max(12, Math.ceil(hours / 6));
}

function formatXAxisLabel(timestamp, hours) {
  const options =
    hours > 24
      ? { month: "short", day: "numeric", hour: "2-digit" }
      : hours > 12
        ? { hour: "2-digit" }
        : { hour: "2-digit", minute: "2-digit" };
  return new Date(Number(timestamp)).toLocaleString([], options);
}

function buildChartState(filterFn, activeKey, hours) {
  const filteredSeries = Array.from(chartStore.values()).filter((series) => filterFn(series));
  const { windowStart, windowEnd } = getChartWindow(filteredSeries, hours);

  const datasets = filteredSeries.map((series) => {
    const highlighted = !activeKey || activeKey === series.key;
    const data = Array.from(series.points.entries())
      .map(([timestamp, point]) => ({
        x: new Date(timestamp).getTime(),
        y: point?.value ?? null,
        state: point?.state || null,
      }))
      .filter((point) => point.x >= windowStart && point.x <= windowEnd);
    return {
      label: series.label,
      key: series.key,
      unit: series.unit,
      yAxisID: series.axis === "percent" ? "percent" : "power",
      data,
      spanGaps: true,
      borderColor: highlighted ? series.color : hexToRgba(series.color, 0.25),
      backgroundColor: highlighted ? hexToRgba(series.color, 0.18) : hexToRgba(series.color, 0.06),
      pointRadius: highlighted ? 1.8 : 0.8,
      pointHoverRadius: 4,
      borderWidth: highlighted ? 2.4 : 1.3,
      tension: 0.28,
      fill: false,
    };
  });

  return { datasets, windowStart, windowEnd };
}

function getPowerScaleBounds(datasets, options = {}) {
  if (!options.forcePowerZero) {
    return { min: options.powerMin ?? null, max: null };
  }

  const powerValues = datasets
    .filter((dataset) => dataset.yAxisID === "power")
    .flatMap((dataset) => dataset.data.map((point) => point?.y))
    .filter((value) => Number.isFinite(value));

  if (!powerValues.length) {
    return { min: 0, max: 1 };
  }

  const maxValue = Math.max(...powerValues, 0);
  const paddedMax = Math.max(1, Math.ceil(maxValue * 1.15 * 10) / 10);
  return { min: 0, max: paddedMax };
}

function syncChartWindowControls(target) {
  const hours = target === "vehicle" ? vehicleChartWindowHours : energyChartWindowHours;
  const rangeId = target === "vehicle" ? "vehicle-chart-window-range" : "chart-window-range";
  const inputId = target === "vehicle" ? "vehicle-chart-window-input" : "chart-window-input";
  document.getElementById(rangeId).value = String(hours);
  document.getElementById(inputId).value = String(hours);
  document.querySelectorAll(`[data-chart-target="${target}"]`).forEach((button) => {
    button.classList.toggle("active", Number(button.getAttribute("data-hours")) === hours);
  });
}

function setChartWindowHours(target, hours) {
  const nextValue = Math.max(1, Math.min(168, Number(hours) || 24));
  if (target === "vehicle") {
    vehicleChartWindowHours = nextValue;
    syncChartWindowControls("vehicle");
    renderVehicleChart();
    return;
  }
  energyChartWindowHours = nextValue;
  syncChartWindowControls("energy");
  renderEnergyChart();
}

function renderChartLegend(rootId, filterFn, activeKey, renderFn, setActiveKey) {
  const root = document.getElementById(rootId);
  if (!root) return;
  const series = Array.from(chartStore.values()).filter((item) => filterFn(item));
  root.innerHTML = series
    .map((item) => {
      const highlighted = !activeKey || activeKey === item.key;
      return `
        <button class="legend-chip ${highlighted ? "active" : "dimmed"}" data-series-key="${item.key}" type="button">
          <span class="legend-swatch" style="background:${item.color}"></span>
          <span>${item.label}</span>
        </button>
      `;
    })
    .join("");

  root.querySelectorAll("[data-series-key]").forEach((button) => {
    button.addEventListener("click", () => {
      const key = button.getAttribute("data-series-key");
      setActiveKey(activeKey === key ? null : key);
      renderFn();
    });
  });
}

function createChart(canvas, datasets, hours, windowStart, windowEnd, options = {}) {
  const stepHours = getXAxisStepHours(hours);
  const forcePowerZero = options.forcePowerZero ?? false;
  const powerScale = getPowerScaleBounds(datasets, options);
  const powerTickSuffix = options.powerTickSuffix || "kW";
  return new window.Chart(canvas, {
      type: "line",
      data: { datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
          mode: "index",
          intersect: false,
        },
        animation: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "rgba(5, 12, 18, 0.94)",
            borderColor: "rgba(139, 179, 180, 0.22)",
            borderWidth: 1,
            padding: 12,
            callbacks: {
              title(items) {
                if (!items.length) return "";
                return formatXAxisLabel(items[0].parsed.x, hours);
              },
              label(context) {
                const unit = context.dataset.unit || "";
                return `${context.dataset.label}: ${formatLegendValue(context.parsed.y, unit)}`;
              },
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            min: windowStart,
            max: windowEnd,
            ticks: {
              color: "rgba(156, 183, 180, 0.88)",
              stepSize: stepHours * 60 * 60 * 1000,
              callback: (value) => formatXAxisLabel(value, hours),
              maxRotation: 0,
            },
            grid: { color: "rgba(139, 179, 180, 0.08)" },
          },
          power: {
            position: "left",
            min: powerScale.min,
            max: powerScale.max,
            suggestedMin: forcePowerZero ? 0 : undefined,
            beginAtZero: forcePowerZero,
            bounds: forcePowerZero ? "ticks" : undefined,
            grace: forcePowerZero ? 0 : undefined,
            afterBuildTicks(scale) {
              if (!forcePowerZero) return;
              scale.ticks = scale.ticks.filter((tick) => Number(tick.value) >= 0);
            },
            ticks: {
              color: "rgba(156, 183, 180, 0.88)",
              callback: (value) => `${value} ${powerTickSuffix}`,
            },
            grid: { color: "rgba(139, 179, 180, 0.08)" },
          },
          percent: {
            position: "right",
            min: 0,
            max: 100,
            ticks: {
              color: "rgba(156, 183, 180, 0.88)",
              callback: (value) => `${value}%`,
            },
            grid: { drawOnChartArea: false },
          },
        },
      },
    });
}

function updateChart(chart, datasets, hours, windowStart, windowEnd, options = {}) {
  const powerScale = getPowerScaleBounds(datasets, options);
  const powerTickSuffix = options.powerTickSuffix || "kW";
  chart.data.datasets = datasets;
  chart.options.scales.x.min = windowStart;
  chart.options.scales.x.max = windowEnd;
  chart.options.scales.x.ticks.stepSize = getXAxisStepHours(hours) * 60 * 60 * 1000;
  chart.options.scales.x.ticks.callback = (value) => formatXAxisLabel(value, hours);
  chart.options.scales.power.min = powerScale.min;
  chart.options.scales.power.max = powerScale.max;
  chart.options.scales.power.suggestedMin = options.forcePowerZero ? 0 : undefined;
  chart.options.scales.power.beginAtZero = Boolean(options.forcePowerZero);
  chart.options.scales.power.bounds = options.forcePowerZero ? "ticks" : undefined;
  chart.options.scales.power.grace = options.forcePowerZero ? 0 : undefined;
  chart.options.scales.power.ticks.callback = (value) => `${value} ${powerTickSuffix}`;
  chart.options.plugins.tooltip.callbacks.title = (items) => {
    if (!items.length) return "";
    return formatXAxisLabel(items[0].parsed.x, hours);
  };
  chart.update();
}

function renderEnergyChart() {
  const canvas = document.getElementById("energy-chart");
  if (!canvas || !window.Chart) return;

  renderChartLegend(
    "chart-legend",
    (series) => !series.key.startsWith("vehicle_"),
    activeEnergyChartKey,
    renderEnergyChart,
    (value) => {
      activeEnergyChartKey = value;
    },
  );
  const { datasets, windowStart, windowEnd } = buildChartState(
    (series) => !series.key.startsWith("vehicle_"),
    activeEnergyChartKey,
    energyChartWindowHours,
  );

  if (!energyChart) {
    energyChart = createChart(canvas, datasets, energyChartWindowHours, windowStart, windowEnd, { powerMin: null });
    return;
  }

  updateChart(energyChart, datasets, energyChartWindowHours, windowStart, windowEnd, { powerMin: null });
}

function renderVehicleChart() {
  const canvas = document.getElementById("vehicle-chart");
  if (!canvas || !window.Chart) return;

  renderChartLegend(
    "vehicle-chart-legend",
    (series) => series.key.startsWith("vehicle_"),
    activeVehicleChartKey,
    renderVehicleChart,
    (value) => {
      activeVehicleChartKey = value;
    },
  );
  const { datasets, windowStart, windowEnd } = buildChartState(
    (series) => series.key.startsWith("vehicle_"),
    activeVehicleChartKey,
    vehicleChartWindowHours,
  );

  if (!vehicleChart) {
    vehicleChart = createChart(canvas, datasets, vehicleChartWindowHours, windowStart, windowEnd, {
      powerMin: 0,
      forcePowerZero: true,
    });
    return;
  }

  updateChart(vehicleChart, datasets, vehicleChartWindowHours, windowStart, windowEnd, {
    powerMin: 0,
    forcePowerZero: true,
  });
}

function renderDashboard(data) {
  mergeEnergySeries(data.energy_chart || []);
  const displayVehicles = hydrateTeslaVehicles(data.vehicles || []);
  const timestamp = data.updated_at || new Date().toISOString();
  appendConnectedSourceContinuityPoints(data, timestamp);
  appendDisconnectedSourcePoints(data, timestamp);
  appendTeslaVehicleFallbackPoints(displayVehicles, timestamp);
  appendTeslaAggregateFallbackPoint(displayVehicles, data, timestamp);
  renderPowerFlow(data.power_flow || {});
  syncChartWindowControls("energy");
  syncChartWindowControls("vehicle");
  renderEnergyChart();
  renderVehicleChart();
  renderAutomationPanel(data.automation_panel || {});
  renderChargers(data.chargers || []);
  renderBatteries(data.batteries || []);
  renderPlants(data.plants || []);
  renderVehicles(displayVehicles);
  renderBatteryRail(displayVehicles, data.batteries || [], data.power_flow || {});
  renderSources(data.sources || [], displayVehicles, data.chargers || [], data.power_flow || {});
  renderUpdatedAt(data.updated_at);
}

function toggleAutomationPanel() {
  automationPanelOpen = !automationPanelOpen;
  renderAutomationPanel(window.__INITIAL_DASHBOARD__.automation_panel || {});
}

function setupHeaderMenu() {
  const menuRoot = document.getElementById("header-menu");
  const toggle = document.getElementById("header-menu-toggle");
  const menuList = document.getElementById("header-menu-list");
  if (!menuRoot || !toggle || !menuList) return;

  const setOpen = (open) => {
    menuRoot.classList.toggle("is-open", open);
    menuList.hidden = !open;
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  };

  toggle.addEventListener("click", (event) => {
    event.stopPropagation();
    setOpen(menuList.hidden);
  });

  menuList.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => setOpen(false));
  });

  document.addEventListener("click", (event) => {
    if (!menuRoot.contains(event.target)) {
      setOpen(false);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      setOpen(false);
    }
  });
}

document.getElementById("automation-toggle")?.addEventListener("click", toggleAutomationPanel);
document.getElementById("charge-toggle")?.addEventListener("click", toggleAutomationPanel);

document.querySelectorAll("[data-hours][data-chart-target]").forEach((button) => {
  button.addEventListener("click", () => {
    setChartWindowHours(button.getAttribute("data-chart-target"), Number(button.getAttribute("data-hours")));
  });
});

document.getElementById("chart-window-range")?.addEventListener("input", (event) => {
  setChartWindowHours("energy", event.currentTarget.value);
});

document.getElementById("chart-window-input")?.addEventListener("change", (event) => {
  setChartWindowHours("energy", event.currentTarget.value);
});

document.getElementById("vehicle-chart-window-range")?.addEventListener("input", (event) => {
  setChartWindowHours("vehicle", event.currentTarget.value);
});

document.getElementById("vehicle-chart-window-input")?.addEventListener("change", (event) => {
  setChartWindowHours("vehicle", event.currentTarget.value);
});

async function refreshDashboard() {
  try {
    const response = await fetch("/api/dashboard");
    const data = await response.json();
    window.__INITIAL_DASHBOARD__ = data;
    try {
      renderDashboard(data);
    } catch (error) {
      console.error("Dashboard render failed", error);
    }
  } catch (error) {
    console.error("Dashboard refresh failed", error);
  }
}

setupHeaderMenu();
loadChartHistoryCache();
loadTeslaVehicleCache();
try {
  renderDashboard(initialData);
} catch (error) {
  console.error("Initial dashboard render failed", error);
}
window.setInterval(refreshDashboard, refreshSeconds * 1000);
