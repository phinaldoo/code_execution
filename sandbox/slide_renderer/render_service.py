from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from .config import SETTINGS
from .local_server import LocalStaticServer
from .models import RenderRequest, RenderingVersion

ZIP_MIME_TYPE = "application/zip"

_SLIDE_SELECTOR = ".slide"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_V2_SCRIPT_PATH = _PROJECT_ROOT / "v2" / "index.js"
_V2_WORKDIR = _PROJECT_ROOT / "v2"
_SUBPROCESS_ENV_KEYS = {
    "PATH",
    "HOME",
    "XDG_CACHE_HOME",
    "PLAYWRIGHT_BROWSERS_PATH",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONUNBUFFERED",
}


class RenderValidationError(ValueError):
    """Raised when request payload is invalid for rendering."""


class RenderExecutionError(RuntimeError):
    """Raised when renderer execution fails."""


@dataclass(frozen=True)
class RenderResult:
    file_name: str
    rendering_version: RenderingVersion
    content: bytes
    media_type: str
    slide_count: int


def validate_render_environment() -> None:
    """Validate runtime dependencies required by the render service."""
    _validate_temporary_storage()
    _validate_static_runtime_dependencies()


def _validate_temporary_storage() -> None:
    """Validate writable temporary storage used for isolated render workspaces."""
    issues: list[str] = []

    tmp_dir = Path(tempfile.gettempdir())
    if not tmp_dir.exists() or not os.access(tmp_dir, os.W_OK | os.X_OK):
        issues.append(f"temporary directory is not writable: {tmp_dir}")

    if issues:
        raise RuntimeError("; ".join(issues))


@lru_cache(maxsize=1)
def _validate_static_runtime_dependencies() -> None:
    """Validate immutable renderer dependencies once per process."""
    issues: list[str] = []
    active_rendering_version = RenderingVersion(SETTINGS.active_rendering_version)

    if active_rendering_version == RenderingVersion.v2:
        if not _V2_SCRIPT_PATH.exists():
            issues.append(f"v2 renderer script not found: {_V2_SCRIPT_PATH}")

        node_executable = shutil.which("node")
        if node_executable is None:
            issues.append("node executable not found in PATH")
        else:
            try:
                _validate_v2_node_dependencies(node_executable)
            except RuntimeError as exc:
                issues.append(str(exc))

    if active_rendering_version == RenderingVersion.v1:
        try:
            import v1.render  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            issues.append(f"v1 renderer import failed: {exc}")

    try:
        _validate_playwright_runtime()
    except RuntimeError as exc:
        issues.append(str(exc))

    if issues:
        raise RuntimeError("; ".join(issues))


