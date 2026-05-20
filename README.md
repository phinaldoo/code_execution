# Code Execution Gateway

Code Execution Gateway is a sub-service of the ChatUI project. It runs untrusted Python and Bash code in isolated Docker sandbox sessions and returns stdout, stderr, structured errors, execution timing, and generated files as JSON.

> **Disclaimer:** This software is provided "as is" without any warranties. Use at your own risk. The maintainers are not liable for damages resulting from use.

The service is built for LLM and chat UI workflows where the main ChatUI backend should not execute model-generated code directly. Instead, ChatUI calls this external gateway, the gateway creates or reuses a sandbox container, and submitted code runs inside that sandbox.

Technical details:

- **FastAPI gateway** for auth, request validation, rate limits, Docker orchestration, health, metrics, and version metadata.
- **Docker sandbox image** for Python, Bash, Playwright Chromium, data analysis packages, plotting, document utilities, and output collection.
- **Redis state backend** for shared session state, distributed locks, and rate limits in multi-replica deployments.
- **Restricted docker-socket-proxy** for local development access to Docker without mounting the raw socket into the gateway.

## Important Security Warning

This service uses Docker containers as the sandbox boundary. It does not create a dedicated virtual machine for each sandbox session.

On Linux hosts, sandbox containers share the host kernel. On macOS and Windows Docker Desktop, containers usually run inside Docker Desktop's Linux VM, but this project still manages Docker containers, not VMs. Treat this as hardened container isolation, not VM-grade isolation.

Do not expose this service to arbitrary hostile users unless you add stronger isolation and operational controls. For public beta or high-risk workloads, use dedicated disposable hosts or a stronger boundary such as microVMs, VMs, gVisor, or Kata Containers, then enable `PUBLIC_BETA_MODE=true` so the gateway rejects risky configuration. Protect Docker daemon access carefully; access to the Docker socket or an overly permissive Docker API proxy can be equivalent to host-level control.

## What It Does

- Creates authenticated sandbox sessions through `POST /containers`.
- Executes Python or Bash in an existing session through `POST /execute`.
- Preserves `/home/sandbox` state across executions in the same session.
- Captures stdout, stderr, structured errors, execution time, and generated files.
- Auto-saves Matplotlib figures when Python code calls `plt.show()`.
- Supports input files uploaded as base64 and output files written to `/tmp/output`.
- Can run Playwright Chromium inside the sandbox image.
- Supports optional per-request pip installs when explicitly enabled.
- Supports optional sandbox `.env` injection for trusted workflows.
- Enforces memory, CPU, PID, timeout, request size, file size, rate, and concurrency limits.
- Uses Redis for shared session state and distributed locks in the Compose stack.
- Exposes version metadata, health checks, Prometheus metrics, and JSON debug metrics.

## Architecture

```text
client
  |
  | HTTP + Bearer token
  v
FastAPI gateway
  |
  | Docker API through restricted docker-socket-proxy or a remote Docker daemon
  v
sandbox container session
  |
  | docker exec python /usr/local/bin/executor.py
  v
executor captures output and files
```

Main components:

- `gateway/app.py` - FastAPI app, auth, rate limits, Docker container lifecycle, execution orchestration, health, metrics, and version endpoint.
- `gateway/version.json` - gateway release version used by `/version`, OpenAPI metadata, and version response headers.
- `gateway/state.py` - in-memory and Redis state backends for sessions, locks, and rate limits.
- `sandbox/executor.py` - code runner inside each sandbox container.
- `docker-compose.yml` - local development stack with gateway, Redis, docker socket proxy, and sandbox image build target.
- `setup.sh` and `setup.ps1` - cross-platform setup helpers that create or sync `.env` and generate a local API key.
- `security/seccomp-profile.json` - optional fallback seccomp allowlist profile. The default runtime path uses Docker's default seccomp policy.
- `tests/` - unit, smoke, integration, Playwright, and feature verification scripts.

## Setup

These instructions are for macOS, Linux, and Windows.

### Prerequisites

- Docker
  - macOS: install Docker Desktop.
  - Linux: install Docker Engine and the Docker Compose v2 plugin.
  - Windows: install Docker Desktop with WSL 2 enabled.
- Docker Compose v2, available as the `docker compose` command.
- `make`, optional but recommended for the shortest commands.
- Python 3.12+, only needed for local unit tests and verification scripts. Docker builds the gateway and sandbox runtime images.
- PowerShell, only needed for native Windows setup without WSL/Git Bash.

Check required tools:

macOS/Linux or Windows with WSL/Git Bash:

