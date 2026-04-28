#!/usr/bin/env python3
from verification_client import GatewayClient


CLIENT = GatewayClient.from_environment()


def main():
    """Verify Playwright functionality in the sandbox."""
    status, container = CLIENT.request("POST", "/containers", {"enable_network": True}, timeout=180)
    assert status == 200, container
    container_id = container["container_id"]

    code = """
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    page.set_content(
        "<html><head><title>Sandbox Playwright Check</title></head>"
        "<body><main><h1>Playwright OK</h1><p>Local render verification.</p></main></body></html>",
        wait_until="domcontentloaded",
    )
    page.screenshot(path="/tmp/output/playwright-example.png", full_page=True)
    print(page.title())
    browser.close()
"""

    try:
        status, result = CLIENT.request(
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
        assert "Sandbox Playwright Check" in result.get("stdout", ""), result
        assert any(file["name"] == "playwright-example.png" for file in result.get("files", [])), result
        print("Playwright verification passed.")
    finally:
        CLIENT.request("DELETE", f"/containers/{container_id}", timeout=180)


if __name__ == "__main__":
    main()
