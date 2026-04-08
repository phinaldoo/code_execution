#!/usr/bin/env python3
"""
Integration tests for the Code Execution Service.

Usage:
    python3 test_execution.py [--url http://localhost:8000]
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request


BASE_URL = "http://localhost:8000"

GREEN = "\033[92m"
RED = "\033[91m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"


def resolve_token() -> str | None:
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


def request(method: str, path: str, payload: dict | None = None, timeout: int = 90) -> tuple[int, dict]:
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
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body) if body else {}


def execute(code: str, timeout: int = 30, enable_network: bool = True, language: str = "python") -> dict:
    status, container = request("POST", "/containers", {"enable_network": enable_network}, timeout=30)
    if status != 200:
        return {"error": f"Failed to create container: {container}", "error_type": "SetupError"}

    container_id = container["container_id"]
    try:
        status, result = request(
            "POST",
            "/execute",
            {
                "container_id": container_id,
                "language": language,
                "code": code,
                "timeout": timeout,
                "enable_network": enable_network,
            },
            timeout=timeout + 30,
        )
        if status != 200:
            return {"error": f"HTTP {status}: {result}", "error_type": "HTTPError"}
        return result
    finally:
        request("DELETE", f"/containers/{container_id}", timeout=30)


def test_health():
    status, data = request("GET", "/healthz", timeout=10)
    assert status == 200, data
    assert data["status"] == "healthy", f"Service unhealthy: {data}"
    return True, "Health check passed"


def test_health_details():
    status, data = request("GET", "/healthz/details", timeout=10)
    assert status == 200, data
    assert data["status"] == "healthy", f"Detailed health check failed: {data}"
    assert "docker_connected" in data, data
    assert "state_backend_healthy" in data, data
    return True, "Detailed health endpoint passed"


def test_simple_print():
    result = execute('print("Hello, World!")')
    assert result.get("stdout", "").strip() == "Hello, World!", f"Unexpected stdout: {result}"
    assert result.get("error") is None, f"Unexpected error: {result.get('error')}"
    return True, f"stdout='{result['stdout'].strip()}', time={result.get('execution_time', '?')}s"


def test_math():
    code = """
import math
result = math.factorial(20)
print(f"20! = {result}")
"""
    result = execute(code)
    assert "2432902008176640000" in result.get("stdout", ""), f"Wrong result: {result}"
    assert result.get("error") is None
    return True, f"Computed 20! correctly, time={result.get('execution_time', '?')}s"


def test_matplotlib_plot():
    code = """
import matplotlib.pyplot as plt
import numpy as np

x = np.linspace(0, 2 * np.pi, 100)
y = np.sin(x)

plt.figure(figsize=(8, 5))
plt.plot(x, y, 'b-', linewidth=2)
plt.title('Sine Wave')
plt.xlabel('x')
plt.ylabel('sin(x)')
plt.grid(True)
plt.show()
"""
    result = execute(code)
    assert result.get("error") is None, f"Error: {result.get('error')}"
    files = result.get("files", [])
    assert files, f"Expected plot output, got {result}"
    decoded = base64.b64decode(files[0]["content"])
    assert decoded[:8] == b"\x89PNG\r\n\x1a\n", "Not a valid PNG file"
    return True, f"Got {len(files)} PNG file(s), size={files[0]['size']} bytes"


def test_syntax_error():
    result = execute("def foo(\n  broken")
    assert result.get("error") is not None, "Expected an error"
    assert "SyntaxError" in (result.get("error_type", "") or result.get("error", ""))
    return True, f"Caught: {result.get('error_type', 'unknown')}"


def test_runtime_error():
    result = execute("x = 1 / 0")
    assert result.get("error") is not None, "Expected an error"
    assert "ZeroDivision" in (result.get("error_type", "") or result.get("error", ""))
    return True, f"Caught: {result.get('error_type', 'unknown')}"


def test_timeout():
    code = """
import time
time.sleep(60)
print("Should not reach here")
"""
    result = execute(code, timeout=5)
    assert result.get("timed_out") is True or "timeout" in (result.get("error", "") or "").lower(), result
    return True, f"Timeout enforced, error={result.get('error_type', 'TimeoutError')}"


def test_multiple_files():
    code = """
import matplotlib.pyplot as plt
import numpy as np

plt.figure()
plt.bar(['A', 'B', 'C'], [3, 7, 2])
plt.title('Bar Chart')
plt.show()

plt.figure()
plt.scatter(np.random.rand(20), np.random.rand(20))
plt.title('Scatter Plot')
plt.show()

with open('/tmp/output/data.csv', 'w', encoding='utf-8') as handle:
    handle.write('name,value\\n')
    handle.write('A,3\\n')
    handle.write('B,7\\n')
    handle.write('C,2\\n')

