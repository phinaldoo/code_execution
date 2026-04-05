"""
Code Execution Gateway — FastAPI service that manages sandbox containers.

This is the main entry point for the code execution service. It:
1. Receives Python code via POST /execute
2. Spins up an ephemeral, hardened sandbox container
3. Mounts the code, waits for execution, collects results
4. Returns stdout, stderr, errors, and base64-encoded output files

Security: Containers are created with strict resource limits, dropped Linux
capabilities, read-only filesystem, custom seccomp profile, and non-root user.
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, List, NamedTuple, Optional

import docker
import docker.errors
from fastapi import FastAPI, HTTPException, Request, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, validator

# --- Helper Utilities ---
def str_to_bool(value: Optional[str], default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# --- Configuration via Environment Variables ---
SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "code-sandbox:latest")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_EXECUTIONS", "10"))
DEFAULT_TIMEOUT = int(os.getenv("DEFAULT_TIMEOUT", "30"))
MAX_TIMEOUT = int(os.getenv("MAX_TIMEOUT", "120"))
SANDBOX_MEM_LIMIT = os.getenv("SANDBOX_MEM_LIMIT", "512m")
SANDBOX_CPU_PERIOD = int(os.getenv("SANDBOX_CPU_PERIOD", "100000"))
SANDBOX_CPU_QUOTA = int(os.getenv("SANDBOX_CPU_QUOTA", "100000"))  # 1 core
SANDBOX_PIDS_LIMIT = int(os.getenv("SANDBOX_PIDS_LIMIT", "256"))
SANDBOX_TMPFS_SIZE = os.getenv("SANDBOX_TMPFS_SIZE", "100m")
SANDBOX_MPL_CACHE_TMPFS_SIZE = os.getenv("SANDBOX_MPL_CACHE_TMPFS_SIZE", "32m")
SANDBOX_MISC_TMPFS_SIZE = os.getenv("SANDBOX_MISC_TMPFS_SIZE", "128m")
SANDBOX_SHM_SIZE = os.getenv("SANDBOX_SHM_SIZE", "128m")
SANDBOX_NETWORK_MODE = os.getenv("SANDBOX_NETWORK_MODE", "bridge")  # "bridge" or "none"
SECCOMP_PROFILE_PATH = os.getenv("SECCOMP_PROFILE_PATH", "/etc/code-execution/seccomp-profile.json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
API_KEY = os.getenv("API_KEY")  # Optional Bearer token protection
MAX_INPUT_FILES = int(os.getenv("MAX_INPUT_FILES", "10"))
MAX_INPUT_FILE_SIZE = int(os.getenv("MAX_INPUT_FILE_SIZE", str(5 * 1024 * 1024)))
MAX_INPUT_TOTAL_SIZE = int(os.getenv("MAX_INPUT_TOTAL_SIZE", str(20 * 1024 * 1024)))
MAX_FILE_NAME_LENGTH = int(os.getenv("MAX_FILE_NAME_LENGTH", "128"))
MAX_PIP_PACKAGES = int(os.getenv("MAX_PIP_PACKAGES", "5"))
MAX_PIP_PACKAGE_NAME_LENGTH = int(os.getenv("MAX_PIP_PACKAGE_NAME_LENGTH", "64"))
ALLOW_PIP_INSTALLS = str_to_bool(os.getenv("ALLOW_PIP_INSTALLS", "true"))
PIP_PACKAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*([\[,\]A-Za-z0-9._-]*)?(([!=<>~]=?|>=?|<=?)[\w.*]+([,;]([!=<>~]=?|>=?|<=?)[\w.*]+)*)?$")
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS_PER_WINDOW", "30"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))

SANDBOX_ENV_TARGET_PATH = os.getenv("SANDBOX_ENV_TARGET_PATH", "/home/sandbox/.env")
_DEFAULT_ENV_SANDBOX_SOURCE = str(Path(__file__).resolve().parents[1] / ".env_sandbox")
_DEFAULT_ENV_EXAMPLE_SOURCE = str(Path(__file__).resolve().parents[1] / ".env.example")
SANDBOX_ENV_SOURCE_PATH = os.getenv("SANDBOX_ENV_SOURCE_PATH", _DEFAULT_ENV_SANDBOX_SOURCE)
SANDBOX_ENV_FALLBACK_SOURCE_PATH = os.getenv("SANDBOX_ENV_FALLBACK_SOURCE_PATH", _DEFAULT_ENV_EXAMPLE_SOURCE)

# --- Logging ---
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("gateway")

# --- Concurrency Semaphore ---
execution_semaphore: asyncio.Semaphore

# --- Docker Client ---
docker_client: docker.DockerClient

# --- Session Management ---


@dataclass
class SessionInfo:
    last_activity: float
    network_enabled: bool


class PreparedFile(NamedTuple):
    name: str
    content: bytes


# Map of container_id -> SessionInfo
active_sessions: Dict[str, SessionInfo] = {}
SESSION_TIMEOUT_SECONDS = 20 * 60  # 20 minutes

# Rate limiting state
rate_limit_state: Dict[str, Deque[float]] = {}
rate_limit_lock: asyncio.Lock


def infer_network_enabled(container: docker.models.containers.Container) -> bool:
    try:
        host_cfg = container.attrs.get("HostConfig", {})
        network_mode = host_cfg.get("NetworkMode", SANDBOX_NETWORK_MODE)
        return network_mode != "none"
    except Exception:
        return SANDBOX_NETWORK_MODE != "none"


def touch_session(container_id: str):
    session = active_sessions.get(container_id)
    if session is None:
        return
    # Backward-compat: older code accidentally stored a raw timestamp (float)
    # instead of a SessionInfo.
    if isinstance(session, (int, float)):
        try:
            container = docker_client.containers.get(container_id)
            network_enabled = infer_network_enabled(container)
        except Exception:
            network_enabled = SANDBOX_NETWORK_MODE != "none"
        active_sessions[container_id] = SessionInfo(
            last_activity=time.time(),
            network_enabled=network_enabled,
        )
        return

    session.last_activity = time.time()


async def check_rate_limit(key: str):
    if RATE_LIMIT_REQUESTS <= 0:
        return
    if not key:
        return

    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS

    async with rate_limit_lock:
        hits = rate_limit_state.setdefault(key, deque())
        while hits and hits[0] < window_start:
            hits.popleft()

        if len(hits) >= RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=429,
                detail=(
                    "Rate limit exceeded for this container. "
                    "Please slow down or request higher limits."
                ),
            )

        hits.append(now)


async def cleanup_idle_containers():
    """Background task to remove idle containers."""
    while True:
        try:
            now = time.time()
            idle_ids = [
                cid for cid, session in active_sessions.items()
                if now - session.last_activity > SESSION_TIMEOUT_SECONDS
            ]

            for cid in idle_ids:
                try:
                    container = docker_client.containers.get(cid)
                    container.remove(force=True)
                    logger.info(f"[{cid}] Removed idle container")
                except docker.errors.NotFound:
                    pass
                except Exception as e:
                    logger.warning(f"[{cid}] Failed to remove idle container: {e}")
                
                if cid in active_sessions:
                    del active_sessions[cid]

            # Also catch any containers we missed that are managed by us but not in active_sessions
            try:
                managed_containers = docker_client.containers.list(
                    all=True, 
                    filters={"label": "managed-by=code-execution-gateway"}
                )
                for c in managed_containers:
                    if c.id not in active_sessions and c.name.startswith("sandbox-"):
                        c.remove(force=True)
                        logger.info(f"[{c.id}] Cleaned up untracked managed container")
            except Exception as e:
                logger.error(f"Error cleaning up untracked containers: {e}")

        except Exception as e:
            logger.error(f"Error in cleanup task: {e}")
            
        await asyncio.sleep(60)

# --- Metrics ---
metrics = {
    "total_executions": 0,
    "successful_executions": 0,
    "failed_executions": 0,
    "timed_out_executions": 0,
    "active_executions": 0,
}


# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize Docker client and semaphore on startup."""
    global docker_client, execution_semaphore, rate_limit_lock

    docker_client = docker.from_env()
    execution_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    rate_limit_lock = asyncio.Lock()

    # Verify sandbox image exists
    try:
        docker_client.images.get(SANDBOX_IMAGE)
        logger.info(f"Sandbox image '{SANDBOX_IMAGE}' found")
    except docker.errors.ImageNotFound:
        logger.warning(
            f"Sandbox image '{SANDBOX_IMAGE}' not found. "
            "Build it first: docker build -t code-sandbox sandbox/"
        )

    logger.info(
        f"Gateway started — max_concurrent={MAX_CONCURRENT}, "
        f"default_timeout={DEFAULT_TIMEOUT}s, "
        f"mem_limit={SANDBOX_MEM_LIMIT}, "
        f"network={SANDBOX_NETWORK_MODE}"
    )

    # Recover existing containers
    try:
        managed_containers = docker_client.containers.list(
            filters={"label": "managed-by=code-execution-gateway"}
        )
        for c in managed_containers:
            active_sessions[c.id] = SessionInfo(
                last_activity=time.time(),
                network_enabled=infer_network_enabled(c),
            )
            logger.info(
                f"Recovered tracking for container {c.id} "
                f"(network={'on' if active_sessions[c.id].network_enabled else 'off'})"
            )
    except Exception as e:
        logger.warning(f"Failed to recover existing containers: {e}")

    # Start cleanup task
    cleanup_task = asyncio.create_task(cleanup_idle_containers())

    yield

    cleanup_task.cancel()
    docker_client.close()
    logger.info("Gateway shut down")


