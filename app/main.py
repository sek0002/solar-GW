from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.services.dashboard import build_dashboard_data
from app.services.automation_state import GlobalAutomationPayload, ManualChargePayload, RuleTogglePayload, update_global_automation, update_manual_charge, update_rule
from app.services.tesla_oauth import (
    TeslaOAuthError,
    build_authorize_url,
    build_state,
    clear_tokens,
    exchange_code_for_token,
    is_tesla_oauth_configured,
    load_saved_tokens,
)

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="solar-GW")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request, settings: Settings = Depends(get_settings)) -> HTMLResponse:
    data = await build_dashboard_data(settings)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "settings": settings,
            "initial_data": data.model_dump(mode="json"),
            "tesla_oauth_enabled": is_tesla_oauth_configured(settings),
            "tesla_connected": bool(settings.tesla_access_token or load_saved_tokens(settings)),
        },
    )


@app.get("/api/dashboard")
async def dashboard_api(settings: Settings = Depends(get_settings)):
    data = await build_dashboard_data(settings)
    return data.model_dump(mode="json")


@app.post("/api/automation/rule")
async def automation_rule_toggle(payload: RuleTogglePayload):
    update_rule(payload.rule_id, payload.enabled)
    return {"ok": True}


@app.post("/api/automation/global")
async def automation_global_toggle(payload: GlobalAutomationPayload):
    update_global_automation(payload.enabled)
    return {"ok": True}


@app.post("/api/automation/manual-charge")
async def automation_manual_charge(payload: ManualChargePayload):
    update_manual_charge(payload.enabled, payload.target_amps)
    return {"ok": True}


@app.get("/auth/tesla/login")
async def tesla_login(settings: Settings = Depends(get_settings)):
    if not is_tesla_oauth_configured(settings):
        raise HTTPException(status_code=400, detail="Tesla OAuth is not configured.")

    state = build_state()
    response = RedirectResponse(build_authorize_url(settings, state))
    response.set_cookie(
        "tesla_oauth_state",
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/auth/tesla/callback")
async def tesla_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
):
    if error:
        raise HTTPException(status_code=400, detail=f"Tesla authorization failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing Tesla OAuth code or state.")

    expected_state = request.cookies.get("tesla_oauth_state")
    if not expected_state or state != expected_state:
        raise HTTPException(status_code=400, detail="Tesla OAuth state mismatch.")

    try:
        await exchange_code_for_token(settings, code)
    except (httpx.HTTPError, TeslaOAuthError) as exc:
        raise HTTPException(status_code=400, detail=f"Tesla token exchange failed: {exc}") from exc

    response = RedirectResponse(url="/")
    response.delete_cookie("tesla_oauth_state")
    return response


@app.get("/auth/tesla/logout")
async def tesla_logout():
    settings = get_settings()
    clear_tokens(settings)
    return RedirectResponse(url="/")
