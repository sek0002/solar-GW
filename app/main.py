from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import Settings, get_settings
from app.services.auth import (
    clear_login_failures,
    create_session_token,
    get_client_key,
    is_otp_auth_configured,
    is_rate_limited,
    record_login_failure,
    require_authenticated_request,
    verify_otp_code,
    verify_session_token,
)
from app.services.dashboard import build_dashboard_data
from app.services.tesla_partner import build_partner_status, register_partner_domain
from app.services.tesla_keys import WELL_KNOWN_TESLA_PUBLIC_KEY_PATH, ensure_tesla_keypair, get_public_key_path
from app.services.automation_state import GlobalAutomationPayload, ManualChargePayload, RuleTogglePayload, update_global_automation, update_manual_charge, update_rule
from app.services.tesla_oauth import (
    TeslaOAuthError,
    build_authorize_url,
    build_pairing_url,
    build_state,
    clear_tokens,
    exchange_code_for_token,
    get_public_key_url,
    is_tesla_oauth_configured,
    load_saved_tokens,
)

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="solar-GW")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

PUBLIC_PATHS = {"/login", WELL_KNOWN_TESLA_PUBLIC_KEY_PATH}


class OTPAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/static") or path in PUBLIC_PATHS:
            return await call_next(request)

        settings = get_settings()
        if not is_otp_auth_configured(settings):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "OTP login is not configured."}, status_code=503)
            return RedirectResponse(url="/login", status_code=303)

        if verify_session_token(settings, request.cookies.get(settings.app_auth_cookie_name)):
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse({"detail": "Authentication required."}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)


app.add_middleware(OTPAuthMiddleware)


def _set_auth_cookie(response: RedirectResponse, settings: Settings, token: str) -> None:
    response.set_cookie(
        settings.app_auth_cookie_name,
        token,
        max_age=settings.app_session_hours * 3600,
        httponly=True,
        secure=settings.app_auth_cookie_secure,
        samesite="lax",
    )


def _clear_auth_cookie(response: RedirectResponse, settings: Settings) -> None:
    response.delete_cookie(settings.app_auth_cookie_name, httponly=True, samesite="lax")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, settings: Settings = Depends(get_settings)) -> HTMLResponse:
    if verify_session_token(settings, request.cookies.get(settings.app_auth_cookie_name)):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "settings": settings,
            "otp_enabled": is_otp_auth_configured(settings),
            "error_message": None,
        },
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, settings: Settings = Depends(get_settings)):
    if not is_otp_auth_configured(settings):
        raise HTTPException(status_code=503, detail="OTP login is not configured.")
    client_key = get_client_key(request)
    if is_rate_limited(settings, client_key):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "settings": settings,
                "otp_enabled": True,
                "error_message": "Too many attempts. Wait a few minutes and try again.",
            },
            status_code=429,
        )
    body = (await request.body()).decode("utf-8")
    otp_code = parse_qs(body).get("otp", [""])[0]
    if not verify_otp_code(settings.app_otp_totp_secret or "", otp_code):
        record_login_failure(settings, client_key)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "settings": settings,
                "otp_enabled": True,
                "error_message": "Invalid one-time passcode.",
            },
            status_code=401,
        )
    clear_login_failures(client_key)
    response = RedirectResponse(url="/", status_code=303)
    _set_auth_cookie(response, settings, create_session_token(settings))
    return response


@app.get("/logout")
async def logout(settings: Settings = Depends(get_settings)):
    response = RedirectResponse(url="/login", status_code=303)
    _clear_auth_cookie(response, settings)
    return response


@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request, settings: Settings = Depends(get_settings)) -> HTMLResponse:
    require_authenticated_request(request, settings)
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
            "app_authenticated": True,
            "tesla_pairing_url": build_pairing_url(settings),
            "tesla_public_key_url": get_public_key_url(settings),
        },
    )


@app.get("/api/dashboard")
async def dashboard_api(request: Request, settings: Settings = Depends(get_settings)):
    require_authenticated_request(request, settings)
    data = await build_dashboard_data(settings)
    return data.model_dump(mode="json")


@app.post("/api/automation/rule")
async def automation_rule_toggle(request: Request, payload: RuleTogglePayload, settings: Settings = Depends(get_settings)):
    require_authenticated_request(request, settings)
    update_rule(payload.rule_id, payload.enabled)
    return {"ok": True}


@app.post("/api/automation/global")
async def automation_global_toggle(request: Request, payload: GlobalAutomationPayload, settings: Settings = Depends(get_settings)):
    require_authenticated_request(request, settings)
    update_global_automation(payload.enabled)
    return {"ok": True}


@app.post("/api/automation/manual-charge")
async def automation_manual_charge(request: Request, payload: ManualChargePayload, settings: Settings = Depends(get_settings)):
    require_authenticated_request(request, settings)
    update_manual_charge(payload.enabled, payload.target_amps)
    return {"ok": True}


@app.get("/auth/tesla/login")
async def tesla_login(request: Request, settings: Settings = Depends(get_settings)):
    require_authenticated_request(request, settings)
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
    require_authenticated_request(request, settings)
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
async def tesla_logout(request: Request, settings: Settings = Depends(get_settings)):
    require_authenticated_request(request, settings)
    clear_tokens(settings)
    return RedirectResponse(url="/")


@app.get("/admin/tesla-partner", response_class=HTMLResponse)
async def tesla_partner_admin(
    request: Request,
    flash: str | None = Query(default=None),
    tone: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
):
    require_authenticated_request(request, settings)
    partner_status = await build_partner_status(settings)
    return templates.TemplateResponse(
        request,
        "admin_tesla_partner.html",
        {
            "request": request,
            "settings": settings,
            "partner_status": partner_status,
            "flash_message": flash,
            "flash_tone": tone,
        },
    )


@app.post("/admin/tesla-partner/register")
async def tesla_partner_register(request: Request, settings: Settings = Depends(get_settings)):
    require_authenticated_request(request, settings)
    try:
        await register_partner_domain(settings)
    except httpx.HTTPStatusError as exc:
        message = f"Tesla register failed with HTTP {exc.response.status_code}: {exc.response.text[:400]}"
        return RedirectResponse(url=f"/admin/tesla-partner?flash={message}&tone=warn", status_code=303)
    except Exception as exc:
        return RedirectResponse(url=f"/admin/tesla-partner?flash={str(exc)}&tone=warn", status_code=303)
    return RedirectResponse(url="/admin/tesla-partner?flash=Tesla partner registration request completed.", status_code=303)


@app.get("/auth/tesla/pair")
async def tesla_pair(request: Request, settings: Settings = Depends(get_settings)):
    require_authenticated_request(request, settings)
    pairing_url = build_pairing_url(settings)
    if not pairing_url:
        raise HTTPException(
            status_code=400,
            detail="Tesla partner domain is not configured. Set TESLA_PARTNER_DOMAIN first.",
        )
    return RedirectResponse(url=pairing_url)


@app.get(WELL_KNOWN_TESLA_PUBLIC_KEY_PATH, response_class=PlainTextResponse)
async def tesla_public_key(request: Request, settings: Settings = Depends(get_settings)):
    ensure_tesla_keypair(settings)
    public_key_path = get_public_key_path(settings)
    if not public_key_path.exists():
        raise HTTPException(status_code=404, detail="Tesla public key is not available.")
    return public_key_path.read_text(encoding="utf-8")
