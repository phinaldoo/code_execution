# Code Execution Gateway

Code Execution Gateway is a sub-service of the ChatUI project. It is a Docker deployment for running untrusted Python and Bash code in isolated sandbox sessions.

The project provides a FastAPI gateway that creates and manages sandbox containers, executes submitted code inside those containers, and returns captured stdout, stderr, errors, and generated files as JSON. It is built for LLM and chat UI workflows where a frontend needs a controlled execution backend for data analysis, plotting, browser automation, and short-lived file processing tasks.

## Important Security Warning

This service uses Docker containers as the sandbox boundary. It does not create a dedicated virtual machine for each sandbox session.

On Linux hosts, sandbox containers share the host kernel. On macOS and Windows Docker Desktop, the containers usually run inside Docker Desktop's hidden Linux VM, but this project still manages Docker containers, not VMs. Treat this as container isolation with hardening, not VM-grade isolation.

Do not expose this service to arbitrary hostile users unless you add stronger isolation and operational controls. For high-risk workloads, run workers on dedicated disposable hosts or use a stronger boundary such as microVMs, VMs, gVisor, or Kata Containers. Protect Docker daemon access carefully; access to the Docker socket or an overly permissive Docker API proxy can be equivalent to host-level control.

## What It Does

- Creates authenticated sandbox sessions through `POST /containers`.
- Executes Python or Bash in an existing session through `POST /execute`.
- Preserves `/home/sandbox` state across executions in the same session.
- Captures stdout, stderr, structured errors, execution time, and generated files.
- Auto-saves Matplotlib figures when Python code calls `plt.show()`.
- Supports input files uploaded as base64 and output files written to `/tmp/output`.
- Can run Playwright Chromium inside the sandbox image.
- Enforces memory, CPU, PID, timeout, rate, and concurrency limits.
- Uses Redis for shared session state and distributed locks in the Compose stack.
- Exposes version metadata, health checks, and Prometheus metrics.

## Architecture

```text
client
  |
  | HTTP + Bearer token
  v
FastAPI gateway
  |
  | Docker API through restricted docker-socket-proxy
  v
sandbox container session
  |
  | docker exec python /usr/local/bin/executor.py
  v
executor captures output and files
```

Main components:

- `gateway/app.py` - FastAPI app, auth, rate limits, Docker container lifecycle, execution orchestration, health, and metrics.
- `gateway/state.py` - in-memory and Redis state backends for sessions, locks, and rate limits.
- `sandbox/executor.py` - code runner inside each sandbox container.
- `docker-compose.yml` - local development stack with gateway, Redis, docker socket proxy, and sandbox image build target.
- `tests/` - unit, smoke, and integration verification scripts.
- `security/seccomp-profile.json` - optional fallback seccomp allowlist profile. The default runtime path uses Docker's default seccomp policy.

## Requirements

- Docker with Docker Compose v2.
- Python 3.12+ for local test scripts.
- `openssl` and `python3` for `setup.sh`.

The gateway and sandbox images are built by Docker. Local Python only needs to run tests and helper scripts.

## Quick Start

Create local configuration and build the images:

```bash
make setup
make build
```

Start the local stack:

```bash
make up
```

Check status:

```bash
make ps
make logs
```

Stop the stack:

```bash
make down
```

`make setup` creates `.env` from `.env.example` if needed and generates a local `API_KEYS` secret when one is missing.

## Authentication

Most endpoints require a Bearer token when `REQUIRE_AUTH=true`, which is the default in `.env.example`.

Static API keys are configured with `API_KEYS`:

```env
API_KEYS=local:replace-with-a-long-random-secret
```

Use the secret portion as the bearer token:

```bash
TOKEN="replace-with-a-long-random-secret"
```

The gateway also supports JWT auth when `JWT_SECRET` is configured. JWTs must include `exp` and `sub`; optional issuer, audience, algorithms, and tenant claim are controlled by environment variables.

## API Usage

Set a token first:

```bash
TOKEN="$(grep '^API_KEYS=' .env | cut -d= -f2- | cut -d: -f2-)"
```

Check health:

```bash
curl -sS http://localhost:8000/healthz
```

Check the running gateway and execution contract version:

