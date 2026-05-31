"""
Code Execution Gateway - FastAPI service that manages sandbox containers.

This service exposes authenticated APIs to create isolated sandbox sessions,
execute code inside them, and retrieve execution artifacts.
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import posixpath
import re
import secrets
import socket
import tarfile
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, NamedTuple, Optional
from urllib.parse import urlparse

import docker
import docker.errors
import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
from docker.utils.socket import frames_iter

from state import InMemoryStateBackend, RedisStateBackend, SessionInfo, StateBackend
from version import APP_VERSION, APP_VERSION_TAG, get_version_payload


def str_to_bool(value: Optional[str], default: bool = True) -> bool:
    """Convert a string value to boolean, using default if value is None."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def split_csv(value: Optional[str]) -> list[str]:
    """Split a CSV string into a list of non-empty, stripped items."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def int_from_env(names: str | tuple[str, ...], default: int, *, min_value: int = 1) -> int:
    """Read an integer from the first configured env var name."""
    env_names = (names,) if isinstance(names, str) else names
    for name in env_names:
        raw_value = os.getenv(name)
        if raw_value is None:
            continue
        try:
            return max(min_value, int(raw_value))
        except ValueError:
            return default
    return default


def resolve_slide_rendering_version() -> str:
    """Resolve the active slide renderer version, preserving the old BETA switch."""
    explicit = (
        os.getenv("SLIDE_RENDERING_VERSION")
        or os.getenv("RENDERING_VERSION")
        or os.getenv("ACTIVE_RENDERING_VERSION")
    )
    if explicit:
        return explicit.strip().lower()
    return "v2" if str_to_bool(os.getenv("BETA"), default=False) else "v1"


APP_ENV = os.getenv("APP_ENV", "production").strip().lower()
IS_PRODUCTION = APP_ENV in {"prod", "production"}
PUBLIC_BETA_MODE = str_to_bool(
    os.getenv("PUBLIC_BETA_MODE"),
    default=APP_ENV in {"beta", "public_beta", "public-beta"},
)

SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "code-sandbox:latest")
SANDBOX_RUNTIME = os.getenv("SANDBOX_RUNTIME", "").strip()
STRONG_SANDBOX_RUNTIMES = split_csv(os.getenv("STRONG_SANDBOX_RUNTIMES")) or [
    "runsc",
    "kata",
    "kata-runtime",
    "io.containerd.runsc.v1",
    "io.containerd.kata.v2",
]
REQUIRE_STRONG_SANDBOX_ISOLATION = str_to_bool(
    os.getenv("REQUIRE_STRONG_SANDBOX_ISOLATION"),
    default=PUBLIC_BETA_MODE,
)
DOCKER_CLIENT_TIMEOUT = int(os.getenv("DOCKER_CLIENT_TIMEOUT", "30"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_EXECUTIONS", "10"))
DEFAULT_TIMEOUT = int(os.getenv("DEFAULT_TIMEOUT", "30"))
MAX_TIMEOUT = int(os.getenv("MAX_TIMEOUT", "120"))
SANDBOX_MEM_LIMIT = os.getenv("SANDBOX_MEM_LIMIT", "512m")
SANDBOX_CPU_PERIOD = int(os.getenv("SANDBOX_CPU_PERIOD", "100000"))
SANDBOX_CPU_QUOTA = int(os.getenv("SANDBOX_CPU_QUOTA", "100000"))
SANDBOX_PIDS_LIMIT = int(os.getenv("SANDBOX_PIDS_LIMIT", "256"))
SANDBOX_TMP_ROOT_SIZE = os.getenv("SANDBOX_TMP_ROOT_SIZE", "512m")
SANDBOX_SHM_SIZE = os.getenv("SANDBOX_SHM_SIZE", "128m")
SANDBOX_HOME_TMPFS_SIZE = os.getenv("SANDBOX_HOME_TMPFS_SIZE", "256m")
SANDBOX_READ_ONLY_ROOTFS = str_to_bool(os.getenv("SANDBOX_READ_ONLY_ROOTFS", "true"))
SANDBOX_NETWORK_MODE = os.getenv("SANDBOX_NETWORK_MODE", "none")
SANDBOX_UID = int(os.getenv("SANDBOX_UID", "10001"))
SANDBOX_GID = int(os.getenv("SANDBOX_GID", "10001"))
SANDBOX_USER = os.getenv("SANDBOX_USER", "sandbox")
USE_DOCKER_DEFAULT_SECCOMP = str_to_bool(os.getenv("USE_DOCKER_DEFAULT_SECCOMP", "true"))
SECCOMP_PROFILE_DAEMON_PATH = (
    os.getenv("SECCOMP_PROFILE_DAEMON_PATH")
    or os.getenv("SECCOMP_PROFILE_PATH")
    or ""
).strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
DOCKER_HOST = os.getenv("DOCKER_HOST", "").strip()
ENABLE_DOCS = str_to_bool(os.getenv("ENABLE_DOCS", "false"))

ENABLE_CORS = str_to_bool(os.getenv("ENABLE_CORS", "true"))
CORS_ALLOW_ORIGINS = split_csv(os.getenv("CORS_ALLOW_ORIGINS"))
CORS_ALLOW_METHODS = split_csv(os.getenv("CORS_ALLOW_METHODS")) or ["GET", "POST", "DELETE", "OPTIONS"]
CORS_ALLOW_HEADERS = split_csv(os.getenv("CORS_ALLOW_HEADERS")) or [
    "Authorization",
    "Content-Type",
    "X-Request-ID",
    "X-API-Key",
]
CORS_ALLOW_CREDENTIALS = str_to_bool(os.getenv("CORS_ALLOW_CREDENTIALS", "true"), default=True)

REQUIRE_AUTH = str_to_bool(os.getenv("REQUIRE_AUTH"), default=IS_PRODUCTION)
METRICS_AUTH_REQUIRED = str_to_bool(
    os.getenv("METRICS_AUTH_REQUIRED"),
    default=IS_PRODUCTION,
)
API_KEYS_RAW = split_csv(os.getenv("API_KEYS") or os.getenv("API_KEY"))
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHMS = split_csv(os.getenv("JWT_ALGORITHMS")) or ["HS256"]
JWT_ISSUER = os.getenv("JWT_ISSUER")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE")
JWT_TENANT_CLAIM = os.getenv("JWT_TENANT_CLAIM", "tenant_id")
MIN_PRODUCTION_SECRET_LENGTH = 32

MAX_INPUT_FILES = int(os.getenv("MAX_INPUT_FILES", "10"))
MAX_INPUT_FILE_SIZE = int(os.getenv("MAX_INPUT_FILE_SIZE", str(5 * 1024 * 1024)))
MAX_INPUT_TOTAL_SIZE = int(os.getenv("MAX_INPUT_TOTAL_SIZE", str(20 * 1024 * 1024)))
MAX_REQUEST_BODY_SIZE = int(os.getenv("MAX_REQUEST_BODY_SIZE", str(32 * 1024 * 1024)))
MAX_FILE_NAME_LENGTH = int(os.getenv("MAX_FILE_NAME_LENGTH", "128"))
MAX_PIP_PACKAGES = int(os.getenv("MAX_PIP_PACKAGES", "5"))
MAX_PIP_PACKAGE_NAME_LENGTH = int(os.getenv("MAX_PIP_PACKAGE_NAME_LENGTH", "64"))
ALLOW_PIP_INSTALLS = str_to_bool(os.getenv("ALLOW_PIP_INSTALLS", "false"))
PIP_PACKAGE_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*([\[,\]A-Za-z0-9._-]*)?"
    r"(([!=<>~]=?|>=?|<=?)[\w.*]+([,;]([!=<>~]=?|>=?|<=?)[\w.*]+)*)?$"
)

SLIDE_RENDERING_VERSION = resolve_slide_rendering_version()
RENDER_RESULT_PREFIX = "__RENDER_RESULT__:"
RENDER_TIMEOUT_SECONDS = int_from_env("RENDER_TIMEOUT_SECONDS", 180, min_value=5)
RENDER_QUEUE_TIMEOUT_MS = int_from_env("RENDER_QUEUE_TIMEOUT_MS", 500, min_value=1)
RENDER_MAX_CONCURRENT = int_from_env("MAX_CONCURRENT_RENDERS", 2, min_value=1)
RENDER_MAX_REQUEST_BODY_SIZE = int_from_env(
    ("RENDER_MAX_REQUEST_BODY_BYTES", "MAX_RENDER_REQUEST_BODY_BYTES"),
    180_000_000,
    min_value=1_024,
)
RENDER_MAX_HTML_CHARS = int_from_env(
    ("RENDER_MAX_HTML_CHARS", "MAX_RENDER_HTML_CHARS", "MAX_HTML_CHARS"),
    2_000_000,
    min_value=1_000,
)
RENDER_MAX_INPUT_FILES = int_from_env(
    ("RENDER_MAX_INPUT_FILES", "MAX_RENDER_INPUT_FILES"),
    32,
    min_value=1,
)
RENDER_MAX_SLIDES = int_from_env(
    ("RENDER_MAX_SLIDES", "MAX_RENDER_SLIDES", "MAX_SLIDES"),
    200,
    min_value=1,
)
RENDER_MAX_ASSET_BYTES = int_from_env(
    ("RENDER_MAX_ASSET_BYTES", "MAX_RENDER_ASSET_BYTES", "MAX_ASSET_BYTES"),
    25_000_000,
    min_value=1_024,
)
RENDER_MAX_TOTAL_ASSET_BYTES = int_from_env(
    ("RENDER_MAX_TOTAL_ASSET_BYTES", "MAX_RENDER_TOTAL_ASSET_BYTES", "MAX_TOTAL_ASSET_BYTES"),
    120_000_000,
    min_value=1_024,
)
RENDER_MAX_OUTPUT_BYTES = int_from_env(
    ("RENDER_MAX_OUTPUT_BYTES", "MAX_RENDER_OUTPUT_BYTES"),
    220_000_000,
    min_value=1_024,
)
RENDER_PAGE_LOAD_TIMEOUT_MS = int_from_env("PAGE_LOAD_TIMEOUT_MS", 30_000, min_value=1_000)
RENDER_RATE_LIMIT_REQUESTS = int_from_env(
    "RENDER_RATE_LIMIT_REQUESTS_PER_WINDOW",
    10,
    min_value=1,
)
RENDER_RATE_LIMIT_WINDOW_SECONDS = int_from_env(
    "RENDER_RATE_LIMIT_WINDOW_SECONDS",
    60,
    min_value=1,
)
RENDER_SANDBOX_MEM_LIMIT = os.getenv("RENDER_SANDBOX_MEM_LIMIT", "2g")
RENDER_SANDBOX_CPU_PERIOD = int_from_env("RENDER_SANDBOX_CPU_PERIOD", SANDBOX_CPU_PERIOD, min_value=1)
RENDER_SANDBOX_CPU_QUOTA = int_from_env("RENDER_SANDBOX_CPU_QUOTA", SANDBOX_CPU_QUOTA, min_value=1)
RENDER_SANDBOX_PIDS_LIMIT = int_from_env("RENDER_SANDBOX_PIDS_LIMIT", 512, min_value=1)
RENDER_SANDBOX_TMP_ROOT_SIZE = os.getenv("RENDER_SANDBOX_TMP_ROOT_SIZE", "1g")
RENDER_SANDBOX_SHM_SIZE = os.getenv("RENDER_SANDBOX_SHM_SIZE", "512m")
RENDER_SANDBOX_HOME_TMPFS_SIZE = os.getenv("RENDER_SANDBOX_HOME_TMPFS_SIZE", SANDBOX_HOME_TMPFS_SIZE)

RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS_PER_WINDOW", "30"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
CONTAINER_RATE_LIMIT_REQUESTS = int(os.getenv("CONTAINER_RATE_LIMIT_REQUESTS_PER_WINDOW", "10"))
CONTAINER_RATE_LIMIT_WINDOW_SECONDS = int(
    os.getenv("CONTAINER_RATE_LIMIT_WINDOW_SECONDS", "60")
)
MAX_CONTAINERS_PER_PRINCIPAL = int(os.getenv("MAX_CONTAINERS_PER_PRINCIPAL", "3"))
MAX_ACTIVE_SESSIONS = int(os.getenv("MAX_ACTIVE_SESSIONS", "100"))
CONTAINER_CREATE_GUARD_TIMEOUT = int(os.getenv("CONTAINER_CREATE_GUARD_TIMEOUT", "30"))
ALLOW_SANDBOX_ENV_INJECTION = str_to_bool(os.getenv("ALLOW_SANDBOX_ENV_INJECTION", "false"))
REQUIRE_SHARED_STATE = str_to_bool(os.getenv("REQUIRE_SHARED_STATE"), default=IS_PRODUCTION)
REDIS_URL = os.getenv("REDIS_URL", "").strip()

SANDBOX_ENV_TARGET_PATH = os.getenv("SANDBOX_ENV_TARGET_PATH", "/home/sandbox/.env")
_DEFAULT_ENV_SANDBOX_SOURCE = str(Path(__file__).resolve().parents[1] / ".env_sandbox")
SANDBOX_ENV_SOURCE_PATH = os.getenv("SANDBOX_ENV_SOURCE_PATH", _DEFAULT_ENV_SANDBOX_SOURCE)
EXECUTOR_RESULT_PREFIX = "__EXECUTOR_RESULT__:"
FILE_PROVISION_TIMEOUT = int(os.getenv("FILE_PROVISION_TIMEOUT", "30"))
FILE_PROVISION_SCRIPT = r"""
import os
import shutil
import sys
import tarfile
from pathlib import Path

