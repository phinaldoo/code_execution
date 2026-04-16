#!/usr/bin/env python3
import base64
import json
import os
import urllib.error
import urllib.request


BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")


def resolve_token() -> str | None:
    """Resolve API token from environment variables."""
    for env_name in ("API_TOKEN", "API_KEY"):
        value = os.getenv(env_name)
        if value:
            return value

    api_keys = os.getenv("API_KEYS", "")
    if not api_keys:
        return None

    first = api_keys.split(",", 1)[0].strip()
    if ":" in first:
        return first.split(":", 1)[1]
    return first or None


TOKEN = resolve_token()


def request(method: str, path: str, payload: dict | None = None, timeout: int = 60):
    """Make HTTP request to the gateway API."""
    data = None
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body) if body else {}


def main():
    """Verify VM flow including health, container creation, and code execution."""
    print("1. Checking health...")
    status, payload = request("GET", "/healthz")
    assert status == 200, payload
    assert payload["status"] == "healthy", payload
    print("   Health OK")

    print("2. Creating container session...")
    status, created = request("POST", "/containers", {"enable_network": True})
    assert status == 200, created
    container_id = created["container_id"]
    print(f"   Container: {container_id}")

    try:
        print("3. Executing Python code...")
        python_code = (
            "print('Hello from persistent Python session!')\n"
            "with open('/home/sandbox/test.txt', 'w', encoding='utf-8') as handle:\n"
            "    handle.write('Python was here')\n"
        )
        status, result = request(
            "POST",
            "/execute",
            {
                "container_id": container_id,
                "language": "python",
                "code": python_code,
            },
        )
        assert status == 200, result
        assert "Hello from persistent Python session!" in result["stdout"], result
        print("   Python execution OK")

        print("4. Executing Bash code...")
        bash_code = "echo 'Hello from Bash!'\ncat /home/sandbox/test.txt"
        status, result = request(
            "POST",
            "/execute",
            {
                "container_id": container_id,
                "language": "bash",
                "code": bash_code,
            },
        )
        assert status == 200, result
        assert "Python was here" in result["stdout"], result
        print("   Bash execution OK")

        print("5. Testing input/output file flow...")
        input_b64 = base64.b64encode(b"This is a secret message uploaded from host.").decode("ascii")
        script = (
            "cat /home/sandbox/input.txt\n"
            "echo \"Processed: $(cat /home/sandbox/input.txt)\" > /tmp/output/result.txt\n"
        )
        status, result = request(
            "POST",
            "/execute",
            {
                "container_id": container_id,
                "language": "bash",
                "code": script,
                "files": [{"name": "input.txt", "content": input_b64}],
            },
        )
        assert status == 200, result
        assert result["files"], result
        decoded = base64.b64decode(result["files"][0]["content"]).decode("utf-8")
        assert "Processed:" in decoded, result
        print("   File flow OK")
    finally:
        print("6. Deleting container session...")
        status, result = request("DELETE", f"/containers/{container_id}")
        assert status == 200, result
        print("   Cleanup OK")


if __name__ == "__main__":
    main()