```bash
docker --version
docker compose version
python3 --version  # or: python --version
```

If you want to use the Makefile path, also check:

```bash
make --version
```

Windows PowerShell:

```powershell
docker --version
docker compose version
python --version
```

### Option 1: macOS/Linux/Windows With Makefile

Use this path if `make` is installed. On macOS/Linux, `make setup` uses `setup.sh`; on Windows, it uses `setup.ps1`.

```bash
# Prepare .env and generate API_KEYS if needed
make setup

# Build the gateway and sandbox images
make build

# Start the local development stack in the background
make up
```

The service is available at:

- `http://localhost:8000`
- `http://localhost:8000/healthz`
- `http://localhost:8000/version`

Useful Makefile commands:

```bash
make setup    # Create or sync .env and generate API_KEYS if needed
make build    # Build gateway and sandbox images
make up       # Build and start the local stack
make down     # Stop containers and remove local stack containers
make restart  # Restart all services
make logs     # Follow logs
make ps       # Show container status
```

### Option 2: macOS/Linux Without Makefile

Use this path if you do not have `make` installed or prefer plain shell commands.

```bash
# Prepare .env and generate API_KEYS if needed
bash ./setup.sh

# Build the gateway and sandbox images
docker compose --profile local-docker --profile build build

# Start the gateway, Redis, and docker socket proxy
docker compose --profile local-docker up -d

# Check status
docker compose --profile local-docker ps

# Follow logs
docker compose --profile local-docker logs -f
```

Useful Docker Compose commands:

```bash
docker compose --profile local-docker --profile build build
docker compose --profile local-docker up -d
docker compose --profile local-docker down --remove-orphans
docker compose --profile local-docker restart
docker compose --profile local-docker logs -f
docker compose --profile local-docker ps
```

### Option 3: Windows PowerShell Without Makefile

Use this path for native Windows setup.

```powershell
# Prepare .env and generate API_KEYS if needed
powershell -ExecutionPolicy Bypass -File .\setup.ps1

# Build the gateway and sandbox images
docker compose --profile local-docker --profile build build

# Start the gateway, Redis, and docker socket proxy
docker compose --profile local-docker up -d

# Check status
docker compose --profile local-docker ps

# Follow logs
docker compose --profile local-docker logs -f
```

Useful Docker Compose commands:

```powershell
docker compose --profile local-docker --profile build build
docker compose --profile local-docker up -d
docker compose --profile local-docker down --remove-orphans
docker compose --profile local-docker restart
docker compose --profile local-docker logs -f
docker compose --profile local-docker ps
```

### What Setup Creates

`make setup`, `bash ./setup.sh`, and `powershell -ExecutionPolicy Bypass -File .\setup.ps1` do the same preparation:

- Create `.env` from `.env.example` if it does not exist.
- Add new keys from `.env.example` into an existing `.env`.
- Generate a secure local `API_KEYS=local:<secret>` value when one is missing, too short, or left as a placeholder.

After setup, review `.env` if you want to change the port, CORS origins, network mode, resource limits, authentication, Redis, Docker daemon target, or production hardening.

### Why Compose Profiles Are Used

The local stack uses two Compose profiles:

- `local-docker` starts the restricted Docker socket proxy used by the gateway in local development.
- `build` builds the sandbox image. The sandbox service is an image build target and does not stay running.

For local development, use both profiles when building, and use `local-docker` when starting:

```bash
docker compose --profile local-docker --profile build build
docker compose --profile local-docker up -d
```

### Changing the Port

By default, the gateway listens on host port `8000`. To use another port, edit `.env`:

```env
GATEWAY_PORT=8010
```

Then restart the stack:

```bash
make restart
```

Without Makefile:

```bash
docker compose --profile local-docker restart
```

Windows PowerShell:

```powershell
docker compose --profile local-docker restart
```

## Authentication

Most endpoints require a Bearer token when `REQUIRE_AUTH=true`, which is the default in `.env.example`.

Static API keys are configured with `API_KEYS`:

```env
API_KEYS=local:replace-with-a-long-random-secret
```

Use the secret portion after the colon as the Bearer token.

macOS/Linux:

```bash
TOKEN="$(grep '^API_KEYS=' .env | cut -d= -f2- | cut -d: -f2-)"
```

Windows PowerShell:

```powershell
$TOKEN = ((Get-Content .env | Where-Object { $_ -like 'API_KEYS=*' } | Select-Object -First 1) -split '=', 2)[1]
$TOKEN = ($TOKEN -split ':', 2)[1]
```

