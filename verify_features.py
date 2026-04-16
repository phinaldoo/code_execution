#!/usr/bin/env python3
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


def request(method: str, path: str, payload: dict | None = None, timeout: int = 90):
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def execute(code: str, *, pip_packages: list[str] | None = None):
    """Execute code in a container session and return result."""
    status, container = request("POST", "/containers", {"enable_network": True})
    assert status == 200, container
    container_id = container["container_id"]

    try:
        return request(
            "POST",
            "/execute",
            {
                "container_id": container_id,
                "language": "python",
                "code": code,
                "pip_packages": pip_packages or [],
            },
        )
    finally:
        request("DELETE", f"/containers/{container_id}")


def test_no_auth():
    """Test that requests without authentication are rejected."""
    print("Testing request without API key...")
    token = TOKEN
    if not token:
        print("  Skipping because no bearer token is configured for this environment.")
        return

    req = urllib.request.Request(
        f"{BASE_URL}/containers",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        print("  Expected 401 but request succeeded")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print("  Correctly rejected with 401")
        else:
            print(f"  Unexpected response: HTTP {exc.code}")


def test_good_auth():
    """Test that authenticated requests succeed."""
    print("Testing authenticated execution...")
    status, result = execute("print('hello')")
    assert status == 200, result
    assert result["stdout"].strip() == "hello", result
    print("  Authorized execution succeeded")


def test_pip_packages():
    """Test dynamic pip package installation."""
    print("Testing dynamic pip package installation...")
    code = "import cowsay; print(cowsay.get_output_string('cow', 'Moo!'))"
    status, result = execute(code, pip_packages=["cowsay"])
    assert status == 200, result
    assert "Moo!" in result["stdout"], result
    print("  Pip package install succeeded")


if __name__ == "__main__":
    print("--- Verification of Security and Package Features ---")
    test_no_auth()
    test_good_auth()
    test_pip_packages()