# --- FastAPI App ---
app = FastAPI(
    title="Code Execution Gateway",
    description="Secure, isolated Python code execution service for LLM models",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Security ---
security = HTTPBearer(auto_error=False)

async def verify_api_key(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    """Verify the Bearer token if API_KEY is set."""
    if not API_KEY:
        return True
    
    if not credentials or credentials.credentials != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return True


class FileInput(BaseModel):
    """An input file to be written into the container."""
    name: str = Field(..., description="File name including extension")
    content: str = Field(..., description="Base64 encoded content of the file")

    @validator("name")
    def validate_name(cls, value: str) -> str:
        sanitized = value.strip()
        if not sanitized:
            raise ValueError("File name cannot be empty")
        if len(sanitized) > MAX_FILE_NAME_LENGTH:
            raise ValueError(f"File name too long (max {MAX_FILE_NAME_LENGTH} chars)")
        if ".." in sanitized or sanitized.startswith(('/', '\\')):
            raise ValueError("File name contains invalid path segments")
        if any(part in {"", ".", ".."} for part in sanitized.split('/')):
            raise ValueError("File name contains invalid components")
        return sanitized


class ExecuteRequest(BaseModel):
    """Request body for code execution."""
    container_id: str = Field(..., description="The ID of the active container session")
    language: str = Field("python", description="Language to execute (python or bash)")
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
    pip_packages: Optional[list[str]] = Field(
        default_factory=list,
        description="Optional list of pip packages to install before execution",
    )
    files: Optional[list[FileInput]] = Field(
        default_factory=list,
        description="Input files to copy to the container before executing",
    )

    @validator("pip_packages", each_item=True)
    def validate_pip_package(cls, pkg: str) -> str:
        name = pkg.strip()
        if not name:
            raise ValueError("Package name cannot be empty")
        if len(name) > MAX_PIP_PACKAGE_NAME_LENGTH:
            raise ValueError(f"Package name too long (max {MAX_PIP_PACKAGE_NAME_LENGTH} chars)")
        if not PIP_PACKAGE_PATTERN.match(name):
            raise ValueError("Package name contains invalid characters")
        return name

    @validator("pip_packages")
    def validate_pip_package_list(cls, packages: list[str]) -> list[str]:
        if not ALLOW_PIP_INSTALLS and packages:
            raise ValueError("Pip installations are disabled")
        if len(packages) > MAX_PIP_PACKAGES:
            raise ValueError(f"Too many pip packages (max {MAX_PIP_PACKAGES})")
        return packages

    @validator("files")
    def validate_file_count(cls, files: list[FileInput]) -> list[FileInput]:
        if len(files) > MAX_INPUT_FILES:
            raise ValueError(f"Too many input files (max {MAX_INPUT_FILES})")
        return files


class ContainerResponse(BaseModel):
    """Response representing a container session."""
    container_id: str
    status: str
    uptime_seconds: float
    last_activity: float

class FileOutput(BaseModel):
    """A file generated during code execution."""
    name: str
    content: Optional[str] = None  # base64-encoded
    mime_type: str
    size: int
    error: Optional[str] = None


class ExecuteResponse(BaseModel):
    """Response from code execution."""
    execution_id: str
    stdout: str
    stderr: str
    error: Optional[str] = None
    error_type: Optional[str] = None
    files: list[FileOutput] = []
    execution_time: float
    timed_out: bool = False


# --- Core Execution Logic ---
async def create_container_session(enable_network: bool = True) -> str:
    """Create a new long-running sandbox container."""
    loop = asyncio.get_event_loop()
    execution_id = str(uuid.uuid4())[:12]
    network_mode = SANDBOX_NETWORK_MODE if enable_network else "none"

    container_config = {
        "image": SANDBOX_IMAGE,
        "mem_limit": SANDBOX_MEM_LIMIT,
        "memswap_limit": SANDBOX_MEM_LIMIT,
        "cpu_period": SANDBOX_CPU_PERIOD,
        "cpu_quota": SANDBOX_CPU_QUOTA,
        "pids_limit": SANDBOX_PIDS_LIMIT,
        "shm_size": SANDBOX_SHM_SIZE,
        "network_mode": network_mode,
        "tmpfs": {
            "/tmp/output": f"size={SANDBOX_TMPFS_SIZE},mode=1777",
            "/tmp/mpl_cache": f"size={SANDBOX_MPL_CACHE_TMPFS_SIZE},mode=1777",
            "/tmp/misc": f"size={SANDBOX_MISC_TMPFS_SIZE},mode=1777",
        },
        "environment": {
            "MPLCONFIGDIR": "/tmp/mpl_cache",
            "TMPDIR": "/tmp/misc",
            "MPLBACKEND": "Agg",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        },
        "cap_drop": ["ALL"],
        "labels": {
            "managed-by": "code-execution-gateway",
            "execution-id": execution_id,
        },
        "name": f"sandbox-{execution_id}",
    }

    seccomp_path = Path(SECCOMP_PROFILE_PATH)
    if seccomp_path.exists():
        with open(seccomp_path) as f:
            seccomp_profile = json.dumps(json.load(f))
        container_config["security_opt"] = [f"seccomp={seccomp_profile}"]
    
    container = await loop.run_in_executor(
        None,
        lambda: docker_client.containers.run(detach=True, **container_config),
    )

    await ensure_sandbox_env_file(container, execution_id=execution_id)

    active_sessions[container.id] = SessionInfo(
        last_activity=time.time(),
        network_enabled=enable_network,
    )
    return container.id


def _read_env_source_bytes() -> Optional[bytes]:
    for path_str in (SANDBOX_ENV_SOURCE_PATH, SANDBOX_ENV_FALLBACK_SOURCE_PATH):
        if not path_str:
            continue
        p = Path(path_str)
        if p.exists() and p.is_file():
            try:
                return p.read_bytes()
            except Exception as e:
                logger.warning(f"Failed reading env source file '{p}': {e}")
                return None
    return None


async def ensure_sandbox_env_file(container: docker.models.containers.Container, execution_id: str) -> None:
    env_bytes = _read_env_source_bytes()
    if env_bytes is None:
        return

    try:
        target = Path(SANDBOX_ENV_TARGET_PATH)
        tar_data = create_tar_archive_from_files(
            [PreparedFile(name=str(target.name), content=env_bytes)]
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: container.put_archive(str(target.parent), tar_data),
        )
    except Exception as e:
        logger.warning(f"[{execution_id}] Failed to provision sandbox .env file: {e}")


def create_tar_archive_from_files(files: list[PreparedFile]) -> bytes:
    """Create a tar archive in memory containing the given files."""
    import tarfile
    from io import BytesIO
    
    tar_stream = BytesIO()
    with tarfile.open(fileobj=tar_stream, mode='w') as tar:
        for file in files:
            info = tarfile.TarInfo(name=file.name)
            info.size = len(file.content)
            # Give proper permissions
            info.mode = 0o666
            tar.addfile(tarinfo=info, fileobj=BytesIO(file.content))
    
    return tar_stream.getvalue()


def prepare_files(files: list[FileInput]) -> list[PreparedFile]:
    prepared: list[PreparedFile] = []
    total_size = 0

    for file in files:
        try:
            content_bytes = base64.b64decode(file.content)
        except binascii.Error:
            raise HTTPException(
                status_code=400,
                detail=f"File '{file.name}' content is not valid base64",
            )

        file_size = len(content_bytes)
        if file_size > MAX_INPUT_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"File '{file.name}' too large ({file_size} bytes, "
                    f"max {MAX_INPUT_FILE_SIZE})"
                ),
            )

        total_size += file_size
        if total_size > MAX_INPUT_TOTAL_SIZE:
            raise HTTPException(
                status_code=400,
                detail="Total size of uploaded files exceeds limit",
            )

        prepared.append(PreparedFile(name=file.name, content=content_bytes))

    return prepared