JWT authentication is also supported. When `JWT_SECRET` is configured, Bearer tokens are first validated as JWTs. If static API keys are also configured, a token that is not a valid JWT can still authenticate as a static API key. JWTs must include `exp` and `sub`; optional issuer, audience, algorithms, and tenant claim are controlled by environment variables.

## API Usage

Check the service:

```bash
curl -sS http://localhost:8000/
curl -sS http://localhost:8000/version
curl -sS http://localhost:8000/healthz
```

Create a sandbox session:

```bash
curl -sS http://localhost:8000/containers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enable_network": false}'
```

Run Python:

```bash
CONTAINER_ID="paste-container-id-here"

curl -sS http://localhost:8000/execute \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"container_id\": \"$CONTAINER_ID\",
    \"language\": \"python\",
    \"code\": \"import math\nprint(math.factorial(20))\",
    \"timeout\": 30
  }"
```

Run Bash:

```bash
curl -sS http://localhost:8000/execute \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"container_id\": \"$CONTAINER_ID\",
    \"language\": \"bash\",
    \"code\": \"echo hello from bash\",
    \"timeout\": 10
  }"
```

Delete the session:

```bash
curl -sS -X DELETE http://localhost:8000/containers/$CONTAINER_ID \
  -H "Authorization: Bearer $TOKEN"
```

PowerShell examples use `Invoke-RestMethod`:

```powershell
$Headers = @{ Authorization = "Bearer $TOKEN" }
$Container = Invoke-RestMethod -Method Post -Uri "http://localhost:8000/containers" -Headers $Headers -ContentType "application/json" -Body '{"enable_network":false}'
$Body = @{
    container_id = $Container.container_id
    language = "python"
    code = "print('hello from python')"
    timeout = 30
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/execute" -Headers $Headers -ContentType "application/json" -Body $Body
Invoke-RestMethod -Method Delete -Uri "http://localhost:8000/containers/$($Container.container_id)" -Headers $Headers
```

## API Reference

### Endpoints

- `GET /` - service metadata.
- `GET /version` - release and execution contract metadata.
- `GET /health` and `GET /healthz` - lightweight health, unauthenticated.
- `GET /health/details` and `GET /healthz/details` - detailed health, authenticated when `REQUIRE_AUTH=true`.
- `GET /metrics` - Prometheus metrics, protected by `METRICS_AUTH_REQUIRED`.
- `GET /metrics/json` - JSON debug counters, authenticated when `REQUIRE_AUTH=true`.
- `POST /containers` - create a sandbox session.
- `GET /containers/{container_id}` - inspect a sandbox session.
- `DELETE /containers/{container_id}` - delete a sandbox session.
- `POST /execute` - execute Python or Bash in a sandbox session.

### Create Container Request

```json
{
  "enable_network": false,
  "inject_sandbox_env": false
}
```

Notes:

- Network access is fixed when the container session is created. A later `/execute` request cannot safely downgrade an already-networked session.
- `inject_sandbox_env` only works when `ALLOW_SANDBOX_ENV_INJECTION=true` and the configured source file is readable.

### Create Container Response

```json
{
  "container_id": "container-id",
  "status": "active",
  "uptime_seconds": 0.0,
  "last_activity": 1710000000.0,
  "docker_daemon_id": "daemon-id"
}
```

### Execute Request

```json
{
  "container_id": "container-id",
  "language": "python",
  "code": "print('hello')",
  "timeout": 30,
  "enable_network": false,
  "pip_packages": [],
  "files": [
    {
      "name": "input.txt",
      "content": "base64-encoded-file-content"
    }
  ]
}
```

Notes:

- `language` must be `python` or `bash`.
- `timeout` is capped by `MAX_TIMEOUT`.
- `pip_packages` is rejected unless `ALLOW_PIP_INSTALLS=true`.
- `files` are copied into `/home/sandbox` before execution.
- Output files must be written under `/tmp/output` inside the sandbox.

### Execute Response

```json
{
  "execution_id": "abc123",
  "stdout": "hello\n",
  "stderr": "",
  "error": null,
  "error_type": null,
  "files": [],
  "execution_time": 0.01,
  "install_time": null,
  "timed_out": false
}
```

Returned file contents are base64 encoded:

```json
{
  "name": "plot.png",
  "content": "iVBORw0KGgoAAAANSUhEUgAA...",
  "mime_type": "image/png",
  "size": 12345,
  "error": null
}
```

### Version Response

`GET /version` returns:

