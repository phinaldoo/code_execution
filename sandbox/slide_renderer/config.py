from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("slide_renderer")

_PRODUCTION_ENV_NAMES = {"prod", "production"}
_CONFIG_PARSE_ERRORS: list[str] = []


def _first_env(names: tuple[str, ...]) -> tuple[str, str] | tuple[None, None]:
    for name in names:
        raw = os.getenv(name)
        if raw is not None:
            return name, raw
    return None, None


def _get_int(names: str | tuple[str, ...], default: int, *, min_value: int) -> int:
    """Get integer from environment variable with default and minimum value."""
    env_names = (names,) if isinstance(names, str) else names
    name, raw = _first_env(env_names)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        _CONFIG_PARSE_ERRORS.append(
            f"{name} must be an integer >= {min_value}; got {raw!r}"
        )
        return default
    if value < min_value:
        _CONFIG_PARSE_ERRORS.append(
            f"{name} must be an integer >= {min_value}; got {value}"
        )
        return min_value
    return value


def _get_bool(names: str | tuple[str, ...], default: bool) -> bool:
    """Get boolean from environment variable with default."""
    env_names = (names,) if isinstance(names, str) else names
    name, raw = _first_env(env_names)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    _CONFIG_PARSE_ERRORS.append(
        f"{name} must be a boolean value (true/false); got {raw!r}"
    )
    return default


def _get_csv(names: str | tuple[str, ...]) -> tuple[str, ...]:
    """Get comma-separated values from environment variable as tuple."""
    env_names = (names,) if isinstance(names, str) else names
    _, raw = _first_env(env_names)
    values = [value.strip() for value in (raw or "").split(",") if value.strip()]
    return tuple(values)


