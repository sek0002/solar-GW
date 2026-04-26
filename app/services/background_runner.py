from __future__ import annotations

import asyncio

from app.config import Settings
from app.providers.tesla import invalidate_tesla_snapshot_cache
from app.services.automation_state import (
    mark_charge_stop_checked,
    mark_automation_applied,
    should_apply_automation,
    should_retry_charge_stop,
    update_manual_charge_result,
)
from app.services.dashboard import build_dashboard_data
from app.services.tesla_commands import apply_automation_panel, enforce_charge_stop_for_panel


async def maybe_apply_automation(settings: Settings, panel) -> dict | None:
    if panel.stop_charging_required or panel.effective_target_amps is None:
        if should_apply_automation(panel):
            mark_automation_applied(panel)
        return None
    if not should_apply_automation(panel):
        return None
    command_result = await apply_automation_panel(settings, panel)
    invalidate_tesla_snapshot_cache()
    update_manual_charge_result(command_result)
    mark_automation_applied(panel)
    return command_result


async def maybe_enforce_charge_stop(settings: Settings, data) -> dict | None:
    panel = data.automation_panel
    tesla_charging_active = any(
        vehicle.source == "Tesla Vehicle" and str(vehicle.charging_state or "").lower() == "charging"
        for vehicle in data.vehicles
    )
    if not should_retry_charge_stop(panel, tesla_charging_active):
        return None
    command_result = await enforce_charge_stop_for_panel(settings, panel)
    invalidate_tesla_snapshot_cache()
    update_manual_charge_result(command_result)
    mark_charge_stop_checked(panel)
    return command_result


async def run_background_cycle(settings: Settings):
    data = await build_dashboard_data(settings)
    await maybe_apply_automation(settings, data.automation_panel)
    await maybe_enforce_charge_stop(settings, data)
    return data


async def background_sampler_loop(settings: Settings) -> None:
    interval = max(30, int(settings.background_history_interval_seconds or 0))
    while True:
        try:
            await run_background_cycle(settings)
        except Exception:
            # Keep background sampling resilient; live requests can still surface provider issues.
            pass
        await asyncio.sleep(interval)