```json
{
  "version": "1.1.0",
  "tag": "v1.1.0",
  "api_contract_version": 1,
  "beta": false,
  "active_execution_version": "v1",
  "default_execution_version": "v1",
  "supported_execution_versions": ["v1"],
  "available_execution_versions": ["v1"],
  "features": {
    "gateway_version_headers": true,
    "persistent_sessions": true,
    "input_files": true,
    "pip_packages": true
  }
}
```

Normal responses include:

- `X-Request-ID`
- `X-Code-Execution-Version`
- `X-Code-Execution-Version-Tag`

## Sandbox Behavior

Each container is a session. Files written under `/home/sandbox` persist until the session is deleted or cleaned up after idleness. Files under `/tmp/output` are collected after each execution and then cleared.

Python execution details:

- User code is passed through `CODE_B64` and executed by `sandbox/executor.py`.
- stdout and stderr are captured.
- Tracebacks are filtered to hide executor internals.
- Matplotlib uses the `Agg` backend.
- `plt.show()` saves open figures as PNG files in `/tmp/output`.
- Output text is truncated after 100,000 characters.

Bash execution details:

- Submitted code is written to a temporary script under `/tmp/misc`.
- Background child processes are terminated after each run.
- Non-zero exit codes are returned as `BashExitError`.

The sandbox image includes common packages for data analysis, visualization, browser automation, document handling, and file formats, including NumPy, pandas, SciPy, Matplotlib, seaborn, scikit-learn, SymPy, requests, Playwright, openpyxl, PyYAML, reportlab, and python-docx.

Sandbox containers use a read-only root filesystem by default. Writable state is limited to bounded tmpfs mounts, primarily `/home/sandbox` for per-session files and `/tmp` for execution scratch space, output collection, and library caches.

## Security

### Public Exposure Checklist

Do not expose this service to the internet or a shared network until all of the following are true:

- `REQUIRE_AUTH=true` and `API_KEYS` or JWT auth is configured with fresh, long, random secrets created for this deployment.
- Traffic is protected by TLS at an upstream reverse proxy, load balancer, ingress, or service mesh.
- `APP_ENV=production` for controlled live deployments, or `APP_ENV=public_beta` / `PUBLIC_BETA_MODE=true` for arbitrary untrusted beta users.
- `ENABLE_DOCS=false`.
- `REQUIRE_SHARED_STATE=true` and `REDIS_URL` points at a durable, access-controlled Redis deployment.
- `GATEWAY_DOCKER_HOST` or `DOCKER_HOST` points at a dedicated remote Docker daemon over TLS (`tcp://...:2376`) or SSH (`ssh://...`), not the local socket proxy.
- `CORS_ALLOW_ORIGINS` is restricted to the real ChatUI origin or origins. Do not use wildcard CORS with credentials.
- `SANDBOX_NETWORK_MODE=none` unless network access is explicitly required and isolated.
- Public beta deployments configure `SANDBOX_RUNTIME` to a stronger runtime such as gVisor/runsc or Kata Containers and set `REQUIRE_STRONG_SANDBOX_ISOLATION=true`.
- `SANDBOX_IMAGE` uses an immutable tag or digest, not `latest`.
- `SANDBOX_READ_ONLY_ROOTFS=true`.
- `ALLOW_PIP_INSTALLS=false` for untrusted workloads.
- `ALLOW_SANDBOX_ENV_INJECTION=false` unless submitted code is trusted.
- `SESSION_TIMEOUT_SECONDS`, `MAX_SESSION_LIFETIME_SECONDS`, and `MAX_EXECUTIONS_PER_SESSION` are set to realistic abuse budgets.
- CPU, memory, PID, request-size, file-size, timeout, session, and rate limits are tuned for your host capacity.
- Real secrets are stored outside source control and rotated if they were ever shared, logged, or used in another environment.

### Docker Daemon Safety

The gateway creates and executes containers, so Docker daemon access is highly sensitive.

Local development uses `docker-proxy` through `GATEWAY_DOCKER_HOST=tcp://docker-proxy:2375`. This is acceptable only inside the local Compose network. In production, use a dedicated remote Docker daemon or worker pool. The gateway intentionally rejects production configurations that use a raw Unix socket, loopback Docker host, local docker proxy, or plain unencrypted TCP on port `2375`.

### Isolation Model

Implemented controls include:

- Non-root gateway and sandbox users.
- Docker capability drop with `no-new-privileges`.
- Configurable CPU, memory, PID, shared memory, and tmpfs limits.
- Optional network isolation with `SANDBOX_NETWORK_MODE=none`.
- Read-only sandbox root filesystem by default.
- Per-execution timeout with container teardown on timeout.
- Per-container execution locks to prevent concurrent mutation of one session.
- Global execution concurrency limits.
- Per-principal request and container creation rate limits.
- Maximum active session limits globally and per principal.
- Base64 input file validation, duplicate name rejection, path traversal prevention, and file size limits.
- Session ownership checks by authenticated principal and tenant.
- Redis-backed shared state for multi-replica coordination.
- Prometheus metrics and health endpoints for operations.