print("Generated 2 plots and 1 CSV")
"""
    result = execute(code)
    assert result.get("error") is None, f"Error: {result.get('error')}"
    file_names = [file["name"] for file in result.get("files", [])]
    assert "data.csv" in file_names, f"Missing data.csv, got: {file_names}"
    assert len(file_names) >= 3, f"Expected 3+ files, got {file_names}"
    return True, f"Got {len(file_names)} files: {file_names}"


def test_pandas_numpy():
    code = """
import pandas as pd
import numpy as np

df = pd.DataFrame({
    'x': np.arange(10),
    'y': np.random.randn(10),
})
print(df.describe().to_string())
print(f"Shape: {df.shape}")
"""
    result = execute(code)
    assert result.get("error") is None, f"Error: {result.get('error')}"
    assert "Shape: (10, 2)" in result.get("stdout", "")
    return True, "pandas + numpy work"


def test_network_access():
    code = """
import requests
resp = requests.get("https://httpbin.org/get", timeout=10)
print(f"Status: {resp.status_code}")
"""
    result = execute(code, enable_network=True)
    assert result.get("error") is None, f"Error: {result.get('error')}"
    assert "Status: 200" in result.get("stdout", "")
    return True, "Network access works"


def test_background_process_cleanup():
    status, container = request("POST", "/containers", {"enable_network": False}, timeout=30)
    assert status == 200, container

    container_id = container["container_id"]
    try:
        status, result = request(
            "POST",
            "/execute",
            {
                "container_id": container_id,
                "language": "bash",
                "code": "sleep 77 >/dev/null 2>&1 &\necho started",
                "timeout": 10,
            },
            timeout=40,
        )
        assert status == 200, result
        assert result.get("error") is None, result

        status, result = request(
            "POST",
            "/execute",
            {
                "container_id": container_id,
                "language": "bash",
                "code": (
                    "for entry in /proc/[0-9]*/cmdline; do\n"
                    "  cmd=$(tr '\\0' ' ' < \"$entry\" 2>/dev/null || true)\n"
                    "  case \"$cmd\" in\n"
                    "    *\"sleep 77\"*) echo leaked; exit 1 ;;\n"
                    "  esac\n"
                    "done\n"
                    "echo clean\n"
                ),
                "timeout": 10,
            },
            timeout=40,
        )
        assert status == 200, result
        assert result.get("error") is None, result
        assert result.get("stdout", "").strip() == "clean", result
        return True, "Residual processes are cleaned between executions"
    finally:
        request("DELETE", f"/containers/{container_id}", timeout=30)


TESTS = [
    ("Health Check", test_health),
    ("Health Details", test_health_details),
    ("Simple Print", test_simple_print),
    ("Math Computation", test_math),
    ("Matplotlib Plot", test_matplotlib_plot),
    ("Syntax Error Handling", test_syntax_error),
    ("Runtime Error Handling", test_runtime_error),
    ("Timeout Enforcement", test_timeout),
    ("Multiple File Output", test_multiple_files),
    ("Pandas + NumPy", test_pandas_numpy),
    ("Network Access", test_network_access),
    ("Residual Process Cleanup", test_background_process_cleanup),
]


def main():
    parser = argparse.ArgumentParser(description="Test the Code Execution Service")
    parser.add_argument("--url", default="http://localhost:8000", help="Service URL")
    args = parser.parse_args()

    global BASE_URL
    BASE_URL = args.url.rstrip("/")

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  Code Execution Service — Integration Tests{RESET}")
    print(f"{BOLD}  Target: {BASE_URL}{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")

    passed = 0
    failed = 0
    errors = []

    for name, test_fn in TESTS:
        print(f"  {CYAN}▸{RESET} {name}...", end=" ", flush=True)
        start = time.time()
        try:
            success, detail = test_fn()
            elapsed = time.time() - start
            if success:
                print(f"{GREEN}✓{RESET} ({elapsed:.1f}s) {detail}")
                passed += 1
            else:
                print(f"{RED}✗{RESET} ({elapsed:.1f}s) {detail}")
                failed += 1
                errors.append((name, detail))
        except Exception as exc:
            elapsed = time.time() - start
            print(f"{RED}✗{RESET} ({elapsed:.1f}s) {type(exc).__name__}: {exc}")
            failed += 1
            errors.append((name, str(exc)))

    print(f"\n{BOLD}{'-' * 60}{RESET}")
    print(f"  Results: {GREEN}{passed} passed{RESET}, {RED}{failed} failed{RESET}, {passed + failed} total")

    if errors:
        print(f"\n  {RED}Failures:{RESET}")
        for name, detail in errors:
            print(f"    - {name}: {detail}")

    print(f"{BOLD}{'-' * 60}{RESET}\n")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