target_root = Path(os.environ["TARGET_DIR"]).resolve()
target_root.mkdir(parents=True, exist_ok=True)

with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as tar:
    for member in tar:
        member_name = member.name
        destination = (target_root / member_name).resolve()
        if destination != target_root and target_root not in destination.parents:
            raise RuntimeError(f"Archive member escapes target directory: {member_name}")
        if member.isdir():
            destination.mkdir(mode=member.mode or 0o700, parents=True, exist_ok=True)
            continue
        if not member.isfile():
            raise RuntimeError(f"Unsupported archive member type: {member_name}")
        source = tar.extractfile(member)
        if source is None:
            raise RuntimeError(f"Unable to read archive member: {member_name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as output:
            shutil.copyfileobj(source, output)
        os.chmod(destination, member.mode or 0o600)
"""
FILE_READ_SCRIPT = r"""
import os
import shutil
import sys
from pathlib import Path

path = Path(os.environ["READ_PATH"]).resolve()
max_bytes = int(os.environ["MAX_BYTES"])

if not str(path).startswith("/tmp/output/"):
    print("requested file is outside /tmp/output", file=sys.stderr)
    raise SystemExit(2)
if not path.is_file():
    print(f"file not found: {path}", file=sys.stderr)
    raise SystemExit(1)
size = path.stat().st_size
if size > max_bytes:
    print(f"file exceeds max size of {max_bytes} bytes", file=sys.stderr)
    raise SystemExit(2)

with path.open("rb") as source:
    shutil.copyfileobj(source, sys.stdout.buffer)
"""


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("gateway")


@dataclass(frozen=True)
class StaticApiKey:
    key_id: str
    secret: str


def parse_static_api_keys(values: list[str]) -> list[StaticApiKey]:
    """Parse static API key strings into StaticApiKey objects, supporting optional key_id prefix."""
    keys: list[StaticApiKey] = []
    for idx, raw_value in enumerate(values):
        key_id = f"key-{idx + 1}"
        secret = raw_value
        if ":" in raw_value:
            key_id, secret = raw_value.split(":", 1)
            key_id = key_id.strip() or f"key-{idx + 1}"
            secret = secret.strip()
        if secret:
            keys.append(StaticApiKey(key_id=key_id, secret=secret))
    return keys


STATIC_API_KEYS = parse_static_api_keys(API_KEYS_RAW)


@dataclass
class AuthContext:
    subject: str
    tenant: Optional[str]
    auth_type: str


class PreparedFile(NamedTuple):
    name: str
    content: bytes


class ExecutorOutputError(ValueError):
    """Raised when the sandbox executor output cannot be parsed safely."""


class RequestBodyTooLarge(ValueError):
    """Raised when an incoming request body exceeds the configured limit."""


execution_semaphore: asyncio.Semaphore
render_semaphore: asyncio.Semaphore
docker_client: docker.DockerClient
state_backend: StateBackend
local_docker_daemon_id: Optional[str] = None
local_docker_daemon_name: Optional[str] = None
SESSION_TIMEOUT_SECONDS = int(os.getenv("SESSION_TIMEOUT_SECONDS", str(20 * 60)))
MAX_SESSION_LIFETIME_SECONDS = int(os.getenv("MAX_SESSION_LIFETIME_SECONDS", str(60 * 60)))
MAX_EXECUTIONS_PER_SESSION = int(os.getenv("MAX_EXECUTIONS_PER_SESSION", "100"))

REQUEST_COUNTER = Counter(
    "gateway_http_requests_total",
    "HTTP requests handled by the gateway",
    ["method", "path", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "gateway_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
)
EXECUTION_COUNTER = Counter(
    "gateway_executions_total",
    "Code execution attempts by outcome",
    ["outcome"],
)
EXECUTION_LATENCY = Histogram(
    "gateway_execution_duration_seconds",
    "Sandbox execution duration in seconds",
)
ACTIVE_EXECUTIONS_GAUGE = Gauge(
    "gateway_active_executions",
    "Current number of active code executions",
)
RENDER_COUNTER = Counter(
    "gateway_slide_renders_total",
    "Slide render attempts by outcome",
    ["outcome"],
)
RENDER_LATENCY = Histogram(
    "gateway_slide_render_duration_seconds",
    "Slide render duration in seconds",
)
ACTIVE_RENDERS_GAUGE = Gauge(
    "gateway_active_slide_renders",
    "Current number of active slide renders",
)

metrics = {
    "total_executions": 0,
    "successful_executions": 0,
    "failed_executions": 0,
    "timed_out_executions": 0,
    "active_executions": 0,
    "total_renders": 0,
    "successful_renders": 0,
    "failed_renders": 0,
    "timed_out_renders": 0,
    "active_renders": 0,
}


def auth_mode_summary() -> str:
    """Return a summary string of enabled authentication modes."""
    modes: list[str] = []
    if JWT_SECRET:
        modes.append("jwt")
    if STATIC_API_KEYS:
        modes.append("api_key")
    if not modes:
        return "disabled"
    return "+".join(modes)


def docker_host_hostname(docker_host: str) -> Optional[str]:
    """Extract the hostname from a Docker host URL string."""
    if not docker_host:
        return None
    parsed = urlparse(docker_host)
    return parsed.hostname.lower() if parsed.hostname else None


def image_reference_is_immutable(image: str) -> bool:
    """Return whether an image reference is pinned enough for production use."""
    if "@sha256:" in image:
        return True
    last_component = image.rsplit("/", 1)[-1]
    if ":" not in last_component:
        return False
    tag = last_component.rsplit(":", 1)[-1].strip().lower()
    return bool(tag and tag != "latest")


def strong_sandbox_runtime_configured() -> bool:
    """Return whether the configured Docker runtime is a known stronger isolation runtime."""
    if not SANDBOX_RUNTIME:
        return False
    return SANDBOX_RUNTIME in set(STRONG_SANDBOX_RUNTIMES)


def parse_optional_float(
    value: Optional[str],
    default: Optional[float] = None,
) -> Optional[float]:
    """Parse a string float, returning a default for missing or invalid values."""
    if not value:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_optional_int(value: Optional[str], default: int = 0) -> int:
    """Parse a string integer, returning a default for missing or invalid values."""
    if not value:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def principal_scope(auth: AuthContext) -> str:
    """Generate a scope string for rate limiting based on subject and tenant."""
    return f"{auth.subject}:{auth.tenant or '-'}"


def validate_runtime_configuration() -> None:
    """Validate runtime configuration settings and raise RuntimeError if invalid."""
    if DEFAULT_TIMEOUT > MAX_TIMEOUT:
        raise RuntimeError("DEFAULT_TIMEOUT must be less than or equal to MAX_TIMEOUT")

    if DOCKER_CLIENT_TIMEOUT < 1:
        raise RuntimeError("DOCKER_CLIENT_TIMEOUT must be at least 1 second")

    if MAX_REQUEST_BODY_SIZE < 1:
        raise RuntimeError("MAX_REQUEST_BODY_SIZE must be at least 1 byte")

    if SLIDE_RENDERING_VERSION not in {"v1", "v2"}:
        raise RuntimeError("SLIDE_RENDERING_VERSION must be either 'v1' or 'v2'")

    if RENDER_TIMEOUT_SECONDS < 5:
        raise RuntimeError("RENDER_TIMEOUT_SECONDS must be at least 5 seconds")

    if RENDER_PAGE_LOAD_TIMEOUT_MS > RENDER_TIMEOUT_SECONDS * 1000:
        raise RuntimeError("PAGE_LOAD_TIMEOUT_MS must not exceed RENDER_TIMEOUT_SECONDS * 1000")

    if RENDER_QUEUE_TIMEOUT_MS > RENDER_TIMEOUT_SECONDS * 1000:
        raise RuntimeError("RENDER_QUEUE_TIMEOUT_MS must not exceed RENDER_TIMEOUT_SECONDS * 1000")

    if RENDER_MAX_REQUEST_BODY_SIZE < RENDER_MAX_HTML_CHARS:
        raise RuntimeError(
            "RENDER_MAX_REQUEST_BODY_BYTES must be greater than or equal to RENDER_MAX_HTML_CHARS"
        )

    if RENDER_MAX_REQUEST_BODY_SIZE < RENDER_MAX_TOTAL_ASSET_BYTES:
        raise RuntimeError(
            "RENDER_MAX_REQUEST_BODY_BYTES must be greater than or equal to RENDER_MAX_TOTAL_ASSET_BYTES"
        )

    if RENDER_MAX_TOTAL_ASSET_BYTES < RENDER_MAX_ASSET_BYTES:
        raise RuntimeError(
            "RENDER_MAX_TOTAL_ASSET_BYTES must be greater than or equal to RENDER_MAX_ASSET_BYTES"
        )

    if RENDER_MAX_OUTPUT_BYTES < RENDER_MAX_ASSET_BYTES:
        raise RuntimeError(
            "RENDER_MAX_OUTPUT_BYTES must be greater than or equal to RENDER_MAX_ASSET_BYTES"
        )

    if SESSION_TIMEOUT_SECONDS < 1:
        raise RuntimeError("SESSION_TIMEOUT_SECONDS must be at least 1 second")

    if MAX_SESSION_LIFETIME_SECONDS < SESSION_TIMEOUT_SECONDS:
        raise RuntimeError(
            "MAX_SESSION_LIFETIME_SECONDS must be greater than or equal to SESSION_TIMEOUT_SECONDS"
        )

    if MAX_EXECUTIONS_PER_SESSION < 1:
        raise RuntimeError("MAX_EXECUTIONS_PER_SESSION must be at least 1")

    if REQUIRE_AUTH and not (JWT_SECRET or STATIC_API_KEYS):
        raise RuntimeError(
            "Authentication is required, but neither JWT nor static API keys are configured."
        )

    if IS_PRODUCTION and STATIC_API_KEYS:
        too_short = [
            key.key_id
            for key in STATIC_API_KEYS
            if len(key.secret) < MIN_PRODUCTION_SECRET_LENGTH
        ]
        if too_short:
            raise RuntimeError(
                "Static API keys must be at least "
                f"{MIN_PRODUCTION_SECRET_LENGTH} characters in production. "
                f"Invalid key ids: {', '.join(too_short)}"
            )

    if IS_PRODUCTION and JWT_SECRET:
        hmac_algorithms = [
            algorithm
            for algorithm in JWT_ALGORITHMS
            if algorithm.upper().startswith("HS")
        ]
        if hmac_algorithms and len(JWT_SECRET) < MIN_PRODUCTION_SECRET_LENGTH:
            raise RuntimeError(
                "JWT_SECRET must be at least "
                f"{MIN_PRODUCTION_SECRET_LENGTH} characters in production when using "
                f"HMAC JWT algorithms. Configured HMAC algorithms: {', '.join(hmac_algorithms)}"
            )

    if ENABLE_CORS:
        if CORS_ALLOW_CREDENTIALS and "*" in CORS_ALLOW_ORIGINS:
            raise RuntimeError(
                "CORS wildcard origins cannot be combined with credentialed requests."
            )
        if IS_PRODUCTION and not CORS_ALLOW_ORIGINS:
            raise RuntimeError(
                "CORS_ALLOW_ORIGINS must be set explicitly in production when CORS is enabled."
            )

    if SANDBOX_NETWORK_MODE not in {"bridge", "none"}:
        raise RuntimeError("SANDBOX_NETWORK_MODE must be either 'bridge' or 'none'")

    if REQUIRE_STRONG_SANDBOX_ISOLATION and not strong_sandbox_runtime_configured():
        allowed = ", ".join(STRONG_SANDBOX_RUNTIMES)
        raise RuntimeError(
            "Strong sandbox isolation is required, but SANDBOX_RUNTIME is not configured "
            f"with a recognized runtime. Set SANDBOX_RUNTIME to one of: {allowed}"
        )

    if not USE_DOCKER_DEFAULT_SECCOMP:
        if not SECCOMP_PROFILE_DAEMON_PATH:
            raise RuntimeError(
                "SECCOMP_PROFILE_DAEMON_PATH must be set when USE_DOCKER_DEFAULT_SECCOMP=false. "
                "Provide an absolute path string that is valid on the Docker daemon host."
            )
        if not Path(SECCOMP_PROFILE_DAEMON_PATH).is_absolute():
            raise RuntimeError(
                "SECCOMP_PROFILE_DAEMON_PATH must be an absolute path string. "
                "This check validates syntax only; the file must exist on the Docker daemon host."
            )

    if REQUIRE_SHARED_STATE and not REDIS_URL:
        raise RuntimeError("REDIS_URL must be configured when shared state is required.")

    if IS_PRODUCTION or PUBLIC_BETA_MODE:
        mode_name = "production/public beta"
        if ENABLE_DOCS:
            raise RuntimeError(f"ENABLE_DOCS must be false in {mode_name}.")
        if not image_reference_is_immutable(SANDBOX_IMAGE):
            raise RuntimeError(
                f"SANDBOX_IMAGE must use an immutable tag or digest in {mode_name}; "
                "floating references such as ':latest' are not allowed."
            )
        if not DOCKER_HOST:
            raise RuntimeError(f"DOCKER_HOST must be configured explicitly in {mode_name}.")
        if DOCKER_HOST.startswith("unix://"):
            raise RuntimeError(
                f"DOCKER_HOST must point at a restricted TCP proxy or remote daemon in {mode_name}; "
                "raw Unix socket access is not allowed."
            )
        parsed_docker = urlparse(DOCKER_HOST)
        if parsed_docker.scheme == "tcp" and parsed_docker.port == 2375:
            raise RuntimeError(
                "DOCKER_HOST must use TLS (port 2376) or ssh:// in production/public beta. "
                "Plain TCP on port 2375 is unencrypted and unsafe."
            )
        if parsed_docker.scheme not in {"tcp", "ssh"}:
            raise RuntimeError(
                "DOCKER_HOST must use tcp:// (with TLS) or ssh:// in production/public beta."
            )
        if docker_host_hostname(DOCKER_HOST) in {
            "docker-proxy",
            "localhost",
            "127.0.0.1",
            "::1",
            "host.docker.internal",
        }:
            raise RuntimeError(
                "Production/public beta gateways must use a dedicated remote Docker daemon. "
                "Local Docker socket proxies and loopback targets are not allowed."
            )

    if PUBLIC_BETA_MODE:
        if not REQUIRE_AUTH:
            raise RuntimeError("REQUIRE_AUTH must be true in public beta mode.")
        if SANDBOX_NETWORK_MODE != "none":
            raise RuntimeError("SANDBOX_NETWORK_MODE must be 'none' in public beta mode.")
        if ALLOW_PIP_INSTALLS:
            raise RuntimeError("ALLOW_PIP_INSTALLS must be false in public beta mode.")
        if ALLOW_SANDBOX_ENV_INJECTION:
            raise RuntimeError("ALLOW_SANDBOX_ENV_INJECTION must be false in public beta mode.")
        if not image_reference_is_immutable(SANDBOX_IMAGE):
            raise RuntimeError("SANDBOX_IMAGE must be immutable in public beta mode.")
        if not strong_sandbox_runtime_configured():
            raise RuntimeError(
                "Public beta mode requires a stronger Docker runtime such as gVisor/runsc or Kata. "
                "Configure SANDBOX_RUNTIME and STRONG_SANDBOX_RUNTIMES."
            )

    if MAX_CONTAINERS_PER_PRINCIPAL < 1:
        raise RuntimeError("MAX_CONTAINERS_PER_PRINCIPAL must be at least 1")

    if MAX_ACTIVE_SESSIONS < 1:
        raise RuntimeError("MAX_ACTIVE_SESSIONS must be at least 1")

    if CONTAINER_CREATE_GUARD_TIMEOUT < 1:
        raise RuntimeError("CONTAINER_CREATE_GUARD_TIMEOUT must be at least 1")


def decode_jwt_token(token: str) -> AuthContext:
    """Decode and validate a JWT token, returning AuthContext with subject and tenant."""
    decode_kwargs = {
        "algorithms": JWT_ALGORITHMS,
        "options": {"require": ["exp", "sub"]},
    }
    if JWT_AUDIENCE:
        decode_kwargs["audience"] = JWT_AUDIENCE
    if JWT_ISSUER:
        decode_kwargs["issuer"] = JWT_ISSUER

    payload = jwt.decode(token, JWT_SECRET, **decode_kwargs)
    subject = str(payload["sub"])
    tenant = payload.get(JWT_TENANT_CLAIM)
    return AuthContext(subject=subject, tenant=tenant, auth_type="jwt")


def decode_static_api_key(token: str) -> Optional[AuthContext]:
    """Validate a static API key against configured keys, returning AuthContext if valid."""
    for api_key in STATIC_API_KEYS:
        if secrets.compare_digest(token, api_key.secret):
            return AuthContext(
                subject=f"api-key:{api_key.key_id}",
                tenant=None,
                auth_type="api_key",
            )
    return None


def authenticate_credentials(
    credentials: Optional[HTTPAuthorizationCredentials],
    *,
    required: bool,
) -> AuthContext:
    """Authenticate credentials using JWT or static API key, returning AuthContext."""
    if not required:
        return AuthContext(subject="anonymous", tenant=None, auth_type="none")

    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    if JWT_SECRET:
        try:
            return decode_jwt_token(token)
        except jwt.InvalidTokenError as exc:
            logger.debug("JWT validation failed: %s", exc)
            if not STATIC_API_KEYS:
                raise HTTPException(
                    status_code=401,
                    detail="Invalid or expired credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                ) from exc

    static_context = decode_static_api_key(token)
    if static_context:
        return static_context

    raise HTTPException(
        status_code=401,
        detail="Invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


security = HTTPBearer(auto_error=False)


def verify_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> AuthContext:
    """FastAPI dependency to verify authentication credentials."""
    return authenticate_credentials(credentials, required=REQUIRE_AUTH)


async def verify_render_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> AuthContext:
    """Authenticate render requests with gateway Bearer auth or legacy X-API-Key."""
    if not REQUIRE_AUTH:
        return AuthContext(subject="anonymous", tenant=None, auth_type="none")

    api_key = request.headers.get("X-API-Key")
    if api_key:
        static_context = decode_static_api_key(api_key.strip())
        if static_context:
            return static_context

    return authenticate_credentials(credentials, required=True)


def verify_metrics_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[AuthContext]:
    """FastAPI dependency to verify authentication for metrics endpoint."""
    if not METRICS_AUTH_REQUIRED:
        return None
    return authenticate_credentials(credentials, required=True)


def infer_network_enabled(container: docker.models.containers.Container) -> bool:
    """Infer whether network is enabled for a container from its configuration."""
    try:
        host_cfg = container.attrs.get("HostConfig", {})
        network_mode = host_cfg.get("NetworkMode", SANDBOX_NETWORK_MODE)
        return network_mode != "none"
    except Exception:
        return SANDBOX_NETWORK_MODE != "none"


def session_is_local(session: SessionInfo) -> bool:
    """Check if a session belongs to the local Docker daemon."""
    if not session.docker_daemon_id:
        return True
    if not local_docker_daemon_id:
        return False
    return session.docker_daemon_id == local_docker_daemon_id


def session_hard_expired(session: SessionInfo, *, now: Optional[float] = None) -> bool:
    """Return whether a session has exceeded its hard lifetime."""
    if session.expires_at is None:
        return False
    return (now or time.time()) >= session.expires_at


def session_idle_expired(session: SessionInfo, *, now: Optional[float] = None) -> bool:
    """Return whether a session has exceeded its idle timeout."""
    return (now or time.time()) - session.last_activity > SESSION_TIMEOUT_SECONDS


def enforce_session_daemon_affinity(session: SessionInfo) -> None:
    """Raise HTTPException if session belongs to a different Docker daemon."""
    if session_is_local(session):
        return
    raise HTTPException(
        status_code=409,
        detail=(
            "Container session is attached to a different execution node. "
            "Route follow-up requests to the original node or use a shared Docker daemon."
        ),
    )


def recover_session_info(container: docker.models.containers.Container) -> SessionInfo:
    """Recover session information from a container's labels and attributes."""
    labels = container.labels or {}
    now = time.time()
    created_at = now
    created_raw = container.attrs.get("Created")
    if created_raw:
        try:
            created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00")).timestamp()
        except Exception:
            created_at = now
    return SessionInfo(
        created_at=created_at,
        last_activity=created_at,
        network_enabled=infer_network_enabled(container),
        owner_subject=labels.get("owner-subject", "unknown"),
        owner_tenant=labels.get("owner-tenant") or None,
        docker_daemon_id=labels.get("docker-daemon-id") or local_docker_daemon_id,
        inject_sandbox_env=(labels.get("inject-sandbox-env") or "0") == "1",
        expires_at=parse_optional_float(
            labels.get("expires-at"),
            created_at + MAX_SESSION_LIFETIME_SECONDS,
        ),
        execution_count=parse_optional_int(labels.get("execution-count"), 0),
    )


async def touch_session(container_id: str) -> Optional[SessionInfo]:
    """Update session last activity timestamp and return session info."""
    return await state_backend.touch_session(
        container_id,
        session_timeout_seconds=SESSION_TIMEOUT_SECONDS,
    )


async def recover_or_remove_managed_container(
    container: docker.models.containers.Container,
    *,
    missing_state_reason: str,
) -> Optional[SessionInfo]:
    """Recover a managed container without resetting shared-state session budgets."""
    existing = await state_backend.get_session(container.id)
    if existing is None:
        if REQUIRE_SHARED_STATE:
            await remove_container(
                container.id,
                reason=missing_state_reason,
                container=container,
            )
            return None
        session = recover_session_info(container)
    else:
        session = existing

    await state_backend.save_session(
        container.id,
        session,
        session_timeout_seconds=SESSION_TIMEOUT_SECONDS,
    )
    return session


async def ensure_session_access(container_id: str, auth: AuthContext) -> SessionInfo:
    """Ensure the authenticated user has access to the container session."""
    session = await state_backend.get_session(container_id)
    if session is None:
        try:
            container = await asyncio.to_thread(docker_client.containers.get, container_id)
        except docker.errors.NotFound as exc:
            raise HTTPException(
                status_code=404,
                detail="Container session not found, or it was shut down due to inactivity.",
            ) from exc

        labels = container.labels or {}
        if labels.get("managed-by") != "code-execution-gateway":
            raise HTTPException(
                status_code=404,
                detail="Container session not found, or it was shut down due to inactivity.",
            )

        if REQUIRE_SHARED_STATE:
            await remove_container(
                container_id,
                reason="missing-shared-session-state",
                container=container,
            )
            raise HTTPException(
                status_code=404,
                detail="Container session state is unavailable; the sandbox was removed.",
            )

        session = recover_session_info(container)
        await state_backend.save_session(
            container_id,
            session,
            session_timeout_seconds=SESSION_TIMEOUT_SECONDS,
        )

    if auth.subject != session.owner_subject:
        raise HTTPException(status_code=403, detail="Container session belongs to another principal")

    if session.owner_tenant and auth.tenant != session.owner_tenant:
        raise HTTPException(status_code=403, detail="Container session belongs to another tenant")

    enforce_session_daemon_affinity(session)

    if session_hard_expired(session):
        await remove_container(container_id, reason="max-session-lifetime")
        raise HTTPException(
            status_code=404,
            detail="Container session expired after reaching its maximum lifetime.",
        )

    return session


async def enforce_rate_limit(
    key: str,
    *,
    limit: int,
    window_seconds: int,
    message: str,
) -> None:
    """Enforce rate limit and raise HTTPException if exceeded."""
    allowed = await state_backend.allow_within_rate_limit(
        key,
        limit=limit,
        window_seconds=window_seconds,
    )
    if not allowed:
        raise HTTPException(status_code=429, detail=message)


async def enforce_container_creation_limits(auth: AuthContext) -> None:
    """Enforce limits on total containers and containers per principal."""
    sessions = await state_backend.list_sessions()
    total_sessions = len(sessions)
    owner_sessions = sum(
        1
        for session in sessions.values()
        if session.owner_subject == auth.subject and session.owner_tenant == auth.tenant
    )

    if total_sessions >= MAX_ACTIVE_SESSIONS:
        raise HTTPException(
            status_code=429,
            detail="The gateway is at its maximum number of active container sessions.",
        )

    if owner_sessions >= MAX_CONTAINERS_PER_PRINCIPAL:
        raise HTTPException(
            status_code=429,
            detail="You have reached the maximum number of active container sessions.",
        )


async def remove_container(
    container_id: str,
    *,
    execution_id: Optional[str] = None,
    reason: str = "cleanup",
    container: Optional[docker.models.containers.Container] = None,
) -> None:
    """Remove a container and clean up its session state."""
    prefix = f"[{execution_id}] " if execution_id else ""
    if container is None:
        session = await state_backend.get_session(container_id)
        if session is not None and not session_is_local(session):
            logger.warning(
                "%sRefusing to remove remote container %s bound to daemon %s from local daemon %s",
                prefix,
                container_id,
                session.docker_daemon_id or "-",
                local_docker_daemon_id or "-",
            )
            return
        try:
            container = await asyncio.to_thread(docker_client.containers.get, container_id)
        except docker.errors.NotFound:
            if session is None or session_is_local(session):
                await state_backend.delete_session(container_id)
            return

    with suppress(docker.errors.NotFound, docker.errors.APIError, RuntimeError):
        await asyncio.to_thread(container.kill)

    removed = False
    try:
        await asyncio.to_thread(container.remove, force=True)
        removed = True
    except docker.errors.NotFound:
        removed = True
    except (docker.errors.APIError, RuntimeError) as exc:
        logger.error(
            "%sFailed to remove container %s (%s): %s",
            prefix, container_id, reason, exc,
        )

    if removed:
        await state_backend.delete_session(container_id)
        logger.info("%sRemoved container %s (%s)", prefix, container_id, reason)
    else:
        logger.warning(
            "%sContainer %s removal unconfirmed; keeping session state for reconciliation",
            prefix, container_id,
        )


async def cleanup_idle_containers() -> None:
    """Background task to clean up idle containers and recover untracked containers."""
    while True:
        try:
            now = time.time()
            sessions = await state_backend.list_sessions()
            idle_ids = [
                cid
                for cid, session in sessions.items()
                if session_is_local(session)
                and (session_idle_expired(session, now=now) or session_hard_expired(session, now=now))
            ]

            for cid in idle_ids:
                await remove_container(cid, reason="idle-timeout")

            try:
                managed_containers = await asyncio.to_thread(
                    docker_client.containers.list,
                    all=True,
                    filters={"label": "managed-by=code-execution-gateway"},
                )
                tracked_ids = set(sessions)
                for container in managed_containers:
                    if container.id not in tracked_ids and container.name.startswith(("sandbox-", "render-")):
                        recovered = await recover_or_remove_managed_container(
                            container,
                            missing_state_reason="untracked-shared-session-state",
                        )
                        if recovered is None:
                            continue
                        logger.info("Recovered untracked managed container %s during cleanup", container.id)
            except Exception as exc:
                logger.error("Error cleaning up untracked containers: %s", exc)

        except Exception as exc:
            logger.error("Error in cleanup task: %s", exc)

        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager for startup and shutdown."""
    global docker_client, execution_semaphore, render_semaphore, state_backend, local_docker_daemon_id, local_docker_daemon_name

    validate_runtime_configuration()
    docker_client = docker.from_env(timeout=DOCKER_CLIENT_TIMEOUT)
    execution_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    render_semaphore = asyncio.Semaphore(RENDER_MAX_CONCURRENT)
    state_backend = RedisStateBackend(REDIS_URL) if REDIS_URL else InMemoryStateBackend()
    await state_backend.connect()

    try:
        docker_info = await asyncio.to_thread(docker_client.info)
        local_docker_daemon_id = str(docker_info.get("ID") or "") or None
        local_docker_daemon_name = str(docker_info.get("Name") or "") or None
    except Exception as exc:
        local_docker_daemon_id = None
        local_docker_daemon_name = None
        logger.warning("Failed to read Docker daemon identity: %s", exc)

    try:
        await asyncio.to_thread(docker_client.images.get, SANDBOX_IMAGE)
        logger.info("Sandbox image '%s' found", SANDBOX_IMAGE)
    except docker.errors.ImageNotFound:
        logger.warning(
            "Sandbox image '%s' not found. Build it first: docker build -t code-sandbox sandbox/",
            SANDBOX_IMAGE,
        )

    try:
        managed_containers = await asyncio.to_thread(
            docker_client.containers.list,
            filters={"label": "managed-by=code-execution-gateway"},
        )
        for container in managed_containers:
            session = await recover_or_remove_managed_container(
                container,
                missing_state_reason="missing-shared-session-state",
            )
            if session is None:
                continue
            logger.info(
                "Recovered container %s for subject=%s tenant=%s network=%s daemon=%s",
                container.id,
                session.owner_subject,
                session.owner_tenant or "-",
                "on" if session.network_enabled else "off",
                session.docker_daemon_id or "-",
            )
    except Exception as exc:
        logger.warning("Failed to recover existing containers: %s", exc)

    logger.info(
        "Gateway started env=%s public_beta=%s auth=%s max_concurrent=%s render_concurrent=%s render_version=%s default_timeout=%ss session_ttl=%ss max_session_lifetime=%ss max_executions_per_session=%s network=%s read_only_rootfs=%s runtime=%s docker_default_seccomp=%s state_backend=%s docker_daemon_id=%s",
        APP_ENV,
        PUBLIC_BETA_MODE,
        auth_mode_summary(),
        MAX_CONCURRENT,
        RENDER_MAX_CONCURRENT,
        SLIDE_RENDERING_VERSION,
        DEFAULT_TIMEOUT,
        SESSION_TIMEOUT_SECONDS,
        MAX_SESSION_LIFETIME_SECONDS,
        MAX_EXECUTIONS_PER_SESSION,
        SANDBOX_NETWORK_MODE,
        SANDBOX_READ_ONLY_ROOTFS,
        SANDBOX_RUNTIME or "default",
        USE_DOCKER_DEFAULT_SECCOMP,
        type(state_backend).__name__,
        local_docker_daemon_id or "-",
    )

    cleanup_task = asyncio.create_task(cleanup_idle_containers())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        await state_backend.close()
        docker_client.close()
        logger.info("Gateway shut down")


app = FastAPI(
    title="Code Execution Gateway",
    description="Secure, isolated Python code execution service for LLM models",
    version=APP_VERSION,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url=None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
    lifespan=lifespan,
)

if ENABLE_CORS and CORS_ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOW_ORIGINS,
        allow_credentials=CORS_ALLOW_CREDENTIALS,
        allow_methods=CORS_ALLOW_METHODS,
        allow_headers=CORS_ALLOW_HEADERS,
    )


def metrics_path_label(request: Request) -> str:
    """Return a low-cardinality path label for metrics."""
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if route_path:
        return str(route_path)
    return "__unmatched__"


def request_body_limit_for_path(path: str) -> int:
    """Return the request body limit for an incoming path."""
    if path in {"/api/render", "/api/v1/render"}:
        return RENDER_MAX_REQUEST_BODY_SIZE
    return MAX_REQUEST_BODY_SIZE


def request_content_length_exceeds_limit(request: Request, *, max_bytes: Optional[int] = None) -> bool:
    """Check Content-Length against the configured request body limit."""
    raw_content_length = request.headers.get("content-length")
    if not raw_content_length:
        return False
    try:
        return int(raw_content_length) > (max_bytes or MAX_REQUEST_BODY_SIZE)
    except ValueError:
        return False


def limited_receive_factory(receive, *, max_bytes: int):
    """Wrap an ASGI receive callable and reject bodies that exceed max_bytes."""
    received_bytes = 0

    async def limited_receive():
        nonlocal received_bytes
        message = await receive()
        if message.get("type") != "http.request":
            return message

        body = message.get("body", b"")
        received_bytes += len(body)
        if received_bytes > max_bytes:
            raise RequestBodyTooLarge(
                f"Request body exceeds maximum size of {max_bytes} bytes"
            )
        return message

    return limited_receive


@app.middleware("http")
async def request_metrics_middleware(request: Request, call_next):
    """Middleware to track request metrics and add request ID to responses."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    start = time.monotonic()
    limited_methods = {"POST", "PUT", "PATCH"}
    max_body_bytes = request_body_limit_for_path(request.url.path)

    if request.method in limited_methods and request_content_length_exceeds_limit(request, max_bytes=max_body_bytes):
        elapsed = time.monotonic() - start
        path_label = metrics_path_label(request)
        REQUEST_COUNTER.labels(request.method, path_label, "413").inc()
        REQUEST_LATENCY.labels(request.method, path_label).observe(elapsed)
        return JSONResponse(
            status_code=413,
            content={
                "detail": f"Request body too large. Maximum size is {max_body_bytes} bytes."
            },
            headers={"X-Request-ID": request_id},
        )

    if request.method in limited_methods:
        request._receive = limited_receive_factory(  # noqa: SLF001 - Starlette exposes no public receive setter.
            request.receive,
            max_bytes=max_body_bytes,
        )

    try:
        response = await call_next(request)
    except RequestBodyTooLarge:
        elapsed = time.monotonic() - start
        path_label = metrics_path_label(request)
        REQUEST_COUNTER.labels(request.method, path_label, "413").inc()
        REQUEST_LATENCY.labels(request.method, path_label).observe(elapsed)
        logger.warning(
            "[%s] %s %s rejected after %.3fs: request body too large",
            request_id,
            request.method,
            request.url.path,
            elapsed,
        )
        return JSONResponse(
            status_code=413,
            content={
                "detail": f"Request body too large. Maximum size is {max_body_bytes} bytes."
            },
            headers={"X-Request-ID": request_id},
        )
    except Exception:
        elapsed = time.monotonic() - start
        path_label = metrics_path_label(request)
        REQUEST_COUNTER.labels(
            request.method,
            path_label,
            "500",
        ).inc()
        REQUEST_LATENCY.labels(request.method, path_label).observe(elapsed)
        logger.exception("[%s] %s %s failed after %.3fs", request_id, request.method, request.url.path, elapsed)
        raise

    elapsed = time.monotonic() - start
    status_code = str(response.status_code)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Code-Execution-Version"] = APP_VERSION
    response.headers["X-Code-Execution-Version-Tag"] = APP_VERSION_TAG
    path_label = metrics_path_label(request)
    REQUEST_COUNTER.labels(request.method, path_label, status_code).inc()
    REQUEST_LATENCY.labels(request.method, path_label).observe(elapsed)
    logger.info(
        "[%s] %s %s -> %s in %.3fs",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        elapsed,
    )
    return response


class FileInput(BaseModel):
    name: str = Field(..., description="File name including extension")
    content: str = Field(..., description="Base64 encoded content of the file")

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        sanitized = value.strip()
        if not sanitized:
            raise ValueError("File name cannot be empty")
        if len(sanitized) > MAX_FILE_NAME_LENGTH:
            raise ValueError(f"File name too long (max {MAX_FILE_NAME_LENGTH} chars)")
        if ".." in sanitized or sanitized.startswith(("/", "\\")) or "\\" in sanitized:
            raise ValueError("File name contains invalid path segments")
        if any(part in {"", ".", ".."} for part in sanitized.split("/")):
            raise ValueError("File name contains invalid components")
        return sanitized


class ExecuteRequest(BaseModel):
    container_id: str = Field(..., description="The ID of the active container session")
    language: Literal["python", "bash"] = Field(
        "python",
        description="Language to execute (python or bash)",
    )
    code: str = Field(..., description="Code to execute", min_length=1, max_length=100_000)
    timeout: Optional[int] = Field(
        default=None,
        description=f"Execution timeout in seconds (max {MAX_TIMEOUT})",
        ge=1,
        le=MAX_TIMEOUT,
    )
    enable_network: Optional[bool] = Field(
        default=False,
        description="Whether to enable network access in the sandbox",
    )
    pip_packages: list[str] = Field(
        default_factory=list,
        description="Optional list of pip packages to install before execution",
    )
    files: list[FileInput] = Field(
        default_factory=list,
        description="Input files to copy to the container before executing",
    )

    @field_validator("pip_packages", mode="before")
    @classmethod
    def validate_pip_packages_input(cls, value: Any) -> Any:
        if value is None:
            return []
        return value

    @field_validator("pip_packages")
    @classmethod
    def validate_pip_package_list(cls, packages: list[str]) -> list[str]:
        normalized: list[str] = []
        for pkg in packages:
            name = pkg.strip()
            if not name:
                raise ValueError("Package name cannot be empty")
            if len(name) > MAX_PIP_PACKAGE_NAME_LENGTH:
                raise ValueError(f"Package name too long (max {MAX_PIP_PACKAGE_NAME_LENGTH} chars)")
            if not PIP_PACKAGE_PATTERN.match(name):
                raise ValueError("Package name contains invalid characters")
            normalized.append(name)

        if not ALLOW_PIP_INSTALLS and normalized:
            raise ValueError("Pip installations are disabled")
        if len(normalized) > MAX_PIP_PACKAGES:
            raise ValueError(f"Too many pip packages (max {MAX_PIP_PACKAGES})")
        return normalized

    @field_validator("files")
    @classmethod
    def validate_file_count(cls, files: list[FileInput]) -> list[FileInput]:
        if len(files) > MAX_INPUT_FILES:
            raise ValueError(f"Too many input files (max {MAX_INPUT_FILES})")
        return files


class ContainerResponse(BaseModel):
    container_id: str
    status: str
    uptime_seconds: float
    last_activity: float
    expires_at: Optional[float] = None
    execution_count: int = 0
    max_executions: int = MAX_EXECUTIONS_PER_SESSION
    docker_daemon_id: Optional[str] = None


class FileOutput(BaseModel):
    name: str
    content: Optional[str] = None
    mime_type: str
    size: int
    error: Optional[str] = None


class ExecuteResponse(BaseModel):
    execution_id: str
    stdout: str
    stderr: str
    error: Optional[str] = None
    error_type: Optional[str] = None
    files: list[FileOutput] = Field(default_factory=list)
    execution_time: float
    install_time: Optional[float] = None
    timed_out: bool = False


_SAFE_RENDER_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class RenderInputFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_name: str = Field(..., min_length=1, max_length=128)
    base64_content: str = Field(
        ...,
        min_length=1,
        max_length=35_000_000,
        validation_alias=AliasChoices("base64_content", "base64"),
    )

    @field_validator("file_name")
    @classmethod
    def validate_file_name(cls, value: str) -> str:
        if "/" in value or "\\" in value:
            raise ValueError("file_name must not contain path separators")
        if not _SAFE_RENDER_FILENAME_RE.fullmatch(value):
            raise ValueError(
                "file_name may only contain letters, numbers, dots, underscores and hyphens"
            )
        if value in {".", ".."}:
            raise ValueError("invalid file_name")
        return value


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    html: str = Field(..., min_length=1)
    input_files: list[RenderInputFile] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_input_files(self) -> "RenderRequest":
        seen: set[str] = set()
        for input_file in self.input_files:
            if input_file.file_name in seen:
                raise ValueError(f"duplicate input file name: {input_file.file_name}")
            seen.add(input_file.file_name)
        return self


@dataclass(frozen=True)
class RenderSandboxResult:
    render_id: str
    file_name: str
    rendering_version: str
    content: bytes
    media_type: str
    slide_count: int
    execution_time: float


def validate_render_payload_limits(payload: RenderRequest) -> None:
    """Reject render requests that exceed gateway-known renderer limits."""
    if len(payload.html) > RENDER_MAX_HTML_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"html is too large (>{RENDER_MAX_HTML_CHARS} characters)",
        )
    if len(payload.input_files) > RENDER_MAX_INPUT_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"too many input_files (max {RENDER_MAX_INPUT_FILES})",
        )