These controls reduce risk but do not make Docker's default container runtime equivalent to VMs. Use a stronger runtime or dedicated disposable worker hosts before accepting arbitrary public users.

### Vulnerability Disclosure

Please see [SECURITY.md](./SECURITY.md) for details on how to report security vulnerabilities.

## Configurable Environment Variables

See `.env.example` for source defaults. `setup.sh` and `setup.ps1` create `.env` from that file and generate `API_KEYS` when needed.

### Core And Build

| Variable | Default | Description | Best practices |
| --- | --- | --- | --- |
| `APP_ENV` | `development` | Deployment environment. Production guardrails are enforced when this is `production` or `prod`; public beta guardrails are enabled when this is `public_beta`, `public-beta`, or `beta`. | Use `development` locally, `staging` before launch, `production` for controlled live deployments, and `public_beta` only with stronger sandbox isolation. |
| `PUBLIC_BETA_MODE` | `false` | Enables strict public beta validation regardless of `APP_ENV`. | Set `true` before exposing arbitrary untrusted beta users. This requires no sandbox network, no pip installs, no env injection, immutable images, and a stronger runtime. |
| `GATEWAY_PORT` | `8000` | Host port mapped to the gateway container's port `8000`. | Keep `8000` locally unless it conflicts. In production, place the service behind TLS infrastructure and expose only required ports. |
| `LOG_LEVEL` | `INFO` | Gateway Python logging level. | Use `INFO` normally. Use `DEBUG` only for temporary debugging because logs may contain operational details. |
| `ENABLE_DOCS` | `false` | Enables FastAPI `/docs` and `/openapi.json`. | Keep `false` in production. Enable only for local debugging or restricted non-production environments. |
| `PYTHON_BASE_IMAGE` | pinned `python:3.12-slim-bookworm` digest | Base image used by the gateway and sandbox Dockerfiles. | Keep pinned for reproducible builds. Update deliberately during maintenance. |

### Authentication

| Variable | Default | Description | Best practices |
| --- | --- | --- | --- |
| `REQUIRE_AUTH` | production-aware, `.env.example`: `true` | Requires Bearer authentication for API endpoints. | Keep `true` for anything except isolated local debugging. |
| `METRICS_AUTH_REQUIRED` | production-aware, `.env.example`: `true` | Requires Bearer authentication for `/metrics`. | Keep `true` outside private local development. |
| `API_KEYS` | empty until setup | Comma-separated static API keys. Values may be `key_id:secret` or just `secret`. | Let setup generate a local key. Use long random per-environment secrets and rotate by temporarily listing old and new keys. |
| `API_KEY` | empty | Legacy single-key fallback used only when `API_KEYS` is empty. | Prefer `API_KEYS`. |
| `JWT_SECRET` | empty | Enables JWT authentication when set. JWTs must include `exp` and `sub`. | Use a strong secret or key material managed by your identity infrastructure. |
| `JWT_ALGORITHMS` | `HS256` | Comma-separated JWT algorithms accepted by PyJWT. | Keep narrow. Do not accept algorithms you do not issue. |
| `JWT_ISSUER` | empty | Optional required JWT issuer. | Set in production when using JWT auth. |
| `JWT_AUDIENCE` | empty | Optional required JWT audience. | Set in production when using JWT auth. |
| `JWT_TENANT_CLAIM` | `tenant_id` | JWT claim used for tenant scoping. | Match your identity provider and ChatUI tenant model. |

### CORS

| Variable | Default | Description | Best practices |
| --- | --- | --- | --- |
| `ENABLE_CORS` | `true` | Enables FastAPI CORS middleware when origins are configured. | Keep enabled when browser clients call the gateway directly. Disable behind a same-origin proxy if not needed. |
| `CORS_ALLOW_ORIGINS` | `http://localhost:3000` | Comma-separated allowed origins. | Use exact ChatUI origins in production. Do not combine `*` with credentials. |
| `CORS_ALLOW_METHODS` | `GET,POST,DELETE,OPTIONS` | Comma-separated allowed CORS methods. | Keep minimal. |
| `CORS_ALLOW_HEADERS` | `Authorization,Content-Type,X-Request-ID` | Comma-separated allowed CORS headers. | Add only headers your clients actually send. |
| `CORS_ALLOW_CREDENTIALS` | `true` | Allows credentialed browser requests. | Keep `true` only with explicit origins. |

