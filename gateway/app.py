"""
Code Execution Gateway — FastAPI service that manages sandbox containers.

This service exposes authenticated APIs to create isolated sandbox sessions,
execute code inside them, and retrieve execution artifacts.
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
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
from pydantic import BaseModel, Field, field_validator

from state import InMemoryStateBackend, RedisStateBackend, SessionInfo, StateBackend


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


APP_ENV = os.getenv("APP_ENV", "production").strip().lower()
IS_PRODUCTION = APP_ENV in {"prod", "production"}

SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "code-sandbox:latest")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_EXECUTIONS", "10"))
DEFAULT_TIMEOUT = int(os.getenv("DEFAULT_TIMEOUT", "30"))
MAX_TIMEOUT = int(os.getenv("MAX_TIMEOUT", "120"))
SANDBOX_MEM_LIMIT = os.getenv("SANDBOX_MEM_LIMIT", "512m")
SANDBOX_CPU_PERIOD = int(os.getenv("SANDBOX_CPU_PERIOD", "100000"))
SANDBOX_CPU_QUOTA = int(os.getenv("SANDBOX_CPU_QUOTA", "100000"))
SANDBOX_PIDS_LIMIT = int(os.getenv("SANDBOX_PIDS_LIMIT", "256"))
SANDBOX_TMPFS_SIZE = os.getenv("SANDBOX_TMPFS_SIZE", "100m")
SANDBOX_MPL_CACHE_TMPFS_SIZE = os.getenv("SANDBOX_MPL_CACHE_TMPFS_SIZE", "32m")
SANDBOX_MISC_TMPFS_SIZE = os.getenv("SANDBOX_MISC_TMPFS_SIZE", "128m")
SANDBOX_SHM_SIZE = os.getenv("SANDBOX_SHM_SIZE", "128m")
SANDBOX_HOME_TMPFS_SIZE = os.getenv("SANDBOX_HOME_TMPFS_SIZE", "256m")
SANDBOX_READ_ONLY_ROOTFS = str_to_bool(os.getenv("SANDBOX_READ_ONLY_ROOTFS", "false"))
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

ENABLE_CORS = str_to_bool(os.getenv("ENABLE_CORS", "true"))
CORS_ALLOW_ORIGINS = split_csv(os.getenv("CORS_ALLOW_ORIGINS"))
CORS_ALLOW_METHODS = split_csv(os.getenv("CORS_ALLOW_METHODS")) or ["GET", "POST", "DELETE", "OPTIONS"]
CORS_ALLOW_HEADERS = split_csv(os.getenv("CORS_ALLOW_HEADERS")) or ["Authorization", "Content-Type", "X-Request-ID"]
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

MAX_INPUT_FILES = int(os.getenv("MAX_INPUT_FILES", "10"))
MAX_INPUT_FILE_SIZE = int(os.getenv("MAX_INPUT_FILE_SIZE", str(5 * 1024 * 1024)))
MAX_INPUT_TOTAL_SIZE = int(os.getenv("MAX_INPUT_TOTAL_SIZE", str(20 * 1024 * 1024)))
MAX_FILE_NAME_LENGTH = int(os.getenv("MAX_FILE_NAME_LENGTH", "128"))
MAX_PIP_PACKAGES = int(os.getenv("MAX_PIP_PACKAGES", "5"))
MAX_PIP_PACKAGE_NAME_LENGTH = int(os.getenv("MAX_PIP_PACKAGE_NAME_LENGTH", "64"))
ALLOW_PIP_INSTALLS = str_to_bool(os.getenv("ALLOW_PIP_INSTALLS", "false"))
PIP_PACKAGE_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*([\[,\]A-Za-z0-9._-]*)?"
    r"(([!=<>~]=?|>=?|<=?)[\w.*]+([,;]([!=<>~]=?|>=?|<=?)[\w.*]+)*)?$"
)

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


execution_semaphore: asyncio.Semaphore
docker_client: docker.DockerClient
state_backend: StateBackend
local_docker_daemon_id: Optional[str] = None
local_docker_daemon_name: Optional[str] = None
SESSION_TIMEOUT_SECONDS = 20 * 60

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

metrics = {
    "total_executions": 0,
    "successful_executions": 0,
    "failed_executions": 0,
    "timed_out_executions": 0,
    "active_executions": 0,
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


def principal_scope(auth: AuthContext) -> str:
    """Generate a scope string for rate limiting based on subject and tenant."""
    return f"{auth.subject}:{auth.tenant or '-'}"


def validate_runtime_configuration() -> None:
    """Validate runtime configuration settings and raise RuntimeError if invalid."""
    if DEFAULT_TIMEOUT > MAX_TIMEOUT:
        raise RuntimeError("DEFAULT_TIMEOUT must be less than or equal to MAX_TIMEOUT")

    if REQUIRE_AUTH and not (JWT_SECRET or STATIC_API_KEYS):
        raise RuntimeError(
            "Authentication is required, but neither JWT nor static API keys are configured."
        )

    if IS_PRODUCTION and STATIC_API_KEYS:
        too_short = [key.key_id for key in STATIC_API_KEYS if len(key.secret) < 32]
        if too_short:
            raise RuntimeError(
                "Static API keys must be at least 32 characters in production. "
                f"Invalid key ids: {', '.join(too_short)}"
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

    if IS_PRODUCTION:
        if not DOCKER_HOST:
            raise RuntimeError("DOCKER_HOST must be configured explicitly in production.")
        if DOCKER_HOST.startswith("unix://"):
            raise RuntimeError(
                "DOCKER_HOST must point at a restricted TCP proxy or remote daemon in production; "
                "raw Unix socket access is not allowed."
            )
        parsed_docker = urlparse(DOCKER_HOST)
        if parsed_docker.scheme == "tcp" and parsed_docker.port == 2375:
            raise RuntimeError(
                "Production DOCKER_HOST must use TLS (port 2376) or ssh://. "
                "Plain TCP on port 2375 is unencrypted and unsafe."
            )
        if parsed_docker.scheme not in {"tcp", "ssh"}:
            raise RuntimeError(
                "Production DOCKER_HOST must use tcp:// (with TLS) or ssh://."
            )
        if docker_host_hostname(DOCKER_HOST) in {
            "docker-proxy",
            "localhost",
            "127.0.0.1",
            "::1",
            "host.docker.internal",
        }:
            raise RuntimeError(
                "Production gateways must use a dedicated remote Docker daemon. "
                "Local Docker socket proxies and loopback targets are not allowed."
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
    )


async def touch_session(container_id: str) -> Optional[SessionInfo]:
    """Update session last activity timestamp and return session info."""
    return await state_backend.touch_session(
        container_id,
        session_timeout_seconds=SESSION_TIMEOUT_SECONDS,
    )


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
                if session_is_local(session) and now - session.last_activity > SESSION_TIMEOUT_SECONDS
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
                    if container.id not in tracked_ids and container.name.startswith("sandbox-"):
                        recovered = recover_session_info(container)
                        await state_backend.save_session(
                            container.id,
                            recovered,
                            session_timeout_seconds=SESSION_TIMEOUT_SECONDS,
                        )
                        logger.info("Recovered untracked managed container %s during cleanup", container.id)
            except Exception as exc:
                logger.error("Error cleaning up untracked containers: %s", exc)

        except Exception as exc:
            logger.error("Error in cleanup task: %s", exc)

        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager for startup and shutdown."""
    global docker_client, execution_semaphore, state_backend, local_docker_daemon_id, local_docker_daemon_name

    validate_runtime_configuration()
    docker_client = docker.from_env()
    execution_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
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
            session = recover_session_info(container)
            await state_backend.save_session(
                container.id,
                session,
                session_timeout_seconds=SESSION_TIMEOUT_SECONDS,
            )
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
        "Gateway started env=%s auth=%s max_concurrent=%s default_timeout=%ss network=%s read_only_rootfs=%s docker_default_seccomp=%s state_backend=%s docker_daemon_id=%s",
        APP_ENV,
        auth_mode_summary(),
        MAX_CONCURRENT,
        DEFAULT_TIMEOUT,
        SANDBOX_NETWORK_MODE,
        SANDBOX_READ_ONLY_ROOTFS,
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
    version="1.1.0",
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


