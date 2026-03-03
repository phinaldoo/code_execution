#!/usr/bin/env python3
"""
Integration tests for the Code Execution Service.

Usage:
    python3 test_execution.py [--url http://localhost:8000]

Tests:
    1. Simple print statement
    2. Math computation
    3. Matplotlib plot generation
    4. Error handling (syntax error)
    5. Error handling (runtime error)
    6. Timeout enforcement
    7. Multiple file output
    8. Network access (requests)
    9. Data science packages (pandas, numpy)
"""

import argparse
import base64
import json
import sys
import time
import urllib.request
import urllib.error


BASE_URL = "http://localhost:8000"

# ANSI colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"


def execute(code: str, timeout: int = 30, enable_network: bool = True) -> dict:
    """Send code to the execution service and return the response."""
    payload = json.dumps({
        "code": code,
        "timeout": timeout,
        "enable_network": enable_network,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{BASE_URL}/execute",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout + 30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        return {"error": f"HTTP {e.code}: {body}", "error_type": "HTTPError"}


def test_health():
    """Test the health endpoint."""
    try:
        req = urllib.request.Request(f"{BASE_URL}/healthz")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["status"] == "healthy", f"Service unhealthy: {data}"
            return True, "Health check passed"
    except Exception as e:
        return False, f"Health check failed: {e}"


def test_simple_print():
    """Test basic print output."""
    result = execute('print("Hello, World!")')
    assert result.get("stdout", "").strip() == "Hello, World!", f"Unexpected stdout: {result}"
    assert result.get("error") is None, f"Unexpected error: {result.get('error')}"
    return True, f"stdout='{result['stdout'].strip()}', time={result.get('execution_time', '?')}s"


def test_math():
    """Test math computation."""
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
    """Test matplotlib plot generation returns a base64 PNG."""
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
    assert len(files) >= 1, f"Expected at least 1 file, got {len(files)}"

    file = files[0]
    assert file["mime_type"] == "image/png", f"Wrong MIME: {file['mime_type']}"
    assert file["content"] is not None, "File content is None"

    # Verify it's valid base64 that decodes to a PNG
    decoded = base64.b64decode(file["content"])
    assert decoded[:8] == b'\x89PNG\r\n\x1a\n', "Not a valid PNG file"

    return True, f"Got {len(files)} PNG file(s), size={file['size']} bytes, time={result.get('execution_time', '?')}s"


def test_syntax_error():
    """Test handling of syntax errors."""
    result = execute("def foo(\n  broken")
    assert result.get("error") is not None, "Expected an error"
    assert "SyntaxError" in (result.get("error_type", "") or result.get("error", ""))
    return True, f"Caught: {result.get('error_type', 'unknown')}"


def test_runtime_error():
    """Test handling of runtime errors."""
    result = execute("x = 1 / 0")
    assert result.get("error") is not None, "Expected an error"
    assert "ZeroDivision" in (result.get("error_type", "") or result.get("error", ""))
    return True, f"Caught: {result.get('error_type', 'unknown')}"


def test_timeout():
    """Test timeout enforcement."""
    code = """
import time
time.sleep(60)
print("Should not reach here")
"""
    result = execute(code, timeout=5)
    assert result.get("timed_out") is True or "timeout" in (result.get("error", "") or "").lower(), \
        f"Expected timeout, got: {result}"
    return True, f"Timeout enforced, error={result.get('error_type', 'TimeoutError')}"


def test_multiple_files():
    """Test generating multiple output files."""
    code = """
import matplotlib.pyplot as plt
import numpy as np

# Plot 1: Bar chart
plt.figure()
plt.bar(['A', 'B', 'C'], [3, 7, 2])
plt.title('Bar Chart')
plt.show()

# Plot 2: Scatter plot
plt.figure()
plt.scatter(np.random.rand(20), np.random.rand(20))
plt.title('Scatter Plot')
plt.show()

# Also write a CSV file
with open('/tmp/output/data.csv', 'w') as f:
    f.write('name,value\\n')
    f.write('A,3\\n')
    f.write('B,7\\n')
    f.write('C,2\\n')

print("Generated 2 plots and 1 CSV")
"""
    result = execute(code)
    assert result.get("error") is None, f"Error: {result.get('error')}"
    files = result.get("files", [])
    assert len(files) >= 3, f"Expected 3+ files, got {len(files)}: {[f['name'] for f in files]}"

    file_names = [f["name"] for f in files]
    assert "data.csv" in file_names, f"Missing data.csv, got: {file_names}"

    return True, f"Got {len(files)} files: {file_names}, time={result.get('execution_time', '?')}s"


def test_pandas_numpy():
    """Test that data science packages work."""
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
    return True, f"pandas + numpy work, time={result.get('execution_time', '?')}s"


def test_network_access():
    """Test that network access works (when enabled)."""
    code = """
import requests
resp = requests.get("https://httpbin.org/get", timeout=10)
print(f"Status: {resp.status_code}")
print(f"Origin: {resp.json().get('origin', 'unknown')}")
"""
    result = execute(code, enable_network=True)
    assert result.get("error") is None, f"Error: {result.get('error')}"
    assert "Status: 200" in result.get("stdout", "")
    return True, f"Network access works, time={result.get('execution_time', '?')}s"


# --- Test Runner ---
TESTS = [
    ("Health Check", test_health),
    ("Simple Print", test_simple_print),
    ("Math Computation", test_math),
    ("Matplotlib Plot", test_matplotlib_plot),
    ("Syntax Error Handling", test_syntax_error),
    ("Runtime Error Handling", test_runtime_error),
    ("Timeout Enforcement", test_timeout),
    ("Multiple File Output", test_multiple_files),
    ("Pandas + NumPy", test_pandas_numpy),
    ("Network Access", test_network_access),
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
        try:
            start = time.time()
            success, detail = test_fn()
            elapsed = time.time() - start

            if success:
                print(f"{GREEN}✓{RESET} ({elapsed:.1f}s) {detail}")
                passed += 1
            else:
                print(f"{RED}✗{RESET} ({elapsed:.1f}s) {detail}")
                failed += 1
                errors.append((name, detail))
        except Exception as e:
            elapsed = time.time() - start
            print(f"{RED}✗{RESET} ({elapsed:.1f}s) {type(e).__name__}: {e}")
            failed += 1
            errors.append((name, str(e)))

    print(f"\n{BOLD}{'─' * 60}{RESET}")
    print(f"  Results: {GREEN}{passed} passed{RESET}, {RED}{failed} failed{RESET}, {passed + failed} total")

    if errors:
        print(f"\n  {RED}Failures:{RESET}")
        for name, detail in errors:
            print(f"    • {name}: {detail}")

    print(f"{BOLD}{'─' * 60}{RESET}\n")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