### Docker, State, And Redis

| Variable | Default | Description | Best practices |
| --- | --- | --- | --- |
| `GATEWAY_DOCKER_HOST` | `tcp://docker-proxy:2375` locally | Compose variable passed into the gateway as `DOCKER_HOST`. | Use local docker-proxy only for development. In production, point at a dedicated remote daemon over TLS or SSH. |
| `DOCKER_HOST` | empty in direct process runs | Docker daemon endpoint read by `docker.from_env()` inside the gateway. | For non-Compose deployments, set this directly to a safe remote daemon. |
| `DOCKER_CLIENT_TIMEOUT` | `30` | Docker API client timeout in seconds. | Keep bounded so Docker API hangs do not pin request workers indefinitely. |
| `USE_DOCKER_DEFAULT_SECCOMP` | `true` | Uses Docker runtime default seccomp policy. | Keep `true` unless you have a tested daemon-visible profile. |
| `SECCOMP_PROFILE_DAEMON_PATH` | empty | Absolute path to a seccomp profile on the Docker daemon host when default seccomp is disabled. `SECCOMP_PROFILE_PATH` is accepted as a legacy alias. | Set only if `USE_DOCKER_DEFAULT_SECCOMP=false`; the path must exist on the daemon host, not merely in this repository. |
| `REDIS_URL` | `redis://redis:6379/0` | Redis URL for shared sessions, locks, and rate limits. | Use Redis in production and for multi-replica deployments. |
| `REQUIRE_SHARED_STATE` | production-aware, `.env.example`: `true` | Requires `REDIS_URL` when enabled. | Keep `true` in production. Disable only for single-process local tests. |
| `REDIS_SOCKET_CONNECT_TIMEOUT` | `5` | Redis connect timeout in seconds. | Lower for quick failure detection; raise only for slow networks. |
| `REDIS_SOCKET_TIMEOUT` | `5` | Redis socket operation timeout in seconds. | Keep bounded to avoid stuck requests. |
| `REDIS_HEALTH_CHECK_INTERVAL` | `30` | Redis client health check interval in seconds. | Default is usually fine. |

### Request, File, And Execution Limits

| Variable | Default | Description | Best practices |
| --- | --- | --- | --- |
| `MAX_REQUEST_BODY_SIZE` | `33554432` | Maximum HTTP request body size in bytes before JSON parsing. | Keep as small as practical for expected code and input files. |
| `MAX_INPUT_FILES` | `10` | Maximum number of input files on one execution request. | Keep low for untrusted workloads. |
| `MAX_INPUT_FILE_SIZE` | `5242880` | Maximum decoded size of one input file in bytes. | Tune for expected uploads; keep below total size. |
| `MAX_INPUT_TOTAL_SIZE` | `20971520` | Maximum decoded size of all input files in one request. | Keep below `MAX_REQUEST_BODY_SIZE`. |
| `MAX_FILE_NAME_LENGTH` | `128` | Maximum input file name length. | Keep bounded to simplify logging and filesystem handling. |
| `DEFAULT_TIMEOUT` | `30` | Default execution timeout in seconds when a request omits `timeout`. | Keep short for interactive chat workflows. |
| `MAX_TIMEOUT` | `120` | Maximum accepted execution timeout in seconds. | Keep bounded. Increase only for trusted workflows or larger hosts. |
| `MAX_CONCURRENT_EXECUTIONS` | `10` | Gateway-wide execution concurrency. | Tune to CPU and memory. Lower this before exposing to many users. |
| `FILE_PROVISION_TIMEOUT` | `30` | Timeout in seconds for copying input files or injected env files into a container. | Keep below normal execution timeout unless large input files require more time. |

### Rate And Session Limits

