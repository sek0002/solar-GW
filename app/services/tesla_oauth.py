from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from app.config import Settings, resolve_path
from app.services.tesla_keys import WELL_KNOWN_TESLA_PUBLIC_KEY_PATH


class TeslaOAuthError(RuntimeError):
    """Raised when the Tesla OAuth flow cannot be completed."""


def is_tesla_oauth_configured(settings: Settings) -> bool:
    return bool(settings.tesla_client_id and settings.tesla_client_secret and settings.tesla_redirect_uri)


def build_authorize_url(settings: Settings, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.tesla_client_id,
        "redirect_uri": settings.tesla_redirect_uri,
        "scope": settings.tesla_scope,
        "state": state,
    }
    return f"{settings.tesla_auth_url}?{urlencode(params)}"


def build_pairing_url(settings: Settings) -> str | None:
    if not settings.tesla_partner_domain:
        return None
    return f"https://www.tesla.com/_ak/{settings.tesla_partner_domain}"


def get_public_key_url(settings: Settings) -> str | None:
    if settings.tesla_public_key_url:
        return settings.tesla_public_key_url
    if not settings.tesla_partner_domain:
        if not settings.tesla_redirect_uri:
            return None
        redirect = urlsplit(settings.tesla_redirect_uri)
        if not redirect.scheme or not redirect.netloc:
            return None
        return f"{redirect.scheme}://{redirect.netloc}{WELL_KNOWN_TESLA_PUBLIC_KEY_PATH}"
    return f"https://{settings.tesla_partner_domain}{WELL_KNOWN_TESLA_PUBLIC_KEY_PATH}"


def build_state() -> str:
    return secrets.token_urlsafe(24)


def _token_store_path(settings: Settings) -> Path:
    path = resolve_path(settings.tesla_token_store_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_saved_tokens(settings: Settings) -> dict | None:
    path = _token_store_path(settings)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_tokens(settings: Settings, payload: dict) -> None:
    path = _token_store_path(settings)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def clear_tokens(settings: Settings) -> None:
    path = _token_store_path(settings)
    if path.exists():
        path.unlink()


async def exchange_code_for_token(settings: Settings, code: str) -> dict:
    if not is_tesla_oauth_configured(settings):
        raise TeslaOAuthError("Tesla OAuth is not configured.")

    data = {
        "grant_type": "authorization_code",
        "client_id": settings.tesla_client_id,
        "client_secret": settings.tesla_client_secret,
        "code": code,
        "redirect_uri": settings.tesla_redirect_uri,
        "audience": settings.tesla_api_base_url,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            settings.tesla_token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    response.raise_for_status()
    payload = response.json()
    payload["created_at"] = int(time.time())
    save_tokens(settings, payload)
    return payload


async def refresh_access_token(settings: Settings, refresh_token: str) -> dict:
    if not is_tesla_oauth_configured(settings):
        raise TeslaOAuthError("Tesla OAuth is not configured.")

    data = {
        "grant_type": "refresh_token",
        "client_id": settings.tesla_client_id,
        "client_secret": settings.tesla_client_secret,
        "refresh_token": refresh_token,
        "audience": settings.tesla_api_base_url,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            settings.tesla_token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    response.raise_for_status()
    payload = response.json()
    payload["created_at"] = int(time.time())
    save_tokens(settings, payload)
    return payload


async def get_valid_access_token(settings: Settings) -> str | None:
    if settings.tesla_access_token:
        return settings.tesla_access_token

    tokens = load_saved_tokens(settings)
    if not tokens:
        return None

    access_token = tokens.get("access_token")
    expires_in = int(tokens.get("expires_in", 0))
    created_at = int(tokens.get("created_at", 0))
    now = int(time.time())
    if access_token and now < created_at + max(expires_in - 120, 0):
        return access_token

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return access_token

    refreshed = await refresh_access_token(settings, refresh_token)
    return refreshed.get("access_token")