def _validate_v2_node_dependencies(node_executable: str) -> None:
    """Validate that the Node renderer dependencies are installed and resolvable."""
    env = _build_subprocess_env()
    probe = (
        "require.resolve('playwright');"
        "require.resolve('pptxgenjs');"
        "process.stdout.write('ok');"
    )
    try:
        result = subprocess.run(
            [node_executable, "-e", probe],
            cwd=str(_V2_WORKDIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"v2 renderer dependency check failed: {exc}") from exc

    if result.returncode == 0:
        return

    details = (result.stderr or result.stdout or "").strip()
    message = details or "required Node dependencies are missing"
    raise RuntimeError(f"v2 renderer dependency check failed: {message}")


def _validate_playwright_runtime() -> None:
    """Validate that the Chromium runtime required by Playwright is present."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"playwright import failed: {exc}") from exc

    def _probe_playwright_runtime() -> Path:
        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path)

    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="playwright-runtime-check",
        ) as executor:
            future = executor.submit(_probe_playwright_runtime)
            executable_path = future.result(timeout=15)
    except concurrent.futures.TimeoutError as exc:
        raise RuntimeError("playwright runtime initialization timed out") from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"playwright runtime initialization failed: {exc}") from exc

    if not executable_path.exists():
        raise RuntimeError(
            f"playwright chromium executable not found: {executable_path}"
        )


async def render_presentation(request: RenderRequest) -> RenderResult:
    """Render presentation HTML to PPTX plus slide images."""
    rendering_version = RenderingVersion(SETTINGS.active_rendering_version)

    if len(request.html) > SETTINGS.max_html_chars:
        raise RenderValidationError(
            f"html is too large (>{SETTINGS.max_html_chars} characters)"
        )
    if len(request.input_files) > SETTINGS.max_input_files:
        raise RenderValidationError(
            f"too many input_files (max {SETTINGS.max_input_files})"
        )

    with tempfile.TemporaryDirectory(prefix="presentation_") as tmp_dir:
        presentation_root = Path(tmp_dir)
        html_path = presentation_root / "index.html"
        assets_dir = presentation_root / "assets"
        output_dir = presentation_root / "output"

        assets_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        html_path.write_text(request.html, encoding="utf-8")

        _save_assets(request, assets_dir)

        with LocalStaticServer(presentation_root) as server:
            input_url = f"{server.base_url}/index.html"
            allowed_origin = server.base_url

            if rendering_version == RenderingVersion.v1:
                render_pptx_coroutine = _render_v1(input_url, output_dir, allowed_origin)
            else:
                render_pptx_coroutine = _render_v2(input_url, output_dir, allowed_origin)

            pptx_task = asyncio.create_task(render_pptx_coroutine)
            slide_images_task = asyncio.create_task(
                _render_slide_images(input_url, output_dir, allowed_origin)
            )

            try:
                output_path, slide_image_paths = await asyncio.gather(
                    pptx_task,
                    slide_images_task,
                )
            except Exception:
                for task in (pptx_task, slide_images_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(pptx_task, slide_images_task, return_exceptions=True)
                raise

        if not output_path.exists():
            raise RenderExecutionError("renderer finished but no pptx output was produced")
        if not slide_image_paths:
            raise RenderExecutionError("renderer finished but no slide images were produced")

        _validate_slide_count(len(slide_image_paths))

        total_output_bytes = 0
        total_output_bytes = _ensure_render_output_budget(
            total_output_bytes,
            output_path.stat().st_size,
            output_path.name,
        )
        pptx_content = output_path.read_bytes()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base_name = f"presentation_{rendering_version.value}_{timestamp}"
        pptx_file_name = f"{base_name}.pptx"
        archive_file_name = f"{base_name}.zip"

        slide_images: list[tuple[str, bytes]] = []
        for slide_image_path in sorted(slide_image_paths):
            archive_path = f"slides/{slide_image_path.name}"
            total_output_bytes = _ensure_render_output_budget(
                total_output_bytes,
                slide_image_path.stat().st_size,
                archive_path,
            )
            slide_images.append((archive_path, slide_image_path.read_bytes()))
        archive_content = _build_render_archive(pptx_file_name, pptx_content, slide_images)
        _ensure_render_output_budget(0, len(archive_content), archive_file_name)

    return RenderResult(
        file_name=archive_file_name,
        rendering_version=rendering_version,
        content=archive_content,
        media_type=ZIP_MIME_TYPE,
        slide_count=len(slide_image_paths),
    )


def _build_render_archive(
    pptx_file_name: str,
    pptx_content: bytes,
    slide_images: list[tuple[str, bytes]],
) -> bytes:
    """Build ZIP archive containing PPTX file and slide images."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(pptx_file_name, pptx_content)
        for slide_image_name, slide_image_content in slide_images:
            archive.writestr(slide_image_name, slide_image_content)
    return buffer.getvalue()


def _validate_slide_count(slide_count: int) -> None:
    """Validate rendered slide count against configured limits."""
    if slide_count > SETTINGS.max_slides:
        raise RenderValidationError(
            f"too many slides rendered (max {SETTINGS.max_slides})"
        )


def _ensure_render_output_budget(consumed_bytes: int, next_bytes: int, label: str) -> int:
    """Track total generated output size and reject oversized render products."""
    total_bytes = consumed_bytes + next_bytes
    if total_bytes > SETTINGS.max_render_output_bytes:
        raise RenderValidationError(
            f"render output exceeds max size of {SETTINGS.max_render_output_bytes} "
            f"bytes while adding {label}"
        )
    return total_bytes


def _save_assets(request: RenderRequest, assets_dir: Path) -> None:
    """Save input files to assets directory after validation."""
    total_bytes = 0

    for input_file in request.input_files:
        try:
            decoded_bytes = _decode_base64(input_file.base64_content)
        except binascii.Error as exc:
            raise RenderValidationError(
                f"input file '{input_file.file_name}' has invalid base64 content"
            ) from exc

        file_size = len(decoded_bytes)
        if file_size > SETTINGS.max_asset_bytes:
            raise RenderValidationError(
                f"input file '{input_file.file_name}' exceeds max size of "
                f"{SETTINGS.max_asset_bytes} bytes"
            )

        total_bytes += file_size
        if total_bytes > SETTINGS.max_total_asset_bytes:
            raise RenderValidationError(
                f"input_files exceed max combined size of "
                f"{SETTINGS.max_total_asset_bytes} bytes"
            )

        (assets_dir / input_file.file_name).write_bytes(decoded_bytes)


def _decode_base64(content: str) -> bytes:
    """Decode base64 content, handling data URI prefix if present."""
    payload = content.strip()
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]
    return base64.b64decode(payload, validate=True)