| Variable | Default | Description | Best practices |
| --- | --- | --- | --- |
| `RATE_LIMIT_REQUESTS_PER_WINDOW` | `30` | Per-principal execution request limit per window. | Lower for public or shared deployments. |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Execution rate limit window size in seconds. | Default gives per-minute limits. |
| `CONTAINER_RATE_LIMIT_REQUESTS_PER_WINDOW` | `10` | Per-principal container creation limit per window. | Keep lower than execution limits because containers are heavier. |
| `CONTAINER_RATE_LIMIT_WINDOW_SECONDS` | `60` | Container creation rate limit window size in seconds. | Default gives per-minute limits. |
| `MAX_ACTIVE_SESSIONS` | `100` | Maximum active sessions tracked by the gateway. | Size to host capacity and Redis/state expectations. |
| `MAX_CONTAINERS_PER_PRINCIPAL` | `3` | Maximum active sessions per authenticated subject and tenant. | Keep small for shared deployments. |
| `CONTAINER_CREATE_GUARD_TIMEOUT` | `30` | Timeout in seconds while waiting for the serialized container creation guard. | Increase only if Docker is slow during normal operation. |
| `SESSION_TIMEOUT_SECONDS` | `1200` | Idle timeout for sandbox sessions. | Keep short for public/shared deployments. |
| `MAX_SESSION_LIFETIME_SECONDS` | `3600` | Hard lifetime for a sandbox session, regardless of activity. | Prevents users from keeping containers alive forever. Lower for public beta. |
| `MAX_EXECUTIONS_PER_SESSION` | `100` | Maximum number of executions allowed in one session before it is removed. | Use this as a per-session abuse budget. Lower for public beta. |

### Sandbox Runtime

| Variable | Default | Description | Best practices |
| --- | --- | --- | --- |
| `SANDBOX_IMAGE` | `code-sandbox:latest` | Docker image used for sandbox sessions. | Use immutable image tags or digests in production. |
| `SANDBOX_RUNTIME` | empty | Optional Docker runtime for sandbox containers, for example `runsc` for gVisor or `kata-runtime` for Kata Containers. | Required in public beta mode. Configure the runtime on the Docker daemon host first. |
| `STRONG_SANDBOX_RUNTIMES` | `runsc,kata,kata-runtime,io.containerd.runsc.v1,io.containerd.kata.v2` | Comma-separated runtime names that satisfy strong isolation checks. | Keep narrow and aligned with runtimes actually installed on workers. |
| `REQUIRE_STRONG_SANDBOX_ISOLATION` | public-beta-aware, `.env.example`: `false` | Requires `SANDBOX_RUNTIME` to match `STRONG_SANDBOX_RUNTIMES`. | Set `true` for any deployment that accepts arbitrary untrusted users. |
| `SANDBOX_USER` | `sandbox` | User name recorded for sandbox behavior and defaults. | Keep aligned with the sandbox image. |
| `SANDBOX_UID` | `10001` | Sandbox Linux user ID. | Keep non-root. |
| `SANDBOX_GID` | `10001` | Sandbox Linux group ID. | Keep non-root. |
| `SANDBOX_MEM_LIMIT` | `512m` | Docker memory limit for each sandbox session. | Tune for expected code and plotting workloads. |
| `SANDBOX_CPU_PERIOD` | `100000` | Docker CPU CFS period for sandbox sessions. | Change only if you understand Docker CPU quota controls. |
| `SANDBOX_CPU_QUOTA` | `100000` | Docker CPU CFS quota for sandbox sessions. Default is about one CPU. | Lower for tighter isolation; raise for trusted heavier jobs. |
| `SANDBOX_PIDS_LIMIT` | `256` | Maximum process count in each sandbox. | Keep bounded to limit fork-heavy code. |
| `SANDBOX_TMP_ROOT_SIZE` | `512m` | Bounded tmpfs size for `/tmp` in the sandbox. | Size for temporary execution files and collected outputs. |
| `SANDBOX_SHM_SIZE` | `128m` | Shared memory size for sandbox containers. | Increase if browser or plotting workloads need more shared memory. |
| `SANDBOX_HOME_TMPFS_SIZE` | `256m` | Bounded tmpfs size for `/home/sandbox`. | This is per-session persistent scratch space. |
| `SANDBOX_READ_ONLY_ROOTFS` | `true` | Runs sandbox containers with a read-only root filesystem. | Keep `true` for untrusted workloads. |
| `SANDBOX_NETWORK_MODE` | `none` | Docker network mode for sandbox sessions. `none` disables network; `bridge` enables network. | Keep `none` for untrusted workloads. Enable `bridge` only when required. |

### Optional Risky Features

| Variable | Default | Description | Best practices |
| --- | --- | --- | --- |
| `ALLOW_PIP_INSTALLS` | `false` | Allows clients to request per-execution `pip install --user` before code runs. | Keep `false` for untrusted workloads. Prefer baking packages into the sandbox image. |
| `MAX_PIP_PACKAGES` | `5` | Maximum package specifiers accepted in one request. | Keep low if pip installs are enabled. |
| `MAX_PIP_PACKAGE_NAME_LENGTH` | `64` | Maximum length of one package specifier. | Keep bounded to reduce abuse and parsing risk. |
| `ALLOW_SANDBOX_ENV_INJECTION` | `false` | Allows clients to request copying a configured env file into the sandbox. | Enable only for trusted workflows. |
| `SANDBOX_ENV_SOURCE_PATH` | repo `.env_sandbox`; Compose overrides to `/etc/code-execution/.env_sandbox` | Source file read by the gateway for sandbox env injection. | Store only sandbox-scoped values here. Never inject host or admin secrets into untrusted sessions. |
| `SANDBOX_ENV_TARGET_PATH` | `/home/sandbox/.env` | Target path inside the sandbox for injected env values. | Keep under `/home/sandbox`. |