@app.middleware("http")
async def request_metrics_middleware(request: Request, call_next):
    """Middleware to track request metrics and add request ID to responses."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    start = time.monotonic()

    try:
        response = await call_next(request)
    except Exception:
        elapsed = time.monotonic() - start
        REQUEST_COUNTER.labels(
            request.method,
            request.url.path,
            "500",
        ).inc()
        REQUEST_LATENCY.labels(request.method, request.url.path).observe(elapsed)
        logger.exception("[%s] %s %s failed after %.3fs", request_id, request.method, request.url.path, elapsed)
        raise

    elapsed = time.monotonic() - start
    status_code = str(response.status_code)
    response.headers["X-Request-ID"] = request_id
    REQUEST_COUNTER.labels(request.method, request.url.path, status_code).inc()
    REQUEST_LATENCY.labels(request.method, request.url.path).observe(elapsed)
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
        default=True,
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


class CreateContainerRequest(BaseModel):
    enable_network: bool = True
    inject_sandbox_env: bool = False


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
        tar_data = create_tar_archive_from_files(
            [PreparedFile(name=target.name, content=env_bytes)]
        )
        await asyncio.to_thread(container.put_archive, str(target.parent), tar_data)
    except Exception as exc:
        raise RuntimeError(f"Failed to provision sandbox .env file: {exc}") from exc


def build_tmpfs_config() -> dict[str, str]:
    """Build tmpfs mount configuration for sandbox container."""
    owner = f"uid={SANDBOX_UID},gid={SANDBOX_GID}"
    return {
        "/home/sandbox": f"size={SANDBOX_HOME_TMPFS_SIZE},mode=0700,{owner}",
        "/tmp/output": f"size={SANDBOX_TMPFS_SIZE},mode=1777,{owner}",
        "/tmp/mpl_cache": f"size={SANDBOX_MPL_CACHE_TMPFS_SIZE},mode=1777,{owner}",
        "/tmp/misc": f"size={SANDBOX_MISC_TMPFS_SIZE},mode=1777,{owner}",
    }


async def create_container_session(
    *,
    enable_network: bool,
    auth: AuthContext,
    inject_sandbox_env: bool,
) -> str:
    """Create a new sandbox container session and return its ID."""
    if not local_docker_daemon_id:
        raise RuntimeError("Docker daemon identity is unavailable; refusing to create a sandbox session.")

    execution_id = str(uuid.uuid4())[:12]
    network_mode = SANDBOX_NETWORK_MODE if enable_network else "none"

    security_opts = ["no-new-privileges:true"]
    if not USE_DOCKER_DEFAULT_SECCOMP:
        security_opts.append(f"seccomp={SECCOMP_PROFILE_DAEMON_PATH}")

    container_config = {
        "image": SANDBOX_IMAGE,
        "mem_limit": SANDBOX_MEM_LIMIT,
        "memswap_limit": SANDBOX_MEM_LIMIT,
        "cpu_period": SANDBOX_CPU_PERIOD,
        "cpu_quota": SANDBOX_CPU_QUOTA,
        "pids_limit": SANDBOX_PIDS_LIMIT,
        "shm_size": SANDBOX_SHM_SIZE,
        "network_mode": network_mode,
        "tmpfs": build_tmpfs_config(),
        "environment": {
            "HOME": "/home/sandbox",
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": "/tmp/mpl_cache",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "TMPDIR": "/tmp/misc",
        },
        "cap_drop": ["ALL"],
        "labels": {
            "managed-by": "code-execution-gateway",
            "execution-id": execution_id,
            "owner-subject": auth.subject,
            "owner-tenant": auth.tenant or "",
            "docker-daemon-id": local_docker_daemon_id or "",
            "inject-sandbox-env": "1" if inject_sandbox_env else "0",
        },
        "name": f"sandbox-{execution_id}",
        # Docker archive uploads for input files and `.env` injection require a writable rootfs.
        "read_only": SANDBOX_READ_ONLY_ROOTFS,
        "security_opt": security_opts,
        "user": f"{SANDBOX_UID}:{SANDBOX_GID}",
        "working_dir": "/home/sandbox",
    }

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

    now = time.time()
    session = SessionInfo(
        created_at=now,
        last_activity=now,
        network_enabled=enable_network,
        owner_subject=auth.subject,
        owner_tenant=auth.tenant,
        docker_daemon_id=local_docker_daemon_id,
        inject_sandbox_env=inject_sandbox_env,
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
            tar_data = create_tar_archive_from_files(prepared_files)
            await asyncio.to_thread(container.put_archive, "/home/sandbox", tar_data)
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


@app.post("/containers", response_model=ContainerResponse)
async def create_container(
    request: Optional[CreateContainerRequest] = None,
    auth: AuthContext = Depends(verify_auth),
):
    """Create a new sandbox container session."""
    enable_network = request.enable_network if request else True
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
        "state_backend": type(state_backend).__name__,
        "state_backend_healthy": state_ok,
        "cors_enabled": ENABLE_CORS,
        "cors_origins_configured": CORS_ALLOW_ORIGINS,
        "max_concurrent_executions": MAX_CONCURRENT,
        "default_timeout": DEFAULT_TIMEOUT,
        "metrics": metrics,
    }
    return healthy, payload


@app.get("/health")
@app.get("/healthz")
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