class CreateContainerRequest(BaseModel):
    enable_network: bool = False
    inject_sandbox_env: bool = False


@app.get("/")
async def root() -> JSONResponse:
    """Service information endpoint."""
    return JSONResponse(
        {
            "message": "Code Execution Gateway",
            "version": APP_VERSION_TAG,
            "execute_endpoint": "/execute",
            "render_endpoint": "/api/render",
            "version_endpoint": "/version",
        }
    )


@app.get("/version")
async def version() -> JSONResponse:
    """Application and execution contract version endpoint."""
    return JSONResponse(get_version_payload())


@app.get("/livez")
async def livez() -> JSONResponse:
    """Renderer-compatible liveness check endpoint."""
    return JSONResponse({"status": "ok"})


@app.get("/internal/auth/apikey", include_in_schema=False)
async def internal_api_key_auth(_: AuthContext = Depends(verify_render_auth)) -> Response:
    """Renderer-compatible internal auth probe endpoint."""
    return Response(status_code=204)


@app.post("/api/render")
@app.post("/api/v1/render")
async def render_endpoint(
    payload: RenderRequest,
    auth: AuthContext = Depends(verify_render_auth),
) -> Response:
    """Render presentation HTML to a PPTX archive in the shared sandbox runtime."""
    render_id = str(uuid.uuid4())[:12]
    acquired_slot = False
    metrics["total_renders"] += 1
    metrics["active_renders"] += 1
    ACTIVE_RENDERS_GAUGE.inc()
    start = time.monotonic()

    try:
        validate_render_payload_limits(payload)
        try:
            await asyncio.wait_for(
                render_semaphore.acquire(),
                timeout=RENDER_QUEUE_TIMEOUT_MS / 1000,
            )
            acquired_slot = True
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=429,
                detail="renderer is busy, please retry shortly",
                headers={"Retry-After": "1"},
            ) from exc

        await enforce_rate_limit(
            f"render:{principal_scope(auth)}",
            limit=RENDER_RATE_LIMIT_REQUESTS,
            window_seconds=RENDER_RATE_LIMIT_WINDOW_SECONDS,
            message="Render rate limit exceeded for this principal.",
        )
        await enforce_rate_limit(
            f"container-create:{principal_scope(auth)}",
            limit=CONTAINER_RATE_LIMIT_REQUESTS,
            window_seconds=CONTAINER_RATE_LIMIT_WINDOW_SECONDS,
            message="Container creation rate limit exceeded for this principal.",
        )

        try:
            result = await render_presentation_in_sandbox(
                payload=payload,
                auth=auth,
                render_id=render_id,
                timeout=RENDER_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=503,
                detail="Container creation is temporarily saturated. Please retry shortly.",
            ) from exc

        metrics["successful_renders"] += 1
        RENDER_COUNTER.labels("successful").inc()
        RENDER_LATENCY.observe(time.monotonic() - start)
        logger.info(
            "[%s] Render complete version=%s slides=%s bytes=%s time=%ss",
            render_id,
            result.rendering_version,
            result.slide_count,
            len(result.content),
            result.execution_time,
        )

        headers = {
            "Content-Disposition": f'attachment; filename="{result.file_name}"',
            "X-Rendering-Version": result.rendering_version,
            "X-Renderer-Version": APP_VERSION,
            "X-Renderer-Version-Tag": APP_VERSION_TAG,
            "X-Code-Execution-Version": APP_VERSION,
            "X-Code-Execution-Version-Tag": APP_VERSION_TAG,
            "X-Slide-Count": str(result.slide_count),
            "X-Render-ID": result.render_id,
        }
        return Response(content=result.content, media_type=result.media_type, headers=headers)
    except HTTPException as exc:
        if exc.status_code == 504:
            metrics["timed_out_renders"] += 1
            RENDER_COUNTER.labels("timed_out").inc()
        else:
            metrics["failed_renders"] += 1
            RENDER_COUNTER.labels("failed").inc()
        raise
    except Exception as exc:
        metrics["failed_renders"] += 1
        RENDER_COUNTER.labels("failed").inc()
        logger.exception("[%s] Render failed", render_id)
        raise HTTPException(status_code=500, detail="rendering failed") from exc
    finally:
        if acquired_slot:
            render_semaphore.release()
        metrics["active_renders"] = max(0, metrics["active_renders"] - 1)
        ACTIVE_RENDERS_GAUGE.dec()


