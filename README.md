# Code Execution Service

A secure, scalable Docker-based service for executing Python code from LLM models. Each execution runs in an isolated, ephemeral container with strict security controls.

## Quick Start

```bash
# 1. Build both images
docker compose build
docker compose build --profile build   # builds sandbox image

# 2. Start the gateway
docker compose up -d

# 3. Test it
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "print(\"Hello from sandbox!\")"}'

# 4. Run full test suite
python3 test_execution.py
```

## API

### `POST /execute`

Execute Python code in a sandbox container.

**Request:**
```json
{
  "code": "import matplotlib.pyplot as plt\nplt.plot([1,2,3])\nplt.show()",
  "timeout": 30,
  "enable_network": true
}
```

**Response:**
```json
{
  "execution_id": "a1b2c3d4e5f6",
  "stdout": "",
  "stderr": "",
  "error": null,
  "error_type": null,
  "files": [
    {
      "name": "figure_1.png",
      "content": "<base64-encoded PNG>",
      "mime_type": "image/png",
      "size": 45231
    }
  ],
  "execution_time": 1.234,
  "timed_out": false
}
```

### `GET /health`

Health check — returns service status, Docker connectivity, and metrics.

### `GET /metrics`

Execution metrics — total, successful, failed, timed out, active counts.

## Architecture

```
LLM → POST /execute → [Gateway] → creates → [Sandbox Container]
                                                 ↓
                                            Runs Python code
                                                 ↓
                                       Returns JSON + base64 files
                                                 ↓
                                     Container auto-destroyed
```

- **Gateway**: Persistent FastAPI service managing sandbox lifecycle
- **Sandbox**: Ephemeral Python container, destroyed after each execution

## Security

| Layer | Measure |
|---|---|
| Container | Ephemeral, auto-removed |
| User | Non-root `sandbox` user |
| Filesystem | Read-only + size-limited tmpfs |
| Resources | 512MB RAM, 1 CPU, 64 PIDs |
| Syscalls | Custom seccomp profile |
| Capabilities | All dropped |
| Timeout | Configurable hard kill |

## Configuration

Copy `.env.example` to `.env` and adjust:

| Variable | Default | Description |
|---|---|---|
| `MAX_CONCURRENT_EXECUTIONS` | 10 | Max parallel sandbox containers |
| `DEFAULT_TIMEOUT` | 30 | Default execution timeout (seconds) |
| `SANDBOX_MEM_LIMIT` | 512m | Memory limit per container |
| `SANDBOX_CPU_QUOTA` | 100000 | CPU quota (100000 = 1 core) |
| `SANDBOX_NETWORK_MODE` | bridge | `bridge` (internet) or `none` |

## Pre-installed Packages

numpy, pandas, matplotlib, seaborn, scipy, scikit-learn, sympy, Pillow, requests, openpyxl, pyyaml

## Scaling

- Run multiple gateway instances behind a load balancer
- Each gateway manages its own sandbox pool
- Gateway is fully stateless
- Sandbox images are cached — container spin-up is ~200ms

## File Output

Code can generate files by:
1. Using `plt.show()` — automatically saved as PNG
2. Writing files to `/tmp/output/` — returned as base64

All output files are included in the response as base64-encoded content with MIME type detection.
