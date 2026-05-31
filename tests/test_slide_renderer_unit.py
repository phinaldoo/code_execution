#!/usr/bin/env python3
import asyncio
import base64
import importlib
import os
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pydantic import ValidationError

import sys


PROJECT_DIR = Path(__file__).resolve().parent.parent
SANDBOX_DIR = PROJECT_DIR / "sandbox"
if str(SANDBOX_DIR) not in sys.path:
    sys.path.insert(0, str(SANDBOX_DIR))

from slide_renderer import config as renderer_config
from slide_renderer import local_server as local_server_module
from slide_renderer import render_service
from slide_renderer.local_server import LocalStaticServer
from slide_renderer.models import InputFile, RenderRequest, RenderingVersion
from slide_renderer.render_service import RenderExecutionError, RenderValidationError

try:
    from v1 import render as v1_render
except ModuleNotFoundError as exc:
    v1_render = None
    V1_IMPORT_ERROR = exc
else:
    V1_IMPORT_ERROR = None


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class SlideRendererModelTests(unittest.TestCase):
    def test_input_file_accepts_base64_alias_and_forbids_extra_fields(self) -> None:
        input_file = InputFile(file_name="image.png", base64="aGVsbG8=")
        self.assertEqual(input_file.base64_content, "aGVsbG8=")

        with self.assertRaises(ValidationError):
            InputFile(file_name="image.png", base64_content="aGVsbG8=", extra=True)

    def test_input_file_rejects_unsafe_file_names(self) -> None:
        invalid_names = ["../image.png", "folder/image.png", "folder\\image.png", "space name.png", ".", "..", "semi;colon.png"]
        for invalid_name in invalid_names:
            with self.subTest(invalid_name=invalid_name):
                with self.assertRaises(ValidationError):
                    InputFile(file_name=invalid_name, base64_content="aGVsbG8=")

    def test_render_request_forbids_extra_fields_and_requires_html(self) -> None:
        with self.assertRaises(ValidationError):
            RenderRequest(html="<section></section>", unexpected=True)
        with self.assertRaises(ValidationError):
            RenderRequest(html="")

    def test_render_request_rejects_duplicate_input_file_names(self) -> None:
        with self.assertRaises(ValidationError):
            RenderRequest(
                html="<section class='slide'>Hello</section>",
                input_files=[
                    InputFile(file_name="same.png", base64_content="aGVsbG8="),
                    InputFile(file_name="same.png", base64_content="aGVsbG8="),
                ],
            )