def build_execution_error_response(
    *,
    execution_id: str,
    error: str,
    error_type: str,
    execution_time: float = 0,
    timed_out: bool = False,
) -> ExecuteResponse:
    """Construct a consistent execution error response."""
    return ExecuteResponse(
        execution_id=execution_id,
        stdout="",
        stderr="",
        error=error,
        error_type=error_type,
        execution_time=execution_time,
        timed_out=timed_out,
    )


def parse_executor_result(raw_output: str) -> dict[str, Any]:
    """Parse the structured executor payload from Docker exec output."""
    lines = [line.strip() for line in raw_output.strip().splitlines() if line.strip()]
    if not lines:
        raise ExecutorOutputError("Executor returned no output")

    for line in reversed(lines):
        if not line.startswith(EXECUTOR_RESULT_PREFIX):
            continue
        payload = line[len(EXECUTOR_RESULT_PREFIX) :].strip()
        if not payload:
            continue
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            continue

    for line in reversed(lines):
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue

    raise ExecutorOutputError("No structured JSON payload found in executor output")


def parse_prefixed_result(raw_output: str, *, prefix: str, empty_error: str) -> dict[str, Any]:
    """Parse the last structured prefixed JSON payload from command output."""
    lines = [line.strip() for line in raw_output.strip().splitlines() if line.strip()]
    if not lines:
        raise ExecutorOutputError(empty_error)

    for line in reversed(lines):
        if not line.startswith(prefix):
            continue
        payload = line[len(prefix) :].strip()
        if not payload:
            continue
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            continue

    raise ExecutorOutputError("No structured JSON payload found in renderer output")