async def run_code_in_sandbox(
    container_id: str,
    language: str,
    code: str,
    timeout: int,
    execution_id: str,
    pip_packages: Optional[list[str]] = None,
    files: Optional[list[FileInput]] = None,
) -> ExecuteResponse:
    """
    Execute code in an existing ephemeral sandbox container.
    """
    loop = asyncio.get_event_loop()

    session = active_sessions.get(container_id)
    if not session:
        raise HTTPException(status_code=404, detail="Container session not found, or it was shut down due to inactivity.")

    try:
        container = await loop.run_in_executor(
            None,
            lambda: docker_client.containers.get(container_id)
        )
    except docker.errors.NotFound:
         if container_id in active_sessions:
             del active_sessions[container_id]
         raise HTTPException(status_code=404, detail="Container session not found.")
    
    # Update activity
    touch_session(container_id)

    await ensure_sandbox_env_file(container, execution_id=execution_id)

    # Base64-encode the code for safe env var transport
    code_b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")

    prepared_files = prepare_files(files) if files else []

    # Upload files if any
    if prepared_files:
        try:
            tar_data = create_tar_archive_from_files(prepared_files)
            await loop.run_in_executor(
                None,
                lambda: container.put_archive("/home/sandbox", tar_data)
            )
        except Exception as e:
            logger.error(f"[{execution_id}] Failed to upload files: {e}")
            return ExecuteResponse(
                execution_id=execution_id,
                stdout="",
                stderr="",
                error=f"Failed to copy input files to container: {str(e)}",
                error_type="FileUploadError",
                files=[],
                execution_time=0,
                timed_out=False,
            )

    environment = {
        "CODE_B64": code_b64,
        "PIP_PACKAGES": ",".join(pip_packages) if pip_packages else "",
        "ENABLE_NETWORK": "1" if session.network_enabled else "0",
    }

    timed_out = False
    
    try:
        # We use standard docker exec
        exec_cmd = ["python", "/usr/local/bin/executor.py", "--lang", language]
        
        # Create exec instance
        exec_id = await loop.run_in_executor(
            None,
            lambda: docker_client.api.exec_create(
                container.id,
                cmd=exec_cmd,
                environment=environment,
                user="sandbox",
                workdir="/home/sandbox",
            )
        )

        # Start exec string
        async def run_exec():
             return docker_client.api.exec_start(exec_id["Id"])

        try:
            start_result = await asyncio.wait_for(run_exec(), timeout=timeout)
            
            # Wait for completion (exec_start is supposed to run to completion synchronously essentially, but we can poll)
            inspect_result = docker_client.api.exec_inspect(exec_id["Id"])
            
            # Usually start_result gives back the output log
            raw_output = start_result.decode("utf-8", errors="replace")
            exit_code = inspect_result.get("ExitCode", 0)

            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning(f"[{execution_id}] Exec timed out after {timeout}s")
            # There is no direct kill for an exec instance in docker-py easily, we can just return timeout
            # If the container itself needs killing, we could do it, but we prefer keeping it alive
            # However a runaway exec might eat cpu. If we care, we'd restart the container.
            # For now we'll just remove the container entirely to be safe
            logger.warning(f"[{execution_id}] Killing container {container_id} due to timeout to stop runaway process")
            await loop.run_in_executor(None, lambda: container.remove(force=True))
            if container_id in active_sessions:
                 del active_sessions[container_id]
            raw_output = ""
            exit_code = -1

        # Parse the executor's JSON output
        if timed_out:
            return ExecuteResponse(
                execution_id=execution_id,
                stdout="",
                stderr="",
                error=f"Execution timed out after {timeout} seconds. Container removed.",
                error_type="TimeoutError",
                files=[],
                execution_time=float(timeout),
                timed_out=True,
            )

        # The executor writes JSON as the last line of stdout
        try:
            # Find the JSON output (last complete JSON object in output)
            lines = raw_output.strip().split("\n")
            json_line = None
            for line in reversed(lines):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    json_line = line
                    break

            if json_line is None:
                raise ValueError("No JSON output found from executor")

            result_data = json.loads(json_line)

            # Re-update activity because execution could have taken a while
            if container_id in active_sessions:
                touch_session(container_id)

            return ExecuteResponse(
                execution_id=execution_id,
                stdout=result_data.get("stdout", ""),
                stderr=result_data.get("stderr", ""),
                error=result_data.get("error"),
                error_type=result_data.get("error_type"),
                files=[FileOutput(**f) for f in result_data.get("files", [])],
                execution_time=result_data.get("execution_time", 0),
                timed_out=False,
            )

        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"[{execution_id}] Failed to parse executor output: {e}\nRaw: {raw_output}")
            return ExecuteResponse(
                execution_id=execution_id,
                stdout=raw_output[:10000] if raw_output else "",
                stderr="",
                error=f"Failed to parse executor output: {str(e)}",
                error_type="GatewayError",
                files=[],
                execution_time=0,
                timed_out=False,
            )

    except docker.errors.APIError as e:
        logger.error(f"[{execution_id}] Docker API error: {e}")
        return ExecuteResponse(
            execution_id=execution_id,
            stdout="",
            stderr="",
            error=f"Docker API error: {str(e)}",
            error_type="DockerError",
            files=[],
            execution_time=0,
            timed_out=False,
        )


