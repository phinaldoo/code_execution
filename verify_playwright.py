#!/usr/bin/env python3
import json
import urllib.request


BASE_URL = "http://localhost:8000"


def request(method: str, path: str, payload: dict | None = None, timeout: int = 180):
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main():
    _, container = request("POST", "/containers", {})
    container_id = container["container_id"]

    code = """
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    page.goto('https://example.com', wait_until='domcontentloaded')
    page.screenshot(path='/tmp/output/playwright-example.png', full_page=True)
    print(page.title())
    browser.close()
"""

    try:
        status, result = request(
            "POST",
            "/execute",
            {
                "container_id": container_id,
                "language": "python",
                "code": code,
                "timeout": 90,
            },
        )

        print(f"HTTP: {status}")
        print(f"Error: {result.get('error')}")
        print(f"Error Type: {result.get('error_type')}")
        print(f"Stdout: {(result.get('stdout') or '').strip()}")
        print(f"Files: {[f.get('name') for f in result.get('files', [])]}")

        if result.get("error"):
            raise SystemExit(1)

        files = result.get("files", [])
        if not files:
            print("No output file returned.")
            raise SystemExit(1)

        print("Playwright verification passed.")
    finally:
        request("DELETE", f"/containers/{container_id}")


if __name__ == "__main__":
    main()