def parse_render_result(raw_output: str) -> dict[str, Any]:
    """Parse a structured render payload from Docker exec output."""
    return parse_prefixed_result(
        raw_output,
        prefix=RENDER_RESULT_PREFIX,
        empty_error="Renderer returned no output",
    )


def _read_env_source_bytes() -> Optional[bytes]:
    """Read the sandbox environment source file bytes for injection."""
    if not SANDBOX_ENV_SOURCE_PATH:
        return None

    path = Path(SANDBOX_ENV_SOURCE_PATH)
    if path.exists() and path.is_file():
        try:
            return path.read_bytes()
        except Exception as exc:
            logger.warning("Failed reading env source file '%s': %s", path, exc)
            return None
    return None


def create_tar_archive_from_files(files: list[PreparedFile]) -> bytes:
    """Create a tar archive from a list of prepared files."""
    import tarfile
    from io import BytesIO

    tar_stream = BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        for file in files:
            info = tarfile.TarInfo(name=file.name)
            info.size = len(file.content)
            info.mode = 0o600
            tar.addfile(tarinfo=info, fileobj=BytesIO(file.content))
    return tar_stream.getvalue()


def _write_to_exec_socket(exec_socket: Any, data: bytes) -> None:
    """Write bytes to a Docker exec hijack socket and close its stdin side."""
    writable = getattr(exec_socket, "_sock", exec_socket)
    if hasattr(writable, "sendall"):
        writable.sendall(data)
    elif hasattr(writable, "write"):
        writable.write(data)
        flush = getattr(writable, "flush", None)
        if flush:
            flush()
    else:
        os.write(writable.fileno(), data)

    with suppress(Exception):
        writable.shutdown(socket.SHUT_WR)


