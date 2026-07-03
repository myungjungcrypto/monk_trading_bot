"""Typed settings loaded from the environment (.env).

Uses pydantic-settings so every value is validated once at startup instead of
scattered ``os.getenv`` calls. Import :data:`settings` where you need config.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ directory — used to resolve relative paths like the endpoint map.
BACKEND_ROOT = Path(__file__).resolve().parent.parent


class VariationalSettings(BaseSettings):
    """Config for the Variational direct-API connector."""

    model_config = SettingsConfigDict(
        env_prefix="VARIATIONAL_",
        env_file=BACKEND_ROOT / ".env",
        extra="ignore",
    )

    private_key: str = Field(
        default="",
        description="Trading wallet private key (0x-prefixed hex). Never commit.",
    )
    api_base: str = Field(
        default="https://omni-client-api.prod.ap-northeast-1.variational.io",
        description="Omni web-client backend base URL.",
    )
    chain_id: int = Field(default=42161, description="Arbitrum One.")
    endpoint_map: str = Field(
        default="config/variational_endpoints.json",
        description="Path (relative to backend/) to the HAR-derived endpoint map.",
    )
    dry_run: bool = Field(
        default=True,
        description="If true, sign+log but do not POST orders to the exchange.",
    )

    @property
    def endpoint_map_path(self) -> Path:
        p = Path(self.endpoint_map)
        return p if p.is_absolute() else BACKEND_ROOT / p


@lru_cache
def get_variational_settings() -> VariationalSettings:
    return VariationalSettings()
