#!/usr/bin/env python3
"""
Smoke test the merged slide renderer endpoint.

Usage:
    python3 tests/verify_slide_rendering.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile

from verification_client import resolve_token


def main() -> int:
    base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    token = resolve_token()
    expected_version = os.getenv("EXPECTED_RENDERING_VERSION")

    html = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <style>
      body { margin: 0; background: #111827; font-family: Arial, sans-serif; }
      .slide {
        width: 1920px;
        height: 1080px;
        box-sizing: border-box;
        padding: 96px;
        background: #f8fafc;
        color: #0f172a;
      }
      h1 { font-size: 96px; margin: 0 0 32px; }
      p { font-size: 44px; line-height: 1.3; max-width: 1300px; }
    </style>
  </head>
  <body>
    <section class="slide">
      <h1>Shared renderer</h1>
      <p>This deck was rendered from the code execution sandbox Playwright runtime.</p>
    </section>
  </body>
</html>
"""
    payload = json.dumps({"html": html}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        f"{base_url}/api/render",
        data=payload,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            body = response.read()
            status = response.status
            rendering_version = response.headers.get("X-Rendering-Version")
            slide_count = response.headers.get("X-Slide-Count")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"Render request failed with HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1

    if status != 200:
        print(f"Unexpected status {status}", file=sys.stderr)
        return 1
    if expected_version and rendering_version != expected_version:
        print(
            f"Expected renderer {expected_version}, got {rendering_version}",
            file=sys.stderr,
        )
        return 1
    if slide_count != "1":
        print(f"Expected one rendered slide, got {slide_count}", file=sys.stderr)
        return 1

    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = archive.namelist()
        if not any(name.endswith(".pptx") for name in names):
            print(f"Archive missing PPTX: {names}", file=sys.stderr)
            return 1
        if "slides/slide_001.png" not in names:
            print(f"Archive missing slide image: {names}", file=sys.stderr)
            return 1

    print(f"Slide rendering works ({rendering_version}, {len(body)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

