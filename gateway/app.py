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
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import docker
import docker.errors
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

# --- Configuration via Environment Variables ---
SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "code-sandbox:latest")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_EXECUTIONS", "10"))
DEFAULT_TIMEOUT = int(os.getenv("DEFAULT_TIMEOUT", "30"))
MAX_TIMEOUT = int(os.getenv("MAX_TIMEOUT", "120"))
SANDBOX_MEM_LIMIT = os.getenv("SANDBOX_MEM_LIMIT", "512m")
SANDBOX_CPU_PERIOD = int(os.getenv("SANDBOX_CPU_PERIOD", "100000"))
SANDBOX_CPU_QUOTA = int(os.getenv("SANDBOX_CPU_QUOTA", "100000"))  # 1 core
SANDBOX_PIDS_LIMIT = int(os.getenv("SANDBOX_PIDS_LIMIT", "64"))
SANDBOX_TMPFS_SIZE = os.getenv("SANDBOX_TMPFS_SIZE", "100m")
SANDBOX_NETWORK_MODE = os.getenv("SANDBOX_NETWORK_MODE", "bridge")  # "bridge" or "none"
SECCOMP_PROFILE_PATH = os.getenv("SECCOMP_PROFILE_PATH", "/etc/code-execution/seccomp-profile.json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
API_KEY = os.getenv("API_KEY")  # Optional Bearer token protection

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
    global docker_client, execution_semaphore

    docker_client = docker.from_env()
    execution_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

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

    yield

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


# --- Request/Response Models ---
class ExecuteRequest(BaseModel):
    """Request body for code execution."""
    code: str = Field(..., description="Python code to execute", min_length=1, max_length=100_000)
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
async def run_code_in_sandbox(
    code: str,
    timeout: int,
    enable_network: bool,
    execution_id: str,
    pip_packages: Optional[list[str]] = None,
) -> ExecuteResponse:
    """
    Create an ephemeral sandbox container, run the code, and collect results.

    Code is passed via base64-encoded environment variable to avoid
    Docker-in-Docker path mapping and read-only filesystem issues.
    """
    loop = asyncio.get_event_loop()

    # Base64-encode the code for safe env var transport
    code_b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")

    # Determine network mode
    network_mode = SANDBOX_NETWORK_MODE if enable_network else "none"

    # Build container config
    container_config = {
        "image": SANDBOX_IMAGE,
        "mem_limit": SANDBOX_MEM_LIMIT,
        "memswap_limit": SANDBOX_MEM_LIMIT,  # No swap
        "cpu_period": SANDBOX_CPU_PERIOD,
        "cpu_quota": SANDBOX_CPU_QUOTA,
        "pids_limit": SANDBOX_PIDS_LIMIT,
        "network_mode": network_mode,
        "read_only": True,
        "tmpfs": {
            "/tmp/output": f"size={SANDBOX_TMPFS_SIZE},mode=1777",
            "/tmp/mpl_cache": "size=10m,mode=1777",
            "/tmp/misc": "size=10m,mode=1777",
            "/home/sandbox": "size=10m,mode=1777",
        },
        "environment": {
            "CODE_B64": code_b64,
            "MPLCONFIGDIR": "/tmp/mpl_cache",
            "TMPDIR": "/tmp/misc",
            "MPLBACKEND": "Agg",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PIP_PACKAGES": ",".join(pip_packages) if pip_packages else "",
        },
        "cap_drop": ["ALL"],
        "labels": {
            "managed-by": "code-execution-gateway",
            "execution-id": execution_id,
        },
        "name": f"sandbox-{execution_id}",
    }

    # Add seccomp profile if it exists
    seccomp_path = Path(SECCOMP_PROFILE_PATH)
    if seccomp_path.exists():
        with open(seccomp_path) as f:
            seccomp_profile = json.dumps(json.load(f))
        container_config["security_opt"] = [f"seccomp={seccomp_profile}"]
        logger.debug(f"[{execution_id}] Using seccomp profile from {SECCOMP_PROFILE_PATH}")
    else:
        logger.debug(f"[{execution_id}] No seccomp profile found at {SECCOMP_PROFILE_PATH}")

    container = None
    timed_out = False

    try:
        # Create the container (don't start yet)
        container = await loop.run_in_executor(
            None,
            lambda: docker_client.containers.create(**container_config),
        )

        # Start the container
        await loop.run_in_executor(None, lambda: container.start())

        # Wait for the container to finish with timeout
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: container.wait()),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning(f"[{execution_id}] Execution timed out after {timeout}s")
            # Kill the container
            try:
                await loop.run_in_executor(None, lambda: container.kill())
            except Exception:
                pass

        # Get container logs (this is the JSON output from executor.py)
        raw_output = await loop.run_in_executor(
            None,
            lambda: container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace"),
        )

        # Parse the executor's JSON output
        if timed_out:
            return ExecuteResponse(
                execution_id=execution_id,
                stdout="",
                stderr="",
                error=f"Execution timed out after {timeout} seconds",
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
            logger.error(f"[{execution_id}] Failed to parse executor output: {e}")
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

    except docker.errors.ContainerError as e:
        # Container exited with non-zero exit code
        raw_output = e.stderr.decode("utf-8", errors="replace") if e.stderr else ""
        try:
            lines = raw_output.strip().split("\n")
            json_line = None
            for line in reversed(lines):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    json_line = line
                    break
            if json_line:
                result_data = json.loads(json_line)
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
        except Exception:
            pass

        return ExecuteResponse(
            execution_id=execution_id,
            stdout="",
            stderr=raw_output[:10000],
            error=f"Container exited with code {e.exit_status}",
            error_type="ContainerError",
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

    finally:
        # Always clean up the container
        if container:
            try:
                await loop.run_in_executor(None, lambda: container.remove(force=True))
                logger.debug(f"[{execution_id}] Container removed")
            except Exception as e:
                logger.warning(f"[{execution_id}] Failed to remove container: {e}")


# --- API Endpoints ---

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
            result = await run_code_in_sandbox(
                code=request.code,
                timeout=timeout,
                enable_network=request.enable_network,
                execution_id=execution_id,
                pip_packages=request.pip_packages,
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


@app.get("/health")
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
            "status": "healthy" if healthy else "degraded",
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