def _normalize_environment_name(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    return normalized or "development"


def _active_rendering_version() -> str:
    explicit = (
        os.getenv("SLIDE_RENDERING_VERSION")
        or os.getenv("RENDERING_VERSION")
        or os.getenv("ACTIVE_RENDERING_VERSION")
    )
    if explicit:
        return explicit.strip().lower()
    return "v2" if _get_bool("BETA", False) else "v1"


@dataclass(frozen=True)
class Settings:
    environment_name: str
    development_mode: bool
    enable_docs: bool
    allow_insecure_production_configuration: bool
    render_timeout_seconds: int
    render_queue_timeout_ms: int
    page_load_timeout_ms: int
    max_concurrent_renders: int
    api_key_auth_enabled: bool
    api_keys: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    max_request_body_bytes: int
    max_html_chars: int
    max_input_files: int
    max_slides: int
    max_asset_bytes: int
    max_total_asset_bytes: int
    max_render_output_bytes: int
    active_rendering_version: str

    @property
    def docs_enabled(self) -> bool:
        return self.development_mode or self.enable_docs

    @property
    def is_production(self) -> bool:
        return self.environment_name in _PRODUCTION_ENV_NAMES

    @property
    def beta(self) -> bool:
        return self.active_rendering_version == "v2"


SETTINGS = Settings(
    environment_name=_normalize_environment_name(os.getenv("APP_ENV") or os.getenv("ENVIRONMENT")),
    development_mode=_get_bool("DEVELOPMENT_MODE", False),
    enable_docs=_get_bool("ENABLE_DOCS", False),
    allow_insecure_production_configuration=_get_bool(
        "ALLOW_INSECURE_PRODUCTION_CONFIGURATION",
        False,
    ),
    render_timeout_seconds=_get_int("RENDER_TIMEOUT_SECONDS", 180, min_value=5),
    render_queue_timeout_ms=_get_int("RENDER_QUEUE_TIMEOUT_MS", 500, min_value=1),
    page_load_timeout_ms=_get_int("PAGE_LOAD_TIMEOUT_MS", 30_000, min_value=1_000),
    max_concurrent_renders=_get_int("MAX_CONCURRENT_RENDERS", 2, min_value=1),
    api_key_auth_enabled=_get_bool("API_KEY_AUTH_ENABLED", True),
    api_keys=_get_csv("API_KEYS"),
    allowed_hosts=_get_csv("ALLOWED_HOSTS") or ("*",),
    max_request_body_bytes=_get_int(
        ("RENDER_MAX_REQUEST_BODY_BYTES", "MAX_RENDER_REQUEST_BODY_BYTES", "MAX_REQUEST_BODY_BYTES"),
        180_000_000,
        min_value=1_024,
    ),
    max_html_chars=_get_int(
        ("RENDER_MAX_HTML_CHARS", "MAX_RENDER_HTML_CHARS", "MAX_HTML_CHARS"),
        2_000_000,
        min_value=1_000,
    ),
    max_input_files=_get_int(
        ("RENDER_MAX_INPUT_FILES", "MAX_RENDER_INPUT_FILES", "MAX_INPUT_FILES"),
        32,
        min_value=1,
    ),
    max_slides=_get_int(
        ("RENDER_MAX_SLIDES", "MAX_RENDER_SLIDES", "MAX_SLIDES"),
        200,
        min_value=1,
    ),
    max_asset_bytes=_get_int(
        ("RENDER_MAX_ASSET_BYTES", "MAX_RENDER_ASSET_BYTES", "MAX_ASSET_BYTES"),
        25_000_000,
        min_value=1_024,
    ),
    max_total_asset_bytes=_get_int(
        ("RENDER_MAX_TOTAL_ASSET_BYTES", "MAX_RENDER_TOTAL_ASSET_BYTES", "MAX_TOTAL_ASSET_BYTES"),
        120_000_000,
        min_value=1_024,
    ),
    max_render_output_bytes=_get_int(
        ("RENDER_MAX_OUTPUT_BYTES", "MAX_RENDER_OUTPUT_BYTES"),
        220_000_000,
        min_value=1_024,
    ),
    active_rendering_version=_active_rendering_version(),
)


def validate_runtime_configuration() -> None:
    """Validate runtime configuration and production guardrails."""
    errors: list[str] = list(_CONFIG_PARSE_ERRORS)
    warnings: list[str] = []

    if SETTINGS.active_rendering_version not in {"v1", "v2"}:
        errors.append("SLIDE_RENDERING_VERSION must be either 'v1' or 'v2'")

    if SETTINGS.max_total_asset_bytes < SETTINGS.max_asset_bytes:
        errors.append("RENDER_MAX_TOTAL_ASSET_BYTES must be greater than or equal to RENDER_MAX_ASSET_BYTES")

    if SETTINGS.max_request_body_bytes < SETTINGS.max_html_chars:
        errors.append("RENDER_MAX_REQUEST_BODY_BYTES must be greater than or equal to RENDER_MAX_HTML_CHARS")

    if SETTINGS.max_request_body_bytes < SETTINGS.max_total_asset_bytes:
        errors.append(
            "RENDER_MAX_REQUEST_BODY_BYTES must be greater than or equal to RENDER_MAX_TOTAL_ASSET_BYTES"
        )

    if SETTINGS.page_load_timeout_ms > SETTINGS.render_timeout_seconds * 1000:
        errors.append("PAGE_LOAD_TIMEOUT_MS must not exceed RENDER_TIMEOUT_SECONDS * 1000")

    if SETTINGS.render_queue_timeout_ms > SETTINGS.render_timeout_seconds * 1000:
        errors.append("RENDER_QUEUE_TIMEOUT_MS must not exceed RENDER_TIMEOUT_SECONDS * 1000")

    if SETTINGS.max_render_output_bytes < SETTINGS.max_asset_bytes:
        errors.append("RENDER_MAX_OUTPUT_BYTES must be greater than or equal to RENDER_MAX_ASSET_BYTES")

    if SETTINGS.is_production:
        if SETTINGS.development_mode:
            warnings.append("DEVELOPMENT_MODE must be false in production")
        if SETTINGS.enable_docs:
            warnings.append("ENABLE_DOCS must be false in production")
        if not SETTINGS.api_key_auth_enabled:
            warnings.append("API_KEY_AUTH_ENABLED must be true in production")
        if "*" in SETTINGS.allowed_hosts:
            warnings.append("ALLOWED_HOSTS must not include '*' in production")

    if errors:
        raise RuntimeError("; ".join(errors))

    if warnings:
        message = "; ".join(warnings)
        if SETTINGS.allow_insecure_production_configuration:
            logger.warning(
                "Starting with insecure production configuration because "
                "ALLOW_INSECURE_PRODUCTION_CONFIGURATION=true: %s",
                message,
            )
            return
        raise RuntimeError(message)
