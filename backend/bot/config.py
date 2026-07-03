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
        default="https://omni.variational.io",
        description="Backend base URL the Omni web client talks to (paths under /api). "
        "Verified via HAR capture 2026-07-03.",
    )
    chain_id: int = Field(default=42161, description="Arbitrum One.")
    max_slippage: float = Field(
        default=0.0005,
        description="max_slippage sent with market orders (fraction; web UI used 0.0002).",
    )
    endpoint_map: str = Field(
        default="config/variational_endpoints.json",
        description="Path (relative to backend/) to the HAR-derived endpoint map.",
    )
    dry_run: bool = Field(
        default=True,
        description="If true, sign+log but do not POST orders to the exchange.",
    )
    transport: str = Field(
        default="curl",
        description="HTTP transport: 'curl' (curl_cffi impersonation), 'browser' "
        "(Playwright fetch-in-page, most robust vs Cloudflare), or 'httpx' (tests).",
    )
    browser_headless: bool = Field(
        default=True,
        description="Run the 'browser' transport headless. Set false to solve an "
        "interactive Cloudflare challenge by hand once.",
    )
    browser_executable: str = Field(
        default="",
        description="Path to an existing Chromium/Chrome binary for the 'browser' "
        "transport. Empty uses Playwright's managed browser (needs `playwright install`).",
    )
    browser_user_data_dir: str = Field(
        default="",
        description="Persistent browser profile dir. Set it so Cloudflare clearance "
        "and login session survive bot restarts. Empty uses a fresh profile each run.",
    )
    impersonate: str = Field(
        default="chrome",
        description="curl_cffi browser to impersonate for Cloudflare (e.g. 'chrome'). "
        "Empty string disables impersonation and uses plain httpx.",
    )
    user_agent: str = Field(
        default="",
        description="Override User-Agent (httpx fallback path). Empty uses a Chrome default.",
    )
    cf_clearance: str = Field(
        default="",
        description="Optional cf_clearance cookie for the browser-bootstrap fallback "
        "(tools/cf_bootstrap.py). Only valid from the IP that solved the challenge.",
    )

    @property
    def endpoint_map_path(self) -> Path:
        p = Path(self.endpoint_map)
        return p if p.is_absolute() else BACKEND_ROOT / p


@lru_cache
def get_variational_settings() -> VariationalSettings:
    return VariationalSettings()