def _extract_origin(url: str) -> str | None:
    """Extract normalized origin from URL."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None

    if not parsed.scheme or not parsed.netloc:
        return None

    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _is_allowed_request_url(request_url: str, allowed_origin: str | None) -> bool:
    """Check if request URL is allowed based on origin policy."""
    if request_url.startswith(("data:", "blob:", "about:")):
        return True
    if not allowed_origin:
        return True
    return _extract_origin(request_url) == _extract_origin(allowed_origin)


def _build_subprocess_env() -> dict[str, str]:
    """Build a minimal environment for subprocess-based renderers."""
    return {
        key: value
        for key, value in os.environ.items()
        if key in _SUBPROCESS_ENV_KEYS and value
    }


async def _apply_request_guard(context, allowed_origin: str | None) -> None:
    """Apply request guard to block disallowed origins."""
    if not allowed_origin:
        return

    async def _route_handler(route) -> None:
        request_url = route.request.url
        if _is_allowed_request_url(request_url, allowed_origin):
            await route.continue_()
            return
        await route.abort()

    await context.route("**/*", _route_handler)


async def _render_slide_images(input_url: str, output_dir: Path, allowed_origin: str) -> list[Path]:
    """Render slides as PNG images using Playwright."""
    from playwright.async_api import async_playwright

    slide_images_dir = output_dir / "slides"
    slide_images_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = None
        try:
            context = await browser.new_context(viewport={"width": 1920, "height": 1080})
            await _apply_request_guard(context, allowed_origin)
            page = await context.new_page()
            page.set_default_navigation_timeout(SETTINGS.page_load_timeout_ms)
            page.set_default_timeout(SETTINGS.page_load_timeout_ms)
            await page.goto(
                input_url,
                wait_until="networkidle",
                timeout=SETTINGS.page_load_timeout_ms,
            )
            await page.wait_for_load_state(
                "networkidle",
                timeout=SETTINGS.page_load_timeout_ms,
            )

            sections = await page.locator(_SLIDE_SELECTOR).all()
            if not sections:
                raise RenderExecutionError(
                    f"no slide elements found for selector '{_SLIDE_SELECTOR}'"
                )
            _validate_slide_count(len(sections))

            rendered_paths: list[Path] = []
            for slide_index, section in enumerate(sections, start=1):
                await section.scroll_into_view_if_needed()
                image_bytes = await section.screenshot(type="png")
                image_path = slide_images_dir / f"slide_{slide_index:03d}.png"
                image_path.write_bytes(image_bytes)
                rendered_paths.append(image_path)

            return rendered_paths
        finally:
            if context is not None:
                await context.close()
            await browser.close()


async def _render_v1(input_url: str, output_dir: Path, allowed_origin: str) -> Path:
    """Render presentation using v1 screenshot-to-PPTX renderer."""
    from v1.render import html_url_to_pptx

    try:
        return await html_url_to_pptx(
            input_url,
            output_dir=output_dir,
            allowed_origin=allowed_origin,
            page_load_timeout_ms=SETTINGS.page_load_timeout_ms,
            max_slides=SETTINGS.max_slides,
        )
    except ValueError as exc:
        raise RenderValidationError(str(exc)) from exc


async def _render_v2(input_url: str, output_dir: Path, allowed_origin: str) -> Path:
    """Render presentation using the v2 Node.js editable PPTX renderer."""
    if not _V2_SCRIPT_PATH.exists():
        raise RenderExecutionError(f"v2 renderer script not found: {_V2_SCRIPT_PATH}")

    output_path = output_dir / f"presentation_{uuid.uuid4()}.pptx"
    node_executable = shutil.which("node")
    if not node_executable:
        raise RenderExecutionError("node executable not found in PATH")

    env = _build_subprocess_env()
    env["ALLOWED_ORIGIN"] = allowed_origin
    env["PAGE_LOAD_TIMEOUT_MS"] = str(SETTINGS.page_load_timeout_ms)

    process = await asyncio.create_subprocess_exec(
        node_executable,
        str(_V2_SCRIPT_PATH),
        input_url,
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(_V2_WORKDIR),
        env=env,
    )
    try:
        stdout_bytes, stderr_bytes = await process.communicate()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise

    if process.returncode != 0:
        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        message = stderr_text or stdout_text or "unknown renderer error"
        raise RenderExecutionError(f"v2 renderer failed: {message}")

    return output_path

