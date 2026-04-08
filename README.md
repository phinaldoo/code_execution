# Code Execution Gateway

Hardened Docker-based code execution for untrusted Python and Bash workloads.

## What Changed

- Auth is mandatory by default in production and supports rotated static bearer keys or expiring JWTs.
- CORS is explicit-origin only; wildcard credentials are rejected at startup.
- Sandbox sessions now run with a read-only root filesystem, `no-new-privileges`, dropped capabilities, fixed UID/GID, and writable tmpfs mounts only where execution needs them.
- Shared session ownership and rate limiting are backed by Redis, and each session is bound to a Docker daemon identity so the wrong replica cannot delete or hijack it.
- Container creation now passes through a shared guard before quota checks, so per-principal and global session caps remain race-safe under concurrent load.
- Local development can use an explicit `local-docker` compose profile, while production must target a dedicated remote Docker daemon instead of the host socket.
- `.env_sandbox` injection is disabled by default and must be explicitly requested per container session.
- Timeout enforcement no longer blocks the event loop. Timed-out executions now deterministically tear down the affected container.
- The executor now kills stray processes after each run, so a returned request cannot leave background workloads running inside the session.
- Prometheus metrics are exported on `/metrics`, with a legacy JSON view on `/metrics/json`.
- CI now validates compose, builds both images, runs security scans, and executes runtime smoke tests.

## Quick Start

1. Run `make setup` to create `.env` and generate a local `API_KEYS` secret.
2. Update `CORS_ALLOW_ORIGINS` in `.env` to the exact browser origins that should call the gateway.
3. Build the images with `docker compose --profile local-docker build`.
4. Start the stack with `docker compose --profile local-docker up -d`.
5. Verify health with:

```bash
curl http://localhost:8000/healthz
```

If you need diagnostic readiness details, call `/healthz/details` with bearer auth. If you use `API_KEYS=kid:secret`, the bearer token value is the `secret` portion.

To create a sandbox that can see values from `.env_sandbox`, first set `ALLOW_SANDBOX_ENV_INJECTION=true` on the gateway, then call `POST /containers` with:

```json
{
  "enable_network": true,
  "inject_sandbox_env": true
}
```

## Production Configuration

- `API_KEYS`: Comma-separated `kid:secret` entries for rotated static tokens.
- `JWT_SECRET`, `JWT_ISSUER`, `JWT_AUDIENCE`: Optional JWT validation path for expiring per-user tokens.
- `CORS_ALLOW_ORIGINS`: Comma-separated explicit origins. Required in production when CORS is enabled.
- `DOCKER_HOST`: Must point at a TLS-secured (`tcp://host:2376`) or SSH (`ssh://`) remote Docker daemon in production. Plain TCP on port 2375 is rejected. The local `docker-proxy` profile is for development and CI only.
- `REDIS_URL`: Required shared-state backend for production session ownership and rate limiting.
- Production replicas must either target the same Docker daemon or route follow-up requests back to the original execution node. Cross-node requests are rejected instead of deleting shared state.
- `MAX_ACTIVE_SESSIONS`, `MAX_CONTAINERS_PER_PRINCIPAL`, `CONTAINER_RATE_LIMIT_REQUESTS_PER_WINDOW`: Capacity controls for sandbox creation.
- `CONTAINER_CREATE_GUARD_TIMEOUT`: How long the API waits to enter the shared creation guard before returning a retryable error.
- `SANDBOX_NETWORK_MODE=none`: Network egress is disabled by default. Set to `bridge` only for workloads that require internet access.
- `ALLOW_PIP_INSTALLS=false`: Runtime pip installs are disabled by default. Enable only for trusted workloads.
- `ALLOW_SANDBOX_ENV_INJECTION=false`: Keeps `.env_sandbox` out of untrusted sandboxes unless explicitly enabled.
- `USE_DOCKER_DEFAULT_SECCOMP=true`: Uses Docker's hardened RuntimeDefault seccomp profile.
- `SECCOMP_PROFILE_DAEMON_PATH`: Required only when `USE_DOCKER_DEFAULT_SECCOMP=false`. This must point to a seccomp profile file on the Docker daemon host, not a path inside the gateway container. The checked-in profile under `security/seccomp-profile.json` is a source artifact you can copy to that host path.
- `METRICS_AUTH_REQUIRED=true`: Protects `/metrics` behind bearer auth in production.

## Operational Endpoints

- `/health` and `/healthz`: Minimal liveness/readiness summary intended for load balancers and compose health checks.
- `/health/details` and `/healthz/details`: Authenticated diagnostics with dependency and configuration status.
- `/metrics`: Prometheus scrape endpoint.
- `/metrics/json`: Lightweight JSON counters.

## Verification

- `python3 verify_vm_flow.py`
- `python3 verify_playwright.py`
- `python3 verify_features.py`
- `python3 test_execution.py`
- `python3 -m unittest -q test_gateway_unit.py`

Each script reads `API_TOKEN`, `API_KEY`, or the first secret from `API_KEYS`.

## Supply Chain Notes

- Both Dockerfiles are pinned to an explicit Python base image digest.
- Python requirements are exact-version pinned and the images no longer self-upgrade `pip` during build.
- Full `--require-hashes` lockfiles are still not included; add them if your release process requires offline artifact verification.

## License

Apache 2.0 License