```bash
curl -sS http://localhost:8000/version
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

## Request And Response Shape

Create container:

```json
{
  "enable_network": false,
  "inject_sandbox_env": false
}
```

Execute code:

```json
{
  "container_id": "container-id",
  "language": "python",
  "code": "print('hello')",
  "timeout": 30,
  "pip_packages": [],
  "files": [
    {
      "name": "input.txt",
      "content": "base64-encoded-file-content"
    }
  ]
}
```

Execution response:

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

Output files must be written under `/tmp/output` inside the sandbox. Returned file contents are base64 encoded.

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

## Security Model

This service is designed for untrusted code, but it is still a powerful execution system. Run it with the same care as any sandbox infrastructure.

The sandbox is a hardened Docker container, not a VM. Containers provide process, filesystem, namespace, cgroup, and capability isolation, but they share the host kernel on Linux. A kernel vulnerability, container runtime vulnerability, Docker daemon exposure, unsafe host mount, privileged configuration, or overly broad network access can break the intended isolation boundary.

Implemented controls include:

- Non-root gateway and sandbox users.
- Docker capability drop with `no-new-privileges`.
- Configurable CPU, memory, PID, shared memory, and tmpfs limits.
- Optional network isolation with `SANDBOX_NETWORK_MODE=none`.
- Per-execution timeout with container teardown on timeout.
- Per-container execution locks to prevent concurrent mutation of one session.
- Global execution concurrency limits.
- Per-principal request and container creation rate limits.
- Maximum active session limits globally and per principal.
- Base64 input file validation, duplicate name rejection, path traversal prevention, and file size limits.
- Session ownership checks by authenticated principal and tenant.
- Redis-backed shared state for multi-replica coordination.
- Prometheus metrics and health endpoints for operations.

Production configuration is intentionally stricter than local development. In `APP_ENV=production`, the gateway requires authentication, shared state, explicit CORS origins, and a remote Docker daemon over TLS or SSH. It rejects raw Unix socket access, local docker proxies, loopback Docker hosts, and plain TCP Docker on port `2375`.

## Configuration

Copy `.env.example` to `.env` with `make setup`, then adjust as needed.

Important settings:

| Variable | Purpose | Default |
| --- | --- | --- |
| `APP_ENV` | `development` or `production` behavior | `development` |
| `GATEWAY_PORT` | Host port for the API | `8000` |
| `API_KEYS` | Comma-separated static keys, optionally `key_id:secret` | empty |
| `REQUIRE_AUTH` | Require Bearer auth for API endpoints | production-aware |
| `METRICS_AUTH_REQUIRED` | Require auth for `/metrics` | production-aware |
| `REDIS_URL` | Shared state backend | `redis://redis:6379/0` |
| `SANDBOX_IMAGE` | Sandbox image name | `code-sandbox:latest` |
| `MAX_REQUEST_BODY_SIZE` | Maximum HTTP request body before JSON parsing | `33554432` |
| `DEFAULT_TIMEOUT` | Default execution timeout in seconds | `30` |
| `MAX_TIMEOUT` | Maximum accepted timeout | `120` |
| `MAX_CONCURRENT_EXECUTIONS` | Gateway-wide execution concurrency | `10` |
| `MAX_ACTIVE_SESSIONS` | Total active session cap | `100` |
| `MAX_CONTAINERS_PER_PRINCIPAL` | Active session cap per authenticated principal | `3` |
| `SANDBOX_NETWORK_MODE` | `none` or `bridge` | `none` |
| `SANDBOX_TMP_ROOT_SIZE` | Bounded tmpfs size for `/tmp` in the sandbox | `512m` |
| `SANDBOX_READ_ONLY_ROOTFS` | Run sandbox containers with a read-only root filesystem | `true` |
| `ALLOW_PIP_INSTALLS` | Allow per-request pip installs | `false` |
| `ALLOW_SANDBOX_ENV_INJECTION` | Allow copying `.env_sandbox` into sessions on request | `false` |
| `GATEWAY_DOCKER_HOST` | Docker daemon endpoint passed to the gateway as `DOCKER_HOST` | `tcp://docker-proxy:2375` locally |
| `USE_DOCKER_DEFAULT_SECCOMP` | Use Docker runtime default seccomp | `true` |
| `REDIS_SOCKET_CONNECT_TIMEOUT` | Redis connect timeout in seconds | `5` |
| `REDIS_SOCKET_TIMEOUT` | Redis socket timeout in seconds | `5` |

Network access is fixed when the container session is created. A later `/execute` request cannot safely downgrade an already-networked session.

Sandbox containers use a read-only root filesystem by default. Writable state is limited to bounded tmpfs mounts, primarily `/home/sandbox` for per-session files and `/tmp` for execution scratch space, output collection, and library caches.

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

When enabled, clients may send up to `MAX_PIP_PACKAGES` package specifiers in `pip_packages`. Package names are validated, installed with `pip install --user --no-cache-dir --quiet`, and counted within the execution timeout.

For untrusted workloads, prefer baking required packages into the sandbox image instead of allowing runtime installs.

## Health And Metrics

Version metadata:

```text
GET /version
```

The version endpoint returns the gateway release version, tag, API contract version, active execution version, supported execution versions, and feature flags. Normal responses also include `X-Code-Execution-Version` and `X-Code-Execution-Version-Tag` headers.

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

Optional network tests are skipped unless explicitly enabled:

```bash
RUN_SANDBOX_NETWORK_TESTS=true python3 tests/test_execution.py
```

CI validates Docker Compose config, compiles Python sources, runs unit tests, runs Bandit and pip-audit, builds the local stack, runs integration checks, and scans the gateway and sandbox images with Trivy.

## Development Notes

- Use `make build` after changing `gateway/Dockerfile`, `sandbox/Dockerfile`, requirements, or executor behavior.
- Use `make restart` after changing Compose-level configuration.
- Keep generated files under `/tmp/output` in sandboxed code when they need to be returned to the caller.
- Keep persistent per-session files under `/home/sandbox`.
- Do not put host secrets in `.env_sandbox` unless the workflow explicitly requires them and the submitted code is trusted.

## License

This app is licensed under the Apache License 2.0. See `LICENSE`.
