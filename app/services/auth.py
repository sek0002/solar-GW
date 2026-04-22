from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from threading import Lock

from fastapi import HTTPException, Request

from app.config import Settings


_attempt_lock = Lock()
_attempt_state: dict[str, dict[str, float | int]] = {}


def is_otp_auth_configured(settings: Settings) -> bool:
    return bool(settings.app_auth_secret and settings.app_otp_totp_secret)


def _urlsafe_b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def _normalize_totp_secret(secret: str) -> bytes:
    compact = "".join(secret.strip().split()).upper()
    padding = "=" * (-len(compact) % 8)
    return base64.b32decode(f"{compact}{padding}", casefold=True)


def _totp_at(secret: str, timestamp: int, digits: int = 6, period: int = 30) -> str:
    counter = int(timestamp // period)
    key = _normalize_totp_secret(secret)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def verify_otp_code(secret: str, code: str, now: int | None = None) -> bool:
    candidate = "".join(char for char in code if char.isdigit())
    if len(candidate) != 6:
        return False
    timestamp = int(time.time() if now is None else now)
    for drift in (-30, 0, 30):
        if hmac.compare_digest(_totp_at(secret, timestamp + drift), candidate):
            return True
    return False


def _session_signature(secret: str, payload: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return _urlsafe_b64encode(digest)


def create_session_token(settings: Settings, now: int | None = None) -> str:
    issued_at = int(time.time() if now is None else now)
    payload = {
        "iat": issued_at,
        "exp": issued_at + settings.app_session_hours * 3600,
        "nonce": secrets.token_urlsafe(18),
    }
    encoded_payload = _urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _session_signature(settings.app_auth_secret or "", encoded_payload)
    return f"{encoded_payload}.{signature}"


def verify_session_token(settings: Settings, token: str | None, now: int | None = None) -> bool:
    if not token or not settings.app_auth_secret:
        return False
    try:
        payload, signature = token.split(".", 1)
    except ValueError:
        return False
    if not hmac.compare_digest(_session_signature(settings.app_auth_secret, payload), signature):
        return False
    try:
        decoded = json.loads(_urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return False
    timestamp = int(time.time() if now is None else now)
    return int(decoded.get("exp", 0)) > timestamp


def get_client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def is_rate_limited(settings: Settings, client_key: str, now: float | None = None) -> bool:
    timestamp = time.time() if now is None else now
    with _attempt_lock:
        state = _attempt_state.get(client_key)
        if not state:
            return False
        blocked_until = float(state.get("blocked_until", 0))
        if blocked_until > timestamp:
            return True
        if blocked_until:
            _attempt_state.pop(client_key, None)
    return False


def record_login_failure(settings: Settings, client_key: str, now: float | None = None) -> None:
    timestamp = time.time() if now is None else now
    with _attempt_lock:
        state = _attempt_state.get(client_key, {"count": 0, "blocked_until": 0.0})
        count = int(state.get("count", 0)) + 1
        blocked_until = float(state.get("blocked_until", 0))
        if count >= settings.app_auth_max_attempts:
            blocked_until = timestamp + settings.app_auth_lockout_minutes * 60
            count = 0
        _attempt_state[client_key] = {"count": count, "blocked_until": blocked_until}


def clear_login_failures(client_key: str) -> None:
    with _attempt_lock:
        _attempt_state.pop(client_key, None)


def require_authenticated_request(request: Request, settings: Settings) -> None:
    if not is_otp_auth_configured(settings):
        raise HTTPException(status_code=503, detail="OTP login is not configured.")
    if not verify_session_token(settings, request.cookies.get(settings.app_auth_cookie_name)):
        raise HTTPException(status_code=401, detail="Authentication required.")
