from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import Settings
from app.services.tesla_oauth import get_public_key_url


@dataclass
class TeslaPartnerStatus:
    domain: str | None
    public_key_url: str | None
    public_key_http_status: int | None = None
    public_key_error: str | None = None
    tesla_registered_key: str | None = None
    tesla_registration_error: str | None = None


async def get_partner_access_token(settings: Settings) -> str:
    if not settings.tesla_client_id or not settings.tesla_client_secret:
        raise RuntimeError("Tesla client credentials are not configured.")
    data = {
        "grant_type": "client_credentials",
        "client_id": settings.tesla_client_id,
        "client_secret": settings.tesla_client_secret,
        "audience": settings.tesla_api_base_url,
        "scope": settings.tesla_partner_scope,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            settings.tesla_token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Tesla partner token response did not include an access token.")
    return token


async def build_partner_status(settings: Settings) -> TeslaPartnerStatus:
    domain = settings.tesla_partner_domain
    public_key_url = get_public_key_url(settings)
    status = TeslaPartnerStatus(domain=domain, public_key_url=public_key_url)

    if public_key_url:
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(public_key_url)
            status.public_key_http_status = response.status_code
            if response.status_code >= 400:
                status.public_key_error = f"Public key URL returned HTTP {response.status_code}."
        except httpx.HTTPError as exc:
            status.public_key_error = str(exc)

    if not domain:
        status.tesla_registration_error = "TESLA_PARTNER_DOMAIN is not configured."
        return status

    try:
        token = await get_partner_access_token(settings)
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{settings.tesla_api_base_url}/api/1/partner_accounts/public_key",
                params={"domain": domain},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        response.raise_for_status()
        payload = response.json()
        status.tesla_registered_key = payload.get("response") or payload.get("public_key") or payload.get("publicKey")
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:600]
        status.tesla_registration_error = f"Tesla returned HTTP {exc.response.status_code}: {body}"
    except Exception as exc:
        status.tesla_registration_error = str(exc)

    return status


async def register_partner_domain(settings: Settings) -> dict:
    domain = settings.tesla_partner_domain
    if not domain:
        raise RuntimeError("TESLA_PARTNER_DOMAIN is not configured.")
    token = await get_partner_access_token(settings)
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{settings.tesla_api_base_url}/api/1/partner_accounts",
            json={"domain": domain},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    response.raise_for_status()
    return response.json()
