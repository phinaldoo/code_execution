from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

VERSION_FILE = Path(__file__).with_name("version.json")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
APP_VERSION = "0.0.0"
APP_VERSION_TAG = "v0.0.0"


def _read_version_file() -> dict[str, Any]:
    with VERSION_FILE.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError(f"{VERSION_FILE} must contain a JSON object")
    return payload


def _normalize_version(raw_value: Any) -> str:
    value = str(raw_value or "").strip()
    if value.startswith("v"):
        value = value[1:]
    if not SEMVER_RE.fullmatch(value):
        raise RuntimeError(f"{VERSION_FILE} contains invalid version {raw_value!r}")
    return value


def load_app_version() -> tuple[str, str]:
    payload = _read_version_file()
    version = _normalize_version(payload.get("version") or payload.get("tag"))
    tag = str(payload.get("tag") or f"v{version}").strip()
    if tag != f"v{version}":
        raise RuntimeError(f"{VERSION_FILE} tag must be v{version}")
    return version, tag


def get_version_payload() -> dict[str, Any]:
    version, tag = load_app_version()
    app_env = os.getenv("APP_ENV", "").strip().lower()
    public_beta = app_env in {"beta", "public_beta", "public-beta"} or os.getenv(
        "PUBLIC_BETA_MODE", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    return {
        "version": version,
        "tag": tag,
        "api_contract_version": 1,
        "beta": public_beta,
        "active_execution_version": "v1",
        "default_execution_version": "v1",
        "supported_execution_versions": ["v1"],
        "available_execution_versions": ["v1"],
        "features": {
            "gateway_version_headers": True,
            "persistent_sessions": True,
            "input_files": True,
            "pip_packages": True,
        },
    }


APP_VERSION, APP_VERSION_TAG = load_app_version()
