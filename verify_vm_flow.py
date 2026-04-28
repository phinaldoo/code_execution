#!/usr/bin/env python3
import base64
from verification_client import GatewayClient


CLIENT = GatewayClient.from_environment()


def main():
    """Verify VM flow including health, container creation, and code execution."""
    print("1. Checking health...")
    status, payload = CLIENT.request("GET", "/healthz", timeout=60)
    assert status == 200, payload
    assert payload["status"] == "healthy", payload
    print("   Health OK")

    print("2. Creating container session...")
    status, created = CLIENT.request("POST", "/containers", {"enable_network": True}, timeout=60)
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
        status, result = CLIENT.request(
            "POST",
            "/execute",
            {
                "container_id": container_id,
                "language": "python",
                "code": python_code,
            },
            timeout=60,
        )
        assert status == 200, result
        assert "Hello from persistent Python session!" in result["stdout"], result
        print("   Python execution OK")

        print("4. Executing Bash code...")
        bash_code = "echo 'Hello from Bash!'\ncat /home/sandbox/test.txt"
        status, result = CLIENT.request(
            "POST",
            "/execute",
            {
                "container_id": container_id,
                "language": "bash",
                "code": bash_code,
            },
            timeout=60,
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
        status, result = CLIENT.request(
            "POST",
            "/execute",
            {
                "container_id": container_id,
                "language": "bash",
                "code": script,
                "files": [{"name": "input.txt", "content": input_b64}],
            },
            timeout=60,
        )
        assert status == 200, result
        assert result["files"], result
        decoded = base64.b64decode(result["files"][0]["content"]).decode("utf-8")
        assert "Processed:" in decoded, result
        print("   File flow OK")
    finally:
        print("6. Deleting container session...")
        status, result = CLIENT.request("DELETE", f"/containers/{container_id}", timeout=60)
        assert status == 200, result
        print("   Cleanup OK")


if __name__ == "__main__":
    main()