class SlideRendererConfigTests(unittest.TestCase):
    def reload_config(self, env: dict[str, str]):
        self.addCleanup(lambda: importlib.reload(renderer_config))
        with mock.patch.dict(os.environ, env, clear=True):
            return importlib.reload(renderer_config)

    def test_settings_parse_environment_aliases_and_properties(self) -> None:
        config = self.reload_config(
            {
                "APP_ENV": "production",
                "DEVELOPMENT_MODE": "false",
                "ENABLE_DOCS": "true",
                "API_KEYS": " one, two ,, ",
                "ALLOWED_HOSTS": "example.com, localhost",
                "RENDERING_VERSION": "v2",
                "RENDER_MAX_HTML_CHARS": "2000",
                "MAX_RENDER_INPUT_FILES": "5",
            }
        )

        self.assertEqual(config.SETTINGS.environment_name, "production")
        self.assertTrue(config.SETTINGS.is_production)
        self.assertTrue(config.SETTINGS.docs_enabled)
        self.assertTrue(config.SETTINGS.beta)
        self.assertEqual(config.SETTINGS.api_keys, ("one", "two"))
        self.assertEqual(config.SETTINGS.allowed_hosts, ("example.com", "localhost"))
        self.assertEqual(config.SETTINGS.max_html_chars, 2000)
        self.assertEqual(config.SETTINGS.max_input_files, 5)

    def test_invalid_integer_and_boolean_env_values_are_reported_by_validation(self) -> None:
        config = self.reload_config(
            {
                "RENDER_TIMEOUT_SECONDS": "bad",
                "DEVELOPMENT_MODE": "sometimes",
            }
        )

        with self.assertRaisesRegex(RuntimeError, "RENDER_TIMEOUT_SECONDS"):
            config.validate_runtime_configuration()

    def test_validate_runtime_configuration_rejects_bad_versions_and_inconsistent_limits(self) -> None:
        settings = replace(
            renderer_config.SETTINGS,
            active_rendering_version="v3",
            max_total_asset_bytes=1_024,
            max_asset_bytes=2_048,
            max_request_body_bytes=1_024,
            max_html_chars=2_048,
            max_render_output_bytes=1_024,
            page_load_timeout_ms=10_000,
            render_timeout_seconds=5,
        )
        with mock.patch.object(renderer_config, "SETTINGS", settings), mock.patch.object(renderer_config, "_CONFIG_PARSE_ERRORS", []):
            with self.assertRaises(RuntimeError) as ctx:
                renderer_config.validate_runtime_configuration()

        message = str(ctx.exception)
        self.assertIn("SLIDE_RENDERING_VERSION", message)
        self.assertIn("RENDER_MAX_TOTAL_ASSET_BYTES", message)
        self.assertIn("PAGE_LOAD_TIMEOUT_MS", message)

    def test_production_warnings_can_raise_or_log_when_explicitly_allowed(self) -> None:
        unsafe_settings = replace(
            renderer_config.SETTINGS,
            environment_name="production",
            development_mode=True,
            enable_docs=True,
            api_key_auth_enabled=False,
            allowed_hosts=("*",),
            allow_insecure_production_configuration=False,
        )
        with mock.patch.object(renderer_config, "SETTINGS", unsafe_settings), mock.patch.object(renderer_config, "_CONFIG_PARSE_ERRORS", []):
            with self.assertRaisesRegex(RuntimeError, "DEVELOPMENT_MODE"):
                renderer_config.validate_runtime_configuration()

        allowed_settings = replace(unsafe_settings, allow_insecure_production_configuration=True)
        with mock.patch.object(renderer_config, "SETTINGS", allowed_settings), mock.patch.object(renderer_config, "_CONFIG_PARSE_ERRORS", []):
            with self.assertLogs("slide_renderer", level="WARNING") as logs:
                renderer_config.validate_runtime_configuration()
        self.assertIn("ALLOW_INSECURE_PRODUCTION_CONFIGURATION=true", "\n".join(logs.output))