# --- API Endpoints ---

class CreateContainerRequest(BaseModel):
    enable_network: bool = True

@app.post("/containers", response_model=ContainerResponse)
async def create_container(request: CreateContainerRequest = None, _auth: bool = Depends(verify_api_key)):
    """Create a new container session and return its ID."""
    enable_network = request.enable_network if request else True
    try:
        container_id = await create_container_session(enable_network)
        return ContainerResponse(
            container_id=container_id,
            status="active",
            uptime_seconds=0.0,
            last_activity=active_sessions[container_id].last_activity
        )
    except Exception as e:
        logger.error(f"Failed to create container: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/containers/{container_id}", response_model=ContainerResponse)
async def get_container(container_id: str, _auth: bool = Depends(verify_api_key)):
    """Check the status of a container session."""
    if container_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Container session not found")
    
    try:
         docker_client.containers.get(container_id)
         now = time.time()
         session = active_sessions[container_id]
         return ContainerResponse(
             container_id=container_id,
             status="active",
             uptime_seconds=now - session.last_activity,
             last_activity=session.last_activity
         )
    except docker.errors.NotFound:
         del active_sessions[container_id]
         raise HTTPException(status_code=404, detail="Container session not found")

@app.delete("/containers/{container_id}")
async def delete_container(container_id: str, _auth: bool = Depends(verify_api_key)):
    """Delete a container session."""
    try:
         container = docker_client.containers.get(container_id)
         container.remove(force=True)
    except docker.errors.NotFound:
         pass # Already gone
    
    if container_id in active_sessions:
        del active_sessions[container_id]
        
    return {"status": "success", "message": f"Container {container_id} removed."}

