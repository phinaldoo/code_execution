#!/usr/bin/env python3
import json
import os
import urllib.error
import urllib.request


BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")


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


def request(method: str, path: str, payload: dict | None = None, timeout: int = 180):
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


def main():
    status, container = request("POST", "/containers", {"enable_network": True})
    assert status == 200, container
    container_id = container["container_id"]

    code = """
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    page.goto("https://example.com", wait_until="domcontentloaded")
    page.screenshot(path="/tmp/output/playwright-example.png", full_page=True)
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
        assert status == 200, result
        assert not result.get("error"), result
        assert any(file["name"] == "playwright-example.png" for file in result.get("files", [])), result
        print("Playwright verification passed.")
    finally:
        request("DELETE", f"/containers/{container_id}")


if __name__ == "__main__":
    main()