def _run_exec_with_stdin(
    *,
    exec_id: str,
    input_bytes: bytes,
) -> tuple[bytes, bytes, int]:
    """Run a Docker exec command, streaming bytes to stdin and collecting output."""
    exec_socket = docker_client.api.exec_start(exec_id, socket=True)
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []

    try:
        _write_to_exec_socket(exec_socket, input_bytes)
        for stream_type, chunk in frames_iter(exec_socket, tty=False):
            if stream_type == 2:
                stderr_parts.append(chunk)
            else:
                stdout_parts.append(chunk)
    finally:
        close = getattr(exec_socket, "close", None)
        if close:
            with suppress(Exception):
                close()

    inspect_result = docker_client.api.exec_inspect(exec_id)
    return b"".join(stdout_parts), b"".join(stderr_parts), int(inspect_result.get("ExitCode", 0) or 0)


async def provision_files_in_container(
    container: docker.models.containers.Container,
    *,
    target_dir: str,
    files: list[PreparedFile],
) -> None:
    """Copy prepared files into a writable sandbox path without Docker's archive API."""
    if not files:
        return

    tar_data = create_tar_archive_from_files(files)
    exec_info = await asyncio.to_thread(
        docker_client.api.exec_create,
        container.id,
        cmd=["python", "-c", FILE_PROVISION_SCRIPT],
        stdin=True,
        stdout=True,
        stderr=True,
        environment={"TARGET_DIR": target_dir},
        user=f"{SANDBOX_UID}:{SANDBOX_GID}",
        workdir="/home/sandbox",
    )

    try:
        stdout, stderr, exit_code = await asyncio.wait_for(
            asyncio.to_thread(
                _run_exec_with_stdin,
                exec_id=exec_info["Id"],
                input_bytes=tar_data,
            ),
            timeout=FILE_PROVISION_TIMEOUT,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"Timed out while provisioning files into sandbox path {target_dir}"
        ) from exc

    if exit_code != 0:
        details = (stderr or stdout).decode("utf-8", errors="replace").strip()
        suffix = f": {details}" if details else ""
        raise RuntimeError(
            f"File provisioning into sandbox path {target_dir} failed with code {exit_code}{suffix}"
        )


def normalize_render_output_path(path: str) -> str:
    """Validate and normalize a renderer output path."""
    stat_path = posixpath.normpath(path)
    if not stat_path.startswith("/tmp/output/"):
        raise RuntimeError("Renderer output path is outside the sandbox output directory")
    return stat_path


def _read_single_file_from_container_archive(
    container: docker.models.containers.Container,
    *,
    path: str,
    max_bytes: int,
) -> bytes:
    """Read one regular file from Docker's get_archive stream."""
    stat_path = normalize_render_output_path(path)

    stream, stat = container.get_archive(stat_path)
    reported_size = int((stat or {}).get("size") or 0)
    if reported_size > max_bytes:
        raise RuntimeError(f"Renderer output exceeds max size of {max_bytes} bytes")

    tar_bytes = b"".join(stream)
    with tarfile.open(fileobj=BytesIO(tar_bytes), mode="r:*") as archive:
        regular_members = [member for member in archive.getmembers() if member.isfile()]
        if len(regular_members) != 1:
            raise RuntimeError("Renderer output archive did not contain exactly one file")
        member = regular_members[0]
        if member.size > max_bytes:
            raise RuntimeError(f"Renderer output exceeds max size of {max_bytes} bytes")
        extracted = archive.extractfile(member)
        if extracted is None:
            raise RuntimeError("Renderer output file could not be read")
        content = extracted.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise RuntimeError(f"Renderer output exceeds max size of {max_bytes} bytes")
        return content


def _read_single_file_from_container_exec(
    container: docker.models.containers.Container,
    *,
    path: str,
    max_bytes: int,
) -> bytes:
    """Read one regular file through Docker exec stdout."""
    stat_path = normalize_render_output_path(path)
    exec_info = docker_client.api.exec_create(
        container.id,
        cmd=["python", "-c", FILE_READ_SCRIPT],
        stdin=True,
        stdout=True,
        stderr=True,
        environment={
            "READ_PATH": stat_path,
            "MAX_BYTES": str(max_bytes),
        },
        user=f"{SANDBOX_UID}:{SANDBOX_GID}",
        workdir="/home/sandbox",
    )
    stdout, stderr, exit_code = _run_exec_with_stdin(
        exec_id=exec_info["Id"],
        input_bytes=b"",
    )
    if exit_code != 0:
        details = (stderr or stdout).decode("utf-8", errors="replace").strip()
        suffix = f": {details}" if details else ""
        raise RuntimeError(f"Renderer output file could not be read{suffix}")
    if len(stdout) > max_bytes:
        raise RuntimeError(f"Renderer output exceeds max size of {max_bytes} bytes")
    return stdout


async def read_render_output_file(
    container: docker.models.containers.Container,
    *,
    path: str,
) -> bytes:
    """Read a rendered archive file out of a sandbox container."""
    return await asyncio.to_thread(
        _read_single_file_from_container_exec,
        container,
        path=path,
        max_bytes=RENDER_MAX_OUTPUT_BYTES,
    )


def prepare_files(files: list[FileInput]) -> list[PreparedFile]:
    """Validate and decode base64-encoded file inputs, returning prepared files."""
    prepared: list[PreparedFile] = []
    total_size = 0
    seen_names: set[str] = set()

    for file in files:
        if file.name in seen_names:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate input file name '{file.name}' is not allowed",
            )
        seen_names.add(file.name)

        try:
            content_bytes = base64.b64decode(file.content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"File '{file.name}' content is not valid base64",
            ) from exc

        file_size = len(content_bytes)
        if file_size > MAX_INPUT_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File '{file.name}' too large ({file_size} bytes, max {MAX_INPUT_FILE_SIZE})",
            )

        total_size += file_size
        if total_size > MAX_INPUT_TOTAL_SIZE:
            raise HTTPException(
                status_code=400,
                detail="Total size of uploaded files exceeds limit",
            )

        prepared.append(PreparedFile(name=file.name, content=content_bytes))

    return prepared