@app.post("/execute", response_model=ExecuteResponse)
async def execute_code(request: ExecuteRequest, _auth: bool = Depends(verify_api_key)):
    """
    Execute Python code in a secure, isolated sandbox container.

    The code runs in a fresh Docker container with strict resource limits.
    Any files written to /tmp/output/ by the code will be returned as
    base64-encoded content in the response.

    matplotlib.pyplot.show() is automatically patched to save figures
    to /tmp/output/ instead of displaying them.
    """
    execution_id = str(uuid.uuid4())[:12]
    timeout = request.timeout or DEFAULT_TIMEOUT

    logger.info(
        f"[{execution_id}] Execution request — "
        f"code_length={len(request.code)}, "
        f"timeout={timeout}s, "
        f"network={'on' if request.enable_network else 'off'}"
    )

    metrics["total_executions"] += 1
    metrics["active_executions"] += 1

    try:
        # Acquire semaphore (limits concurrency)
        try:
            await asyncio.wait_for(execution_semaphore.acquire(), timeout=30)
        except asyncio.TimeoutError:
            metrics["active_executions"] -= 1
            raise HTTPException(
                status_code=429,
                detail="Too many concurrent executions. Please try again later.",
            )

        try:
            await check_rate_limit(request.container_id or execution_id)

            result = await run_code_in_sandbox(
                container_id=request.container_id,
                language=request.language,
                code=request.code,
                timeout=timeout,
                execution_id=execution_id,
                pip_packages=request.pip_packages,
                files=request.files,
            )

            if result.timed_out:
                metrics["timed_out_executions"] += 1
            elif result.error:
                metrics["failed_executions"] += 1
            else:
                metrics["successful_executions"] += 1

            logger.info(
                f"[{execution_id}] Execution complete — "
                f"time={result.execution_time}s, "
                f"files={len(result.files)}, "
                f"error={'yes' if result.error else 'no'}, "
                f"timed_out={result.timed_out}"
            )

            return result

        finally:
            execution_semaphore.release()

    finally:
        metrics["active_executions"] -= 1


@app.get("/healthz")
async def health_check():
    """Health check endpoint."""
    try:
        docker_client.ping()
        docker_ok = True
    except Exception:
        docker_ok = False

    # Check if sandbox image is available
    try:
        docker_client.images.get(SANDBOX_IMAGE)
        image_ok = True
    except Exception:
        image_ok = False

    healthy = docker_ok and image_ok

    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "docker_connected": docker_ok,
            "sandbox_image_available": image_ok,
            "sandbox_image": SANDBOX_IMAGE,
            "max_concurrent_executions": MAX_CONCURRENT,
            "default_timeout": DEFAULT_TIMEOUT,
            "metrics": metrics,
        },
    )


@app.get("/metrics")
async def get_metrics():
    """Get execution metrics."""
    return metrics


# --- Error Handler ---
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
