# API Reference: Code Execution Service (Mini-VM)

A secure, scalable Docker-based service for executing code in persistent "mini-VM" sessions. This service supports stateful container sessions, multi-language execution (Python, Bash), and advanced file management.

---

## Architecture Overview

The system consists of two primary components:
1.  **Gateway (FastAPI)**: Manages the lifecycle of sandbox containers, handles API requests, enforces security policies, and orchestrates code execution.
2.  **Sandbox (Docker)**: Hardened containers where code is actually executed. Each session runs in its own isolated container.

### Session Management
- **Stateful Sessions**: Containers persist between requests. Use the `container_id` returned from `/containers` in subsequent `/execute` calls.
- **Inactivity Timeout**: Containers are automatically destroyed after **20 minutes** of inactivity (`SESSION_TIMEOUT_SECONDS`).
- **Persistence**: Files saved in `/home/sandbox` are persistent for the life of the session.

---

## Authentication

If configured, the service requires a Bearer Token for all endpoints.

**Header:**
`Authorization: Bearer <YOUR_API_KEY>`

---

## Endpoints

### 1. Create Container
`POST /containers`

Initialize a new persistent VM session.

**Request Body:**
```json
{
  "enable_network": true
}
```
- `enable_network` (bool, optional): If `false`, the container will have no internet access. Default `true`.

**Response:**
```json
{
  "container_id": "...",
  "status": "active",
  "uptime_seconds": 0.0,
  "last_activity": 1700000000.0
}
```

---

### 2. Get Container Status
`GET /containers/{id}`

Check if a session is still active and get its metadata.

**Response:**
Returns a `ContainerResponse` object (same as Create Container).

---

### 3. Delete Container
`DELETE /containers/{id}`

Immediately terminate and remove a container session.

**Response:**
```json
{
  "status": "success",
  "message": "Container <id> removed."
}
```

---

### 4. Execute Code
`POST /execute`

Runs code inside an existing session.

**Request Body:**
```json
{
  "container_id": "string",
  "language": "python" | "bash",
  "code": "string",
  "timeout": 30,
  "pip_packages": ["pandas", "requests"],
  "files": [
    {
      "name": "data.csv",
      "content": "<base64_encoded_content>"
    }
  ]
}
```
- `language`: Supports `python` or `bash`.
- `timeout` (int, optional): Max execution time in seconds (Default: 30, Max: 120).
- `pip_packages` (list, optional): Packages to install *dynamically* via `pip` before running Python code.
- `files` (list, optional): Files to seed into `/home/sandbox` before execution.

**Response:**
```json
{
  "execution_id": "string",
  "stdout": "string",
  "stderr": "string",
  "error": "string | null",
  "error_type": "string | null",
  "files": [
    {
      "name": "plot.png",
      "content": "<base64_encoded_content>",
      "mime_type": "image/png",
      "size": 1234
    }
  ],
  "execution_time": 0.45,
  "timed_out": false
}
```

---

## Features

### File Handling
- **Input Files**: Passed in the `files` array of `/execute`. Placed in `/home/sandbox`.
- **Output Files**: Any file saved to **`/tmp/output/`** inside the container is automatically:
    1. Base64-encoded.
    2. Included in the `files` array of the response.
    3. Deleted from the container to save space.

### Python Auto-Patching
- **Matplotlib**: `plt.show()` is automatically patched. It will save all active figures to `/tmp/output/` as PNGs instead of attempting to open a window.
- **UTF-8**: The environment is forced to UTF-8 to handle special characters (e.g., German umlauts).

### Dynamic Package Installation
You can request specific PyPI packages via the `pip_packages` field. These are installed in the user's site-packages (`--user`) to ensure they are available to the script.

---

## Security & Isolation

| Feature | Implementation |
| :--- | :--- |
| **User** | Runs as non-root `sandbox` user. |
| **RAM Limit** | 512MB (Strict). |
| **CPU Limit** | 1 Core (100% quota). |
| **PIDs Limit** | 64 processes maximum. |
| **Filesystem** | Core OS is Read-Only. `/tmp/output` is a `tmpfs` (memory-based) mount. |
| **Capabilities** | All Linux capabilities dropped (`cap_drop: ["ALL"]`). |
| **Seccomp** | Restricted syscall profile applied. |

---

## Health & Monitoring

### Health Check
`GET /health`
Returns status of the Docker connection and sandbox image availability.

### Metrics
`GET /metrics`
Returns aggregate statistics:
- `total_executions`
- `successful_executions`
- `failed_executions`
- `timed_out_executions`
- `active_executions`