## Environment Injection

The gateway can copy a sandbox environment file into `/home/sandbox/.env`, but this is disabled by default.

To use it:

1. Put sandbox-only values in `.env_sandbox`.
2. Set `ALLOW_SANDBOX_ENV_INJECTION=true`.
3. Create the container with `"inject_sandbox_env": true`.

Only enable this for trusted workflows. Values injected into a sandbox can be read by code running in that session.

## Dynamic Pip Installs

Per-request pip installs are disabled by default:

```env
ALLOW_PIP_INSTALLS=false
```

When enabled, clients may send package specifiers in `pip_packages`. Package names are validated, installed with `pip install --user --no-cache-dir --quiet`, and counted within the execution timeout.

For untrusted workloads, prefer baking required packages into the sandbox image instead of allowing runtime installs.

## Health And Metrics

Version metadata:

```text
GET /version
```

Unauthenticated lightweight health:

```text
GET /health
GET /healthz
```

Authenticated detailed health:

```text
GET /health/details
GET /healthz/details
```

Metrics:

```text
GET /metrics       Prometheus format
GET /metrics/json  Debug JSON counters
```

`/metrics` can be protected independently with `METRICS_AUTH_REQUIRED`.

## Testing

Run unit tests locally:

```bash
python3 -m pip install -r gateway/requirements.txt
python3 -m unittest -q tests/test_gateway_unit.py
```

Run the local stack and smoke tests:

```bash
make up
export API_TOKEN="$(grep '^API_KEYS=' .env | cut -d= -f2- | cut -d: -f2-)"

python3 tests/verify_vm_flow.py
python3 tests/verify_playwright.py
python3 tests/test_execution.py
python3 tests/verify_features.py
```

Windows PowerShell:

```powershell
make up
$env:API_TOKEN = ((Get-Content .env | Where-Object { $_ -like 'API_KEYS=*' } | Select-Object -First 1) -split '=', 2)[1]
$env:API_TOKEN = ($env:API_TOKEN -split ':', 2)[1]

python tests\verify_vm_flow.py
python tests\verify_playwright.py
python tests\test_execution.py
python tests\verify_features.py
```

Optional outbound network tests are skipped unless explicitly enabled:

```bash
RUN_SANDBOX_NETWORK_TESTS=true python3 tests/test_execution.py
```

PowerShell:

```powershell
$env:RUN_SANDBOX_NETWORK_TESTS = "true"
python tests\test_execution.py
```

CI validates Docker Compose config, compiles Python sources, runs unit tests, validates version metadata, runs Bandit and pip-audit, builds the local stack, runs integration checks, and scans the gateway and sandbox images with Trivy.

## Operations

Recommended production deployment pattern:

1. Build and publish immutable gateway and sandbox images.
2. Run the gateway behind TLS infrastructure.
3. Run Redis as a managed or persistent service.
4. Run sandbox containers on dedicated worker hosts or a dedicated remote Docker daemon.
5. For public beta, configure gVisor/runsc, Kata Containers, or an equivalent stronger runtime and set `PUBLIC_BETA_MODE=true`.
6. Keep Docker daemon credentials and API keys out of source control.
7. Monitor request rates, execution latency, error rates, `429` responses, active executions, active sessions, session expirations, Redis health, Docker daemon health, container restarts, memory, CPU, and disk pressure.
8. Rotate API keys and JWT secrets on a schedule.
9. Keep base images, Python dependencies, Docker, runtimes, and host kernels patched.

Development notes:

- Use `make build` after changing `gateway/Dockerfile`, `sandbox/Dockerfile`, requirements, or executor behavior.
- Use `make restart` after changing Compose-level configuration.
- Keep generated files under `/tmp/output` in sandboxed code when they need to be returned to the caller.
- Keep persistent per-session files under `/home/sandbox`.
- Do not put host secrets in `.env_sandbox` unless the workflow explicitly requires them and the submitted code is trusted.

## License

This app is licensed under the Apache License 2.0. See `LICENSE`.