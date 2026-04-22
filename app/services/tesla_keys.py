from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import Settings, resolve_path


WELL_KNOWN_TESLA_PUBLIC_KEY_PATH = "/.well-known/appspecific/com.tesla.3p.public-key.pem"


def _resolve_key_path(path_value: str) -> Path:
    path = resolve_path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_public_key_path(settings: Settings) -> Path:
    return _resolve_key_path(settings.tesla_public_key_path)


def get_private_key_path(settings: Settings) -> Path:
    return _resolve_key_path(settings.tesla_private_key_path)


def ensure_tesla_keypair(settings: Settings) -> tuple[Path, Path] | None:
    public_key_path = get_public_key_path(settings)
    private_key_path = get_private_key_path(settings)

    if public_key_path.exists() and private_key_path.exists():
        return public_key_path, private_key_path

    if not settings.tesla_auto_generate_keys:
        return None

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()

    private_key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_key_path.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return public_key_path, private_key_path
