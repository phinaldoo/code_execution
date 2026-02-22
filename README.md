# Code Execution Service (Mini-VM)

A secure, scalable Docker-based service for executing code in persistent "mini-VM" sessions. This version introduces stateful container sessions that survive between multiple execution calls, supporting both Python and Bash with advanced file management.

## Quick Start

```bash
# 1. Build images
docker compose build

# 2. Start the gateway
docker compose up -d

# 3. Create a container session
curl -X POST http://localhost:8000/containers -d '{}'
# Returns: {"container_id": "...", "status": "active", ...}

# 4. Execute code in that session
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{
    "container_id": "YOUR_CONTAINER_ID",
    "language": "python",
    "code": "with open(\"data.txt\", \"w\") as f: f.write(\"Hello VM!\")"
  }'

# 5. Verify persistence in Bash
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{
    "container_id": "YOUR_CONTAINER_ID",
    "language": "bash",
    "code": "cat data.txt"
  }'
```

## Key Features

- **Stateful Sessions**: Containers stay alive for **20 minutes after the latest activity**. Subsequent requests to the same `container_id` share the same filesystem (`/home/sandbox`).
- **Multi-language**: Native support for **Python** and **Bash**.
- **File Injection**: Upload input files directly to the VM session in the `/execute` request.
- **Auto-Retrieval**: Any files written to `/tmp/output/` are automatically returned as base64-encoded strings in the API response and cleared from the container.
- **Highly Secure**: Strict resource limits, non-root users, and isolated execution via `exec_run`.

## API Reference

### `POST /containers`
Create a new long-running container session.
- **Body**: `{"enable_network": true}`
- **Response**: Returns `container_id`.

### `GET /containers/{id}`
Check session status, uptime, and last activity.

### `DELETE /containers/{id}`
Manually terminate and remove a container session.

### `POST /execute`
Execute code inside an active session.

**Request Body:**
```json
{
  "container_id": "string",
  "language": "python" | "bash",
  "code": "string",
  "timeout": 30,
  "pip_packages": ["list", "of", "packages"],
  "files": [
    {
      "name": "input.json",
      "content": "<base64_encoded_content>"
    }
  ]
}
```

**Response Body:**
```json
{
  "execution_id": "string",
  "stdout": "string",
  "stderr": "string",
  "error": "string | null",
  "files": [
    {
      "name": "result.png",
      "content": "<base64_encoded_content>",
      "mime_type": "image/png",
      "size": 1234
    }
  ],
  "execution_time": 0.45,
  "timed_out": false
}
```

## Security & Isolation

| Feature | Implementation |
|---|---|
| **Session Lifecycle** | 20-min inactivity timeout (auto-cleanup) |
| **Isolation** | Shared-kernel container isolation |
| **User** | Non-root `sandbox` user |
| **Resource Limits** | 512MB RAM, 1 CPU, 64 PIDs |
| **Filesystem** | Persistent `/home/sandbox`, Ephemeral `/tmp/output` |
| **Network** | Configurable per container session |

## Advanced Usage

### File Outputs
Any file you want returned by the API must be saved to `/tmp/output/`.
- In Python: `plt.savefig('/tmp/output/plot.png')` (or just `plt.show()` which is auto-patched).
- In Bash: `echo "Done" > /tmp/output/status.txt`.

### Input Files
You can seed your VM session with files by providing them in the `files` array of the `/execute` request. They will be placed in the current working directory (`/home/sandbox`).

## Configuration
Configuration is managed via environment variables in `docker-compose.yml` or a `.env` file.
- `SESSION_TIMEOUT_SECONDS`: Default 1200 (20 minutes).
- `SANDBOX_MEM_LIMIT`: Memory allocated per VM.
- `API_KEY`: Set to enable Bearer token authentication.
