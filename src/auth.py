"""
auth.py — credential loading and Google Ads API client construction.

SECURITY: every secret is read from the environment (loaded from a local .env
file via python-dotenv). Nothing sensitive is hardcoded or stored in config.yaml.
See .env.example for the full list of required variables.

This module is intentionally side-effect-light: importing it does NOT build a
client or touch the network. Build a client explicitly via get_client().
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Union

import yaml
from dotenv import load_dotenv

# The google-ads import is deferred into get_client() so the rest of the package
# (config loading, dry-run wiring, unit tests) imports cleanly without the
# dependency installed or live credentials present.

# Project root = the directory that contains this src/ package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Variables that MUST be present to authenticate against the Google Ads API.
REQUIRED_ENV_VARS = (
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
    "GOOGLE_ADS_CUSTOMER_ID",
)

PathLike = Union[str, os.PathLike]


def load_env(dotenv_path: Optional[PathLike] = None) -> None:
    """Load variables from .env into the process environment (idempotent).

    override=False so a value already exported in the real shell/CI environment
    wins over the .env file — the safer default for production.
    """
    path = Path(dotenv_path) if dotenv_path else PROJECT_ROOT / ".env"
    load_dotenv(dotenv_path=path, override=False)


def _clean_customer_id(raw: str) -> str:
    """Customer IDs are 10 digits; strip the dashes/spaces the API rejects."""
    return raw.replace("-", "").replace(" ", "").strip()


def get_credentials() -> Dict[str, Any]:
    """Build and validate the credentials dict for GoogleAdsClient.

    Raises EnvironmentError naming every missing variable at once, instead of
    failing opaquely inside the google-ads library.
    """
    load_env()

    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        raise EnvironmentError(
            "Missing required Google Ads credentials in environment/.env:\n  - "
            + "\n  - ".join(missing)
            + "\nCopy .env.example to .env and fill in the values."
        )

    return {
        "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
        "login_customer_id": _clean_customer_id(
            os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"]
        ),
        # proto-plus messages are friendlier to work with than raw protobuf.
        "use_proto_plus": True,
    }


def get_customer_id() -> str:
    """The operating account ID (10 digits, no dashes) used in GAQL queries."""
    load_env()
    raw = os.getenv("GOOGLE_ADS_CUSTOMER_ID")
    if not raw:
        raise EnvironmentError(
            "GOOGLE_ADS_CUSTOMER_ID is not set; copy .env.example to .env."
        )
    return _clean_customer_id(raw)


def get_client():
    """Construct an authenticated GoogleAdsClient from the env credentials.

    The google-ads dependency is imported here (lazily) so this is the single
    point that requires the library and valid credentials to be present.
    """
    try:
        from google.ads.googleads.client import GoogleAdsClient
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ImportError(
            "The 'google-ads' package is not installed. "
            "Run 'pip install -r requirements.txt' first."
        ) from exc

    # Optional: pin the API version for deterministic behavior across library
    # upgrades (GOOGLE_ADS_API_VERSION, e.g. "v24"). Default = library's latest,
    # which never gets stranded when Google sunsets an old version.
    api_version = os.getenv("GOOGLE_ADS_API_VERSION")
    if api_version:
        return GoogleAdsClient.load_from_dict(get_credentials(), version=api_version)
    return GoogleAdsClient.load_from_dict(get_credentials())


def load_config(path: Optional[PathLike] = None) -> Dict[str, Any]:
    """Load config.yaml (safety caps, rule thresholds, automation gates)."""
    cfg_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"config.yaml not found at {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}