class SlideRendererServiceHelperTests(unittest.TestCase):
    def test_decode_base64_accepts_plain_payloads_and_data_uris(self) -> None:
        self.assertEqual(render_service._decode_base64("aGVsbG8="), b"hello")
        self.assertEqual(render_service._decode_base64(" data:text/plain;base64,aGVsbG8= "), b"hello")
        with self.assertRaises(Exception):
            render_service._decode_base64("not base64")

    def test_origin_extraction_and_request_allow_list(self) -> None:
        self.assertEqual(
            render_service._extract_origin("HTTP://Example.COM:8080/path?x=1"),
            "http://example.com:8080",
        )
        self.assertIsNone(render_service._extract_origin("not-a-url"))
        self.assertTrue(render_service._is_allowed_request_url("data:image/png;base64,abc", "http://example.com"))
        self.assertTrue(render_service._is_allowed_request_url("blob:http://example.com/id", "http://example.com"))
        self.assertTrue(render_service._is_allowed_request_url("about:blank", "http://example.com"))
        self.assertTrue(render_service._is_allowed_request_url("http://example.com/a", "http://EXAMPLE.com"))
        self.assertFalse(render_service._is_allowed_request_url("http://evil.example/a", "http://example.com"))
        self.assertTrue(render_service._is_allowed_request_url("http://anything.example/a", None))

    def test_build_subprocess_env_filters_to_explicit_allow_list(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "PATH": "/bin",
                "HOME": "/home/sandbox",
                "SECRET": "do-not-pass",
                "PYTHONUNBUFFERED": "1",
                "EMPTY_ALLOWED": "",
            },
            clear=True,
        ):
            env = render_service._build_subprocess_env()

        self.assertEqual(env, {"PATH": "/bin", "HOME": "/home/sandbox", "PYTHONUNBUFFERED": "1"})

    def test_build_render_archive_contains_pptx_and_slide_images(self) -> None:
        archive_bytes = render_service._build_render_archive(
            "deck.pptx",
            b"pptx",
            [("slides/slide_001.png", b"png1"), ("slides/slide_002.png", b"png2")],
        )

        with zipfile.ZipFile(render_service.BytesIO(archive_bytes)) as archive:
            self.assertEqual(archive.read("deck.pptx"), b"pptx")
            self.assertEqual(archive.read("slides/slide_001.png"), b"png1")
            self.assertEqual(archive.read("slides/slide_002.png"), b"png2")

    def test_validate_slide_count_uses_configured_limit(self) -> None:
        settings = SimpleNamespace(max_slides=2)
        with mock.patch.object(render_service, "SETTINGS", settings):
            render_service._validate_slide_count(2)
            with self.assertRaises(RenderValidationError):
                render_service._validate_slide_count(3)

    def test_ensure_render_output_budget_tracks_total_and_rejects_overflow(self) -> None:
        settings = SimpleNamespace(max_render_output_bytes=10)
        with mock.patch.object(render_service, "SETTINGS", settings):
            self.assertEqual(render_service._ensure_render_output_budget(4, 6, "file"), 10)
            with self.assertRaisesRegex(RenderValidationError, "render output exceeds"):
                render_service._ensure_render_output_budget(4, 7, "file")

    def test_save_assets_writes_files_and_enforces_limits(self) -> None:
        request = RenderRequest(
            html="<section class='slide'>Hello</section>",
            input_files=[InputFile(file_name="hello.txt", base64_content=base64.b64encode(b"hello").decode("ascii"))],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = SimpleNamespace(max_asset_bytes=10, max_total_asset_bytes=10)
            with mock.patch.object(render_service, "SETTINGS", settings):
                render_service._save_assets(request, Path(temp_dir))
            self.assertEqual((Path(temp_dir) / "hello.txt").read_bytes(), b"hello")

    def test_save_assets_rejects_invalid_base64_and_size_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = SimpleNamespace(max_asset_bytes=4, max_total_asset_bytes=10)
            invalid = RenderRequest(
                html="<section class='slide'>Hello</section>",
                input_files=[InputFile(file_name="bad.txt", base64_content="%%%")],
            )
            with mock.patch.object(render_service, "SETTINGS", settings):
                with self.assertRaisesRegex(RenderValidationError, "invalid base64"):
                    render_service._save_assets(invalid, Path(temp_dir))

            too_big = RenderRequest(
                html="<section class='slide'>Hello</section>",
                input_files=[InputFile(file_name="big.txt", base64_content=base64.b64encode(b"12345").decode("ascii"))],
            )
            with mock.patch.object(render_service, "SETTINGS", settings):
                with self.assertRaisesRegex(RenderValidationError, "exceeds max size"):
                    render_service._save_assets(too_big, Path(temp_dir))

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = SimpleNamespace(max_asset_bytes=10, max_total_asset_bytes=5)
            request = RenderRequest(
                html="<section class='slide'>Hello</section>",
                input_files=[
                    InputFile(file_name="one.txt", base64_content=base64.b64encode(b"123").decode("ascii")),
                    InputFile(file_name="two.txt", base64_content=base64.b64encode(b"456").decode("ascii")),
                ],
            )
            with mock.patch.object(render_service, "SETTINGS", settings):
                with self.assertRaisesRegex(RenderValidationError, "combined size"):
                    render_service._save_assets(request, Path(temp_dir))

    def test_validate_v2_node_dependencies_accepts_successful_probe(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(render_service.subprocess, "run", return_value=completed) as run:
            render_service._validate_v2_node_dependencies("/usr/bin/node")
        self.assertEqual(run.call_args.args[0][:2], ["/usr/bin/node", "-e"])

    def test_validate_v2_node_dependencies_reports_probe_failure_and_os_errors(self) -> None:
        failed = SimpleNamespace(returncode=1, stdout="", stderr="missing module")
        with mock.patch.object(render_service.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "missing module"):
                render_service._validate_v2_node_dependencies("/usr/bin/node")

        with mock.patch.object(render_service.subprocess, "run", side_effect=OSError("cannot exec")):
            with self.assertRaisesRegex(RuntimeError, "cannot exec"):
                render_service._validate_v2_node_dependencies("/usr/bin/node")


class SlideRendererAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_request_guard_registers_route_handler_when_origin_is_present(self) -> None:
        context = SimpleNamespace(route=mock.AsyncMock())
        await render_service._apply_request_guard(context, "http://example.com")
        context.route.assert_awaited_once()

    async def test_apply_request_guard_noops_without_origin(self) -> None:
        context = SimpleNamespace(route=mock.AsyncMock())
        await render_service._apply_request_guard(context, None)
        context.route.assert_not_awaited()

    async def test_render_v2_rejects_missing_script_before_spawning_node(self) -> None:
        with mock.patch.object(render_service, "_V2_SCRIPT_PATH", Path("/missing/index.js")):
            with self.assertRaisesRegex(RenderExecutionError, "v2 renderer script not found"):
                await render_service._render_v2("http://localhost/index.html", Path(tempfile.gettempdir()), "http://localhost")

    async def test_render_presentation_rejects_payload_limits_before_workdir_setup(self) -> None:
        request = RenderRequest(html="12345")
        settings = SimpleNamespace(
            active_rendering_version="v1",
            max_html_chars=4,
            max_input_files=10,
            max_asset_bytes=100,
            max_total_asset_bytes=100,
            max_render_output_bytes=100,
            max_slides=10,
        )
        with mock.patch.object(render_service, "SETTINGS", settings):
            with self.assertRaisesRegex(RenderValidationError, "html is too large"):
                await render_service.render_presentation(request)

    async def test_render_presentation_builds_zip_archive_from_pptx_and_slide_images(self) -> None:
        class FakeServer:
            def __init__(self, root_dir: Path) -> None:
                self.root_dir = root_dir
                self.base_url = "http://127.0.0.1:12345"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        async def fake_render_v1(input_url: str, output_dir: Path, allowed_origin: str) -> Path:
            self.assertEqual(input_url, "http://127.0.0.1:12345/index.html")
            self.assertEqual(allowed_origin, "http://127.0.0.1:12345")
            output_path = output_dir / "deck.pptx"
            output_path.write_bytes(b"pptx-content")
            return output_path

        async def fake_slide_images(input_url: str, output_dir: Path, allowed_origin: str) -> list[Path]:
            slides_dir = output_dir / "slides"
            slides_dir.mkdir(parents=True, exist_ok=True)
            first = slides_dir / "slide_001.png"
            second = slides_dir / "slide_002.png"
            first.write_bytes(b"png-1")
            second.write_bytes(b"png-2")
            return [second, first]

        settings = SimpleNamespace(
            active_rendering_version="v1",
            max_html_chars=10_000,
            max_input_files=10,
            max_asset_bytes=100,
            max_total_asset_bytes=100,
            max_render_output_bytes=10_000,
            max_slides=10,
        )
        with mock.patch.object(render_service, "SETTINGS", settings), mock.patch.object(
            render_service,
            "LocalStaticServer",
            FakeServer,
        ), mock.patch.object(render_service, "_render_v1", fake_render_v1), mock.patch.object(
            render_service,
            "_render_slide_images",
            fake_slide_images,
        ):
            result = await render_service.render_presentation(
                RenderRequest(html="<section class='slide'>Hello</section>")
            )

        self.assertEqual(result.rendering_version, RenderingVersion.v1)
        self.assertEqual(result.media_type, "application/zip")
        self.assertEqual(result.slide_count, 2)
        self.assertRegex(result.file_name, r"^presentation_v1_\d{8}T\d{6}Z\.zip$")
        with zipfile.ZipFile(render_service.BytesIO(result.content)) as archive:
            names = archive.namelist()
            self.assertTrue(any(name.endswith(".pptx") for name in names))
            self.assertEqual(archive.read("slides/slide_001.png"), b"png-1")
            self.assertEqual(archive.read("slides/slide_002.png"), b"png-2")


class LocalStaticServerTests(unittest.TestCase):
    def test_local_static_server_starts_and_stops_threaded_http_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "index.html").write_text("hello", encoding="utf-8")
            fake_server = SimpleNamespace(
                server_address=("127.0.0.1", 43210),
                daemon_threads=False,
                request_queue_size=0,
                serve_forever=mock.Mock(),
                shutdown=mock.Mock(),
                server_close=mock.Mock(),
            )
            fake_thread = SimpleNamespace(start=mock.Mock(), join=mock.Mock())

            with mock.patch.object(local_server_module, "ThreadingHTTPServer", return_value=fake_server) as server_cls:
                with mock.patch.object(local_server_module.threading, "Thread", return_value=fake_thread) as thread_cls:
                    with LocalStaticServer(root) as server:
                        self.assertEqual(server.base_url, "http://127.0.0.1:43210")
                        self.assertTrue(fake_server.daemon_threads)
                        self.assertEqual(fake_server.request_queue_size, 16)
                        fake_thread.start.assert_called_once_with()

            server_cls.assert_called_once()
            thread_cls.assert_called_once_with(target=fake_server.serve_forever, daemon=True)
            fake_server.shutdown.assert_called_once_with()
            fake_server.server_close.assert_called_once_with()
            fake_thread.join.assert_called_once_with(timeout=2)


@unittest.skipIf(v1_render is None, f"v1 renderer optional dependency unavailable: {V1_IMPORT_ERROR}")
class V1RendererHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_origin_helpers_match_slide_render_service_behavior(self) -> None:
        self.assertEqual(v1_render._extract_origin("HTTPS://Example.COM/path"), "https://example.com")
        self.assertIsNone(v1_render._extract_origin("not-a-url"))
        self.assertTrue(v1_render._is_allowed_request_url("data:text/plain,hi", "http://example.com"))
        self.assertTrue(v1_render._is_allowed_request_url("http://example.com/a", "http://EXAMPLE.com"))
        self.assertFalse(v1_render._is_allowed_request_url("http://other.example/a", "http://example.com"))

    def test_create_presentation_sets_sixteen_by_nine_size(self) -> None:
        prs = v1_render._create_presentation()
        self.assertEqual(prs.slide_width, v1_render.Inches(16))
        self.assertEqual(prs.slide_height, v1_render.Inches(9))

    def test_save_presentation_writes_generated_pptx_to_output_directory(self) -> None:
        prs = v1_render._create_presentation()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = v1_render._save_presentation(prs, temp_dir)
            self.assertEqual(path.parent, Path(temp_dir))
            self.assertEqual(path.suffix, ".pptx")
            self.assertTrue(path.exists())

    async def test_render_page_to_pptx_rejects_missing_and_excess_slide_elements(self) -> None:
        class FakeLocator:
            def __init__(self, sections):
                self._sections = sections

            async def all(self):
                return self._sections

        class FakePage:
            def __init__(self, sections):
                self._sections = sections

            def locator(self, selector):
                return FakeLocator(self._sections)

        prs = v1_render._create_presentation()
        with self.assertRaisesRegex(RuntimeError, "No elements"):
            await v1_render._render_page_to_pptx(FakePage([]), prs, ".slide")

        with self.assertRaisesRegex(ValueError, "too many slides"):
            await v1_render._render_page_to_pptx(FakePage([object(), object()]), prs, ".slide", max_slides=1)

    async def test_render_page_to_pptx_adds_one_slide_per_section(self) -> None:
        class FakeSection:
            async def scroll_into_view_if_needed(self):
                return None

            async def screenshot(self):
                return PNG_1X1

        class FakeLocator:
            async def all(self):
                return [FakeSection(), FakeSection()]

        class FakePage:
            def locator(self, selector):
                self.selector = selector
                return FakeLocator()

        prs = v1_render._create_presentation()
        page = FakePage()

        await v1_render._render_page_to_pptx(page, prs, ".slide", max_slides=2)

        self.assertEqual(page.selector, ".slide")
        self.assertEqual(len(prs.slides), 2)


if __name__ == "__main__":
    unittest.main()