async def ensure_sandbox_env_file(
    container: docker.models.containers.Container,
    *,
    inject_sandbox_env: bool,
) -> None:
    """Ensure the sandbox environment file is injected into the container if requested."""
    if not inject_sandbox_env:
        return

    env_bytes = _read_env_source_bytes()
    if env_bytes is None:
        raise RuntimeError(
            "Sandbox env injection was requested, but no readable env source file is configured."
        )

    try:
        target = Path(SANDBOX_ENV_TARGET_PATH)
        await provision_files_in_container(
            container,
            target_dir=str(target.parent),
            files=[PreparedFile(name=target.name, content=env_bytes)],
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to provision sandbox .env file: {exc}") from exc


def build_tmpfs_config(
    *,
    tmp_root_size: Optional[str] = None,
    home_tmpfs_size: Optional[str] = None,
) -> dict[str, str]:
    """Build tmpfs mount configuration for sandbox container."""
    owner = f"uid={SANDBOX_UID},gid={SANDBOX_GID}"
    return {
        "/home/sandbox": f"size={home_tmpfs_size or SANDBOX_HOME_TMPFS_SIZE},mode=0700,{owner}",
        "/tmp": f"size={tmp_root_size or SANDBOX_TMP_ROOT_SIZE},mode=1777,{owner}",  # nosec
    }


async def create_container_session(
    *,
    enable_network: bool,
    auth: AuthContext,
    inject_sandbox_env: bool,
    purpose: str = "execution",
    name_prefix: str = "sandbox",
    mem_limit: Optional[str] = None,
    cpu_period: Optional[int] = None,
    cpu_quota: Optional[int] = None,
    pids_limit: Optional[int] = None,
    tmp_root_size: Optional[str] = None,
    shm_size: Optional[str] = None,
    home_tmpfs_size: Optional[str] = None,
) -> str:
    """Create a new sandbox container session and return its ID."""
    if not local_docker_daemon_id:
        raise RuntimeError("Docker daemon identity is unavailable; refusing to create a sandbox session.")

    execution_id = str(uuid.uuid4())[:12]
    network_mode = SANDBOX_NETWORK_MODE if enable_network else "none"
    network_enabled = network_mode != "none"
    now = time.time()
    expires_at = now + MAX_SESSION_LIFETIME_SECONDS

    security_opts = ["no-new-privileges:true"]
    if not USE_DOCKER_DEFAULT_SECCOMP:
        security_opts.append(f"seccomp={SECCOMP_PROFILE_DAEMON_PATH}")

    container_config = {
        "image": SANDBOX_IMAGE,
        "mem_limit": mem_limit or SANDBOX_MEM_LIMIT,
        "memswap_limit": mem_limit or SANDBOX_MEM_LIMIT,
        "cpu_period": cpu_period or SANDBOX_CPU_PERIOD,
        "cpu_quota": cpu_quota or SANDBOX_CPU_QUOTA,
        "pids_limit": pids_limit or SANDBOX_PIDS_LIMIT,
        "shm_size": shm_size or SANDBOX_SHM_SIZE,
        "network_mode": network_mode,
        "tmpfs": build_tmpfs_config(
            tmp_root_size=tmp_root_size,
            home_tmpfs_size=home_tmpfs_size,
        ),
        "environment": {
            "HOME": "/home/sandbox",
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": "/tmp/mpl_cache",  # nosec
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "TMPDIR": "/tmp/misc",  # nosec
            "XDG_CACHE_HOME": "/tmp/.cache",  # nosec
        },
        "cap_drop": ["ALL"],
        "labels": {
            "managed-by": "code-execution-gateway",
            "purpose": purpose,
            "execution-id": execution_id,
            "owner-subject": auth.subject,
            "owner-tenant": auth.tenant or "",
            "docker-daemon-id": local_docker_daemon_id or "",
            "inject-sandbox-env": "1" if inject_sandbox_env else "0",
            "expires-at": str(expires_at),
            "execution-count": "0",
        },
        "name": f"{name_prefix}-{execution_id}",
        "read_only": SANDBOX_READ_ONLY_ROOTFS,
        "security_opt": security_opts,
        "user": f"{SANDBOX_UID}:{SANDBOX_GID}",
        "working_dir": "/home/sandbox",
    }
    if SANDBOX_RUNTIME:
        container_config["runtime"] = SANDBOX_RUNTIME

    container = await asyncio.to_thread(
        docker_client.containers.run,
        detach=True,
        **container_config,
    )

    try:
        await ensure_sandbox_env_file(
            container,
            inject_sandbox_env=inject_sandbox_env,
        )
    except Exception:
        await remove_container(
            container.id,
            execution_id=execution_id,
            reason="sandbox-env-provisioning-failed",
            container=container,
        )
        raise

    session = SessionInfo(
        created_at=now,
        last_activity=now,
        network_enabled=network_enabled,
        owner_subject=auth.subject,
        owner_tenant=auth.tenant,
        docker_daemon_id=local_docker_daemon_id,
        inject_sandbox_env=inject_sandbox_env,
        expires_at=expires_at,
        execution_count=0,
    )
    try:
        await state_backend.save_session(
            container.id,
            session,
            session_timeout_seconds=SESSION_TIMEOUT_SECONDS,
        )
    except Exception:
        await remove_container(
            container.id,
            execution_id=execution_id,
            reason="state-save-failed",
            container=container,
        )
        raise
    return container.id


async def run_exec_with_timeout(
    *,
    container: docker.models.containers.Container,
    container_id: str,
    exec_id: str,
    timeout: int,
    execution_id: str,
) -> tuple[str, int, bool]:
    """Run a Docker exec with timeout, returning (output, exit_code, timed_out)."""
    exec_task = asyncio.create_task(asyncio.to_thread(docker_client.api.exec_start, exec_id))

    try:
        raw_output = await asyncio.wait_for(exec_task, timeout=timeout)
        inspect_result = await asyncio.to_thread(docker_client.api.exec_inspect, exec_id)
        decoded = raw_output.decode("utf-8", errors="replace") if isinstance(raw_output, bytes) else str(raw_output)
        return decoded, int(inspect_result.get("ExitCode", 0) or 0), False
    except asyncio.TimeoutError:
        logger.warning("[%s] Exec timed out after %ss; tearing down container %s", execution_id, timeout, container_id)
        exec_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(exec_task, timeout=5)
        await remove_container(
            container_id,
            execution_id=execution_id,
            reason="execution-timeout",
            container=container,
        )
        return "", -1, True


async def run_code_in_sandbox(
    *,
    container_id: str,
    language: str,
    code: str,
    timeout: int,
    execution_id: str,
    pip_packages: Optional[list[str]] = None,
    files: Optional[list[FileInput]] = None,
) -> ExecuteResponse:
    """Execute code in a sandbox container and return the execution response."""
    session = await state_backend.get_session(container_id)
    if not session:
        raise HTTPException(
            status_code=404,
            detail="Container session not found, or it was shut down due to inactivity.",
        )

    try:
        container = await asyncio.to_thread(docker_client.containers.get, container_id)
    except docker.errors.NotFound as exc:
        await state_backend.delete_session(container_id)
        raise HTTPException(status_code=404, detail="Container session not found.") from exc

    if session_hard_expired(session):
        await remove_container(
            container_id,
            execution_id=execution_id,
            reason="max-session-lifetime",
            container=container,
        )
        raise HTTPException(
            status_code=404,
            detail="Container session expired after reaching its maximum lifetime.",
        )

    if session.execution_count >= MAX_EXECUTIONS_PER_SESSION:
        await remove_container(
            container_id,
            execution_id=execution_id,
            reason="max-executions",
            container=container,
        )
        raise HTTPException(
            status_code=429,
            detail="Container session reached its maximum number of executions.",
        )

    session.execution_count += 1
    session.last_activity = time.time()
    await state_backend.save_session(
        container_id,
        session,
        session_timeout_seconds=SESSION_TIMEOUT_SECONDS,
    )

    await touch_session(container_id)
    try:
        await ensure_sandbox_env_file(
            container,
            inject_sandbox_env=session.inject_sandbox_env,
        )
    except Exception as exc:
        logger.error("[%s] Failed to provision sandbox env file: %s", execution_id, exc)
        return build_execution_error_response(
            execution_id=execution_id,
            error=str(exc),
            error_type="EnvironmentProvisionError",
        )

    code_b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
    prepared_files = prepare_files(files or [])

    if prepared_files:
        try:
            await provision_files_in_container(
                container,
                target_dir="/home/sandbox",
                files=prepared_files,
            )
        except Exception as exc:
            logger.error("[%s] Failed to upload files: %s", execution_id, exc)
            return build_execution_error_response(
                execution_id=execution_id,
                error=f"Failed to copy input files to container: {exc}",
                error_type="FileUploadError",
            )

    environment = {
        "CODE_B64": code_b64,
        "ENABLE_NETWORK": "1" if session.network_enabled else "0",
        "EXEC_TIMEOUT": str(timeout),
        "PIP_PACKAGES": ",".join(pip_packages or []),
    }

    try:
        exec_cmd = ["python", "/usr/local/bin/executor.py", "--lang", language, "--exec-id", execution_id]
        exec_info = await asyncio.to_thread(
            docker_client.api.exec_create,
            container.id,
            cmd=exec_cmd,
            environment=environment,
            user=f"{SANDBOX_UID}:{SANDBOX_GID}",
            workdir="/home/sandbox",
        )

        raw_output, _, timed_out = await run_exec_with_timeout(
            container=container,
            container_id=container_id,
            exec_id=exec_info["Id"],
            timeout=timeout,
            execution_id=execution_id,
        )

        if timed_out:
            return build_execution_error_response(
                execution_id=execution_id,
                error=f"Execution timed out after {timeout} seconds. Container removed.",
                error_type="TimeoutError",
                execution_time=float(timeout),
                timed_out=True,
            )

        try:
            result_data = parse_executor_result(raw_output)

            await touch_session(container_id)

            return ExecuteResponse(
                execution_id=execution_id,
                stdout=result_data.get("stdout", ""),
                stderr=result_data.get("stderr", ""),
                error=result_data.get("error"),
                error_type=result_data.get("error_type"),
                files=[FileOutput(**item) for item in result_data.get("files", [])],
                execution_time=float(result_data.get("execution_time", 0)),
                install_time=result_data.get("install_time"),
                timed_out=False,
            )
        except (ExecutorOutputError, TypeError, ValueError) as exc:
            logger.error("[%s] Failed to parse executor output: %s", execution_id, exc)
            logger.debug("[%s] Raw executor output: %s", execution_id, raw_output[:2000] if raw_output else "")
            return build_execution_error_response(
                execution_id=execution_id,
                error="Failed to parse executor output",
                error_type="GatewayError",
            )

    except docker.errors.APIError as exc:
        logger.error("[%s] Docker API error: %s", execution_id, exc)
        return build_execution_error_response(
            execution_id=execution_id,
            error="Execution failed due to an infrastructure error",
            error_type="GatewayError",
        )


def build_render_exec_environment(timeout: int) -> dict[str, str]:
    """Build environment variables for the sandbox render CLI."""
    return {
        "APP_ENV": APP_ENV,
        "BETA": "1" if SLIDE_RENDERING_VERSION == "v2" else "0",
        "SLIDE_RENDERING_VERSION": SLIDE_RENDERING_VERSION,
        "RENDERING_VERSION": SLIDE_RENDERING_VERSION,
        "RENDER_TIMEOUT_SECONDS": str(timeout),
        "RENDER_QUEUE_TIMEOUT_MS": str(RENDER_QUEUE_TIMEOUT_MS),
        "PAGE_LOAD_TIMEOUT_MS": str(RENDER_PAGE_LOAD_TIMEOUT_MS),
        "MAX_CONCURRENT_RENDERS": "1",
        "RENDER_MAX_REQUEST_BODY_BYTES": str(RENDER_MAX_REQUEST_BODY_SIZE),
        "RENDER_MAX_HTML_CHARS": str(RENDER_MAX_HTML_CHARS),
        "RENDER_MAX_INPUT_FILES": str(RENDER_MAX_INPUT_FILES),
        "RENDER_MAX_SLIDES": str(RENDER_MAX_SLIDES),
        "RENDER_MAX_ASSET_BYTES": str(RENDER_MAX_ASSET_BYTES),
        "RENDER_MAX_TOTAL_ASSET_BYTES": str(RENDER_MAX_TOTAL_ASSET_BYTES),
        "RENDER_MAX_OUTPUT_BYTES": str(RENDER_MAX_OUTPUT_BYTES),
        "HOME": "/home/sandbox",
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": "/tmp/mpl_cache",  # nosec
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "TMPDIR": "/tmp",  # nosec
        "XDG_CACHE_HOME": "/tmp/.cache",  # nosec
        "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright",
    }


async def create_render_container(auth: AuthContext) -> str:
    """Create a short-lived sandbox container for slide rendering."""
    async with state_backend.container_creation_guard(
        timeout_seconds=CONTAINER_CREATE_GUARD_TIMEOUT
    ):
        await enforce_container_creation_limits(auth)
        return await create_container_session(
            enable_network=False,
            auth=auth,
            inject_sandbox_env=False,
            purpose="slide-render",
            name_prefix="render",
            mem_limit=RENDER_SANDBOX_MEM_LIMIT,
            cpu_period=RENDER_SANDBOX_CPU_PERIOD,
            cpu_quota=RENDER_SANDBOX_CPU_QUOTA,
            pids_limit=RENDER_SANDBOX_PIDS_LIMIT,
            tmp_root_size=RENDER_SANDBOX_TMP_ROOT_SIZE,
            shm_size=RENDER_SANDBOX_SHM_SIZE,
            home_tmpfs_size=RENDER_SANDBOX_HOME_TMPFS_SIZE,
        )


async def render_presentation_in_sandbox(
    *,
    payload: RenderRequest,
    auth: AuthContext,
    render_id: str,
    timeout: int,
) -> RenderSandboxResult:
    """Render a presentation inside the shared Playwright sandbox image."""
    container_id = await create_render_container(auth)
    container: docker.models.containers.Container | None = None
    try:
        container = await asyncio.to_thread(docker_client.containers.get, container_id)
        request_dir = f"/tmp/render/{render_id}"  # nosec
        output_dir = f"/tmp/output/{render_id}"  # nosec
        request_file_name = "request.json"
        request_json = json.dumps(payload.model_dump(mode="json"), separators=(",", ":")).encode("utf-8")

        await provision_files_in_container(
            container,
            target_dir=request_dir,
            files=[PreparedFile(name=request_file_name, content=request_json)],
        )

        exec_info = await asyncio.to_thread(
            docker_client.api.exec_create,
            container.id,
            cmd=[
                "python",
                "/usr/local/bin/render_presentation.py",
                "--request",
                f"{request_dir}/{request_file_name}",
                "--output-dir",
                output_dir,
            ],
            environment=build_render_exec_environment(timeout),
            user=f"{SANDBOX_UID}:{SANDBOX_GID}",
            workdir="/home/sandbox",
        )

        raw_output, exit_code, timed_out = await run_exec_with_timeout(
            container=container,
            container_id=container_id,
            exec_id=exec_info["Id"],
            timeout=timeout,
            execution_id=render_id,
        )

        if timed_out:
            raise HTTPException(
                status_code=504,
                detail=f"render timeout exceeded after {timeout} seconds",
            )

        try:
            result_data = parse_render_result(raw_output)
        except (ExecutorOutputError, TypeError, ValueError) as exc:
            logger.error("[%s] Failed to parse renderer output: %s", render_id, exc)
            logger.debug("[%s] Raw renderer output: %s", render_id, raw_output[:2000] if raw_output else "")
            raise HTTPException(status_code=500, detail="rendering failed") from exc

        if result_data.get("error"):
            status_code = 400 if exit_code == 2 else 500
            raise HTTPException(status_code=status_code, detail=str(result_data["error"]))

        output_path = str(result_data.get("output_path") or "")
        if not output_path:
            raise HTTPException(status_code=500, detail="renderer did not report an output file")

        content = await read_render_output_file(container, path=output_path)
        file_name = str(result_data.get("file_name") or posixpath.basename(output_path))
        media_type = str(result_data.get("media_type") or "application/zip")
        rendering_version = str(result_data.get("rendering_version") or SLIDE_RENDERING_VERSION)
        slide_count = int(result_data.get("slide_count") or 0)
        execution_time = float(result_data.get("execution_time") or 0)

        return RenderSandboxResult(
            render_id=render_id,
            file_name=file_name,
            rendering_version=rendering_version,
            content=content,
            media_type=media_type,
            slide_count=slide_count,
            execution_time=execution_time,
        )
    finally:
        await remove_container(
            container_id,
            execution_id=render_id,
            reason="slide-render-complete",
            container=container,
        )


@app.post("/containers", response_model=ContainerResponse)
async def create_container(
    request: Optional[CreateContainerRequest] = None,
    auth: AuthContext = Depends(verify_auth),
):
    """Create a new sandbox container session."""
    enable_network = request.enable_network if request else False
    inject_sandbox_env = request.inject_sandbox_env if request else False
    if inject_sandbox_env and not ALLOW_SANDBOX_ENV_INJECTION:
        raise HTTPException(status_code=400, detail="Sandbox env injection is disabled.")
    if inject_sandbox_env and _read_env_source_bytes() is None:
        raise HTTPException(
            status_code=400,
            detail="Sandbox env injection was requested, but no readable env source file is configured.",
        )
    try:
        owner_key = principal_scope(auth)
        await enforce_rate_limit(
            f"container-create:{owner_key}",
            limit=CONTAINER_RATE_LIMIT_REQUESTS,
            window_seconds=CONTAINER_RATE_LIMIT_WINDOW_SECONDS,
            message="Container creation rate limit exceeded for this principal.",
        )
        try:
            async with state_backend.container_creation_guard(
                timeout_seconds=CONTAINER_CREATE_GUARD_TIMEOUT
            ):
                await enforce_container_creation_limits(auth)
                container_id = await create_container_session(
                    enable_network=enable_network,
                    auth=auth,
                    inject_sandbox_env=inject_sandbox_env,
                )
                session = await state_backend.get_session(container_id)
                if session is None:
                    raise RuntimeError("Container session was created but not persisted.")
        except TimeoutError as exc:
            raise HTTPException(
                status_code=503,
                detail="Container creation is temporarily saturated. Please retry shortly.",
            ) from exc
        return ContainerResponse(
            container_id=container_id,
            status="active",
            uptime_seconds=0.0,
            last_activity=session.last_activity,
            expires_at=session.expires_at,
            execution_count=session.execution_count,
            docker_daemon_id=session.docker_daemon_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to create container: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error during container creation") from exc


@app.get("/containers/{container_id}", response_model=ContainerResponse)
async def get_container(container_id: str, auth: AuthContext = Depends(verify_auth)):
    """Get information about a container session."""
    session = await ensure_session_access(container_id, auth)

    try:
        await asyncio.to_thread(docker_client.containers.get, container_id)
        now = time.time()
        return ContainerResponse(
            container_id=container_id,
            status="active",
            uptime_seconds=max(0.0, now - session.created_at),
            last_activity=session.last_activity,
            expires_at=session.expires_at,
            execution_count=session.execution_count,
            docker_daemon_id=session.docker_daemon_id,
        )
    except docker.errors.NotFound as exc:
        await state_backend.delete_session(container_id)
        raise HTTPException(status_code=404, detail="Container session not found") from exc


@app.delete("/containers/{container_id}")
async def delete_container(container_id: str, auth: AuthContext = Depends(verify_auth)):
    """Delete a container session."""
    await ensure_session_access(container_id, auth)
    await remove_container(container_id, reason="client-delete")
    return {"status": "success", "message": f"Container {container_id} removed."}


@app.post("/execute", response_model=ExecuteResponse)
async def execute_code(request: ExecuteRequest, auth: AuthContext = Depends(verify_auth)):
    """Execute code in a container session."""
    session = await ensure_session_access(request.container_id, auth)
    timeout = request.timeout or DEFAULT_TIMEOUT
    execution_id = str(uuid.uuid4())[:12]

    if request.enable_network is False and session.network_enabled:
        logger.info(
            "[%s] Ignoring per-request enable_network=false because session network mode is fixed at creation time",
            execution_id,
        )

    logger.info(
        "[%s] Execution request subject=%s tenant=%s code_length=%s timeout=%ss network=%s",
        execution_id,
        auth.subject,
        auth.tenant or "-",
        len(request.code),
        timeout,
        "on" if session.network_enabled else "off",
    )

    metrics["total_executions"] += 1
    metrics["active_executions"] += 1
    ACTIVE_EXECUTIONS_GAUGE.inc()

    start = time.monotonic()
    acquired_execution_slot = False
    try:
        try:
            await asyncio.wait_for(execution_semaphore.acquire(), timeout=30)
            acquired_execution_slot = True
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=429,
                detail="Too many concurrent executions. Please try again later.",
            ) from exc

        try:
            await enforce_rate_limit(
                f"execute:{principal_scope(auth)}",
                limit=RATE_LIMIT_REQUESTS,
                window_seconds=RATE_LIMIT_WINDOW_SECONDS,
                message="Rate limit exceeded for this principal. Please slow down.",
            )
            try:
                async with state_backend.execution_lock(
                    request.container_id, timeout_seconds=timeout + 30
                ):
                    result = await run_code_in_sandbox(
                        container_id=request.container_id,
                        language=request.language,
                        code=request.code,
                        timeout=timeout,
                        execution_id=execution_id,
                        pip_packages=request.pip_packages,
                        files=request.files,
                    )
            except TimeoutError as exc:
                raise HTTPException(
                    status_code=429,
                    detail="Another execution is in progress for this container. Please wait.",
                ) from exc

            if result.timed_out:
                metrics["timed_out_executions"] += 1
                EXECUTION_COUNTER.labels("timed_out").inc()
            elif result.error:
                metrics["failed_executions"] += 1
                EXECUTION_COUNTER.labels("failed").inc()
            else:
                metrics["successful_executions"] += 1
                EXECUTION_COUNTER.labels("successful").inc()

            EXECUTION_LATENCY.observe(time.monotonic() - start)
            logger.info(
                "[%s] Execution complete time=%ss files=%s error=%s timed_out=%s",
                execution_id,
                result.execution_time,
                len(result.files),
                "yes" if result.error else "no",
                result.timed_out,
            )
            return result
        finally:
            if acquired_execution_slot:
                execution_semaphore.release()
    finally:
        metrics["active_executions"] -= 1
        ACTIVE_EXECUTIONS_GAUGE.dec()


async def build_health_payload() -> tuple[bool, dict]:
    """Build health check payload with Docker and state backend status."""
    try:
        await asyncio.to_thread(docker_client.ping)
        docker_ok = True
    except Exception:
        docker_ok = False

    try:
        await asyncio.to_thread(docker_client.images.get, SANDBOX_IMAGE)
        image_ok = True
    except Exception:
        image_ok = False

    state_ok = await state_backend.health_check()
    healthy = docker_ok and image_ok and state_ok
    payload = {
        "status": "healthy" if healthy else "degraded",
        "app_env": APP_ENV,
        "auth_mode": auth_mode_summary(),
        "docker_connected": docker_ok,
        "docker_daemon_id": local_docker_daemon_id,
        "docker_daemon_name": local_docker_daemon_name,
        "sandbox_image_available": image_ok,
        "sandbox_image": SANDBOX_IMAGE,
        "sandbox_runtime": SANDBOX_RUNTIME or "default",
        "public_beta_mode": PUBLIC_BETA_MODE,
        "strong_sandbox_isolation_required": REQUIRE_STRONG_SANDBOX_ISOLATION,
        "state_backend": type(state_backend).__name__,
        "state_backend_healthy": state_ok,
        "cors_enabled": ENABLE_CORS,
        "cors_origins_configured": CORS_ALLOW_ORIGINS,
        "max_concurrent_executions": MAX_CONCURRENT,
        "max_concurrent_renders": RENDER_MAX_CONCURRENT,
        "active_rendering_version": SLIDE_RENDERING_VERSION,
        "render_timeout_seconds": RENDER_TIMEOUT_SECONDS,
        "default_timeout": DEFAULT_TIMEOUT,
        "session_timeout_seconds": SESSION_TIMEOUT_SECONDS,
        "max_session_lifetime_seconds": MAX_SESSION_LIFETIME_SECONDS,
        "max_executions_per_session": MAX_EXECUTIONS_PER_SESSION,
        "metrics": metrics,
    }
    return healthy, payload


@app.get("/health")
@app.get("/healthz")
@app.get("/readyz")
async def health_check():
    """Simple health check endpoint returning service status."""
    healthy, payload = await build_health_payload()
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": payload["status"]},
    )


@app.get("/health/details")
@app.get("/healthz/details")
async def health_check_details(_auth: AuthContext = Depends(verify_auth)):
    """Detailed health check endpoint returning full service status."""
    healthy, payload = await build_health_payload()
    return JSONResponse(
        status_code=200 if healthy else 503,
        content=payload,
    )


@app.get("/metrics")
async def get_metrics(_auth: Optional[AuthContext] = Depends(verify_metrics_auth)):
    """Prometheus metrics endpoint."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/metrics/json")
async def get_metrics_json(auth: AuthContext = Depends(verify_auth)):
    """JSON metrics endpoint for debugging."""
    return {
        "_note": "replica-local counters; use /metrics (Prometheus) for cluster-wide aggregation",
        **metrics,
    }


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler to log errors and return 500 response."""
    logger.error(
        "[%s] Unhandled exception: %s",
        getattr(request.state, "request_id", "-"),
        exc,
        exc_info=True,
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
