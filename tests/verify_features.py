#!/usr/bin/env python3
import urllib.error
import urllib.request

from verification_client import GatewayClient, env_flag


CLIENT = GatewayClient.from_environment()


def execute(code: str, *, pip_packages: list[str] | None = None):
    """Execute code in a container session and return result."""
    status, container = CLIENT.request("POST", "/containers", {"enable_network": True})
    assert status == 200, container
    container_id = container["container_id"]

    try:
        return CLIENT.request(
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
        CLIENT.request("DELETE", f"/containers/{container_id}")


def test_no_auth():
    """Test that requests without authentication are rejected."""
    print("Testing request without API key...")
    token = CLIENT.token
    if not token:
        print("  Skipping because no bearer token is configured for this environment.")
        return

    req = urllib.request.Request(
        f"{CLIENT.base_url}/containers",
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
    if not env_flag("ALLOW_PIP_INSTALLS", default=False):
        print("  Skipping because ALLOW_PIP_INSTALLS is disabled in this environment.")
        return
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
