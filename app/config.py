from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "solar-GW"
    environment: str = "development"
    dashboard_title: str = "Home Energy Control"
    refresh_interval_seconds: int = 30
    request_timeout_seconds: float = 15.0
    demo_mode: bool = True
    app_auth_secret: str | None = None
    app_otp_totp_secret: str | None = None
    app_otp_issuer: str = "solar-GW"
    app_session_hours: int = 12
    app_auth_cookie_name: str = "solar_gw_session"
    app_auth_cookie_secure: bool = False
    app_auth_lockout_minutes: int = 15
    app_auth_max_attempts: int = 5

    tesla_api_base_url: str = "https://fleet-api.prd.na.vn.cloud.tesla.com"
    tesla_auth_url: str = "https://auth.tesla.com/oauth2/v3/authorize"
    tesla_token_url: str = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
    tesla_client_id: str | None = None
    tesla_client_secret: str | None = None
    tesla_redirect_uri: str | None = None
    tesla_scope: str = "openid offline_access user_data vehicle_device_data vehicle_charging_cmds"
    tesla_access_token: str | None = None
    tesla_vehicle_vins: str = ""
    tesla_token_store_path: str = ".data/tesla_oauth.json"
    wall_connector_name: str = "Wall Connector 3"
    wall_connector_location: str = "Garage"
    wall_connector_max_kw: float = 11.0
    wall_connector_circuit_amps: int = 32

    growatt_overview_url: str | None = None
    growatt_battery_url: str | None = None
    growatt_token: str | None = None
    growatt_server_url: str = "https://openapi-au.growatt.com"
    growatt_platform: str = "hybrid_inverter"

    goodwe_overview_url: str | None = None
    goodwe_battery_url: str | None = None
    goodwe_token: str | None = None
    goodwe_username: str | None = None
    goodwe_password: str | None = None
    goodwe_plant_id: str | None = None
    goodwe_api_url: str = "https://semsportal.com/api/"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def resolve_path(path_str: str) -> Path:
    path = Path(path_str)
    if path.is_absolute():
        return path
    return Path.cwd() / path
