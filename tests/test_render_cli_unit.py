#!/usr/bin/env python3
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import sys as _sys


PROJECT_DIR = Path(__file__).resolve().parent.parent
SANDBOX_DIR = PROJECT_DIR / "sandbox"
if str(SANDBOX_DIR) not in _sys.path:
    _sys.path.insert(0, str(SANDBOX_DIR))

import render_presentation as render_cli
from slide_renderer.models import RenderingVersion
from slide_renderer.render_service import RenderExecutionError, RenderValidationError


class RenderCliPayloadTests(unittest.TestCase):
    def test_emit_result_prints_compact_prefixed_json(self) -> None:
        stream = io.StringIO()
        with redirect_stdout(stream):
            render_cli.emit_result({"file_name": "deck.zip", "error": None})

        line = stream.getvalue().strip()
        self.assertTrue(line.startswith(render_cli.RESULT_PREFIX))
        self.assertEqual(json.loads(line[len(render_cli.RESULT_PREFIX) :]), {"file_name": "deck.zip", "error": None})
        self.assertNotIn(" ", line[len(render_cli.RESULT_PREFIX) :])

    def test_build_error_payload_normalizes_error_fields(self) -> None:
        self.assertEqual(
            render_cli.build_error_payload("bad", "ValueError", execution_time=1.25),
            {"error": "bad", "error_type": "ValueError", "execution_time": 1.25},
        )


class RenderCliRenderFromFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_render_from_file_validates_request_and_writes_result_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            request_path = root / "request.json"
            output_dir = root / "output"
            request_path.write_text(json.dumps({"html": "<section class='slide'>Hello</section>"}), encoding="utf-8")
            fake_result = SimpleNamespace(
                file_name="deck.zip",
                rendering_version=RenderingVersion.v2,
                media_type="application/zip",
                slide_count=3,
                content=b"zip-content",
            )

            with mock.patch.object(render_cli, "validate_runtime_configuration") as validate_config, mock.patch.object(
                render_cli,
                "validate_render_environment",
            ) as validate_env, mock.patch.object(
                render_cli,
                "render_presentation",
                mock.AsyncMock(return_value=fake_result),
            ) as render:
                payload = await render_cli.render_from_file(request_path, output_dir)

            validate_config.assert_called_once_with()
            validate_env.assert_called_once_with()
            render.assert_awaited_once()
            self.assertEqual((output_dir / "deck.zip").read_bytes(), b"zip-content")
            self.assertEqual(payload["file_name"], "deck.zip")
            self.assertEqual(payload["rendering_version"], "v2")
            self.assertEqual(payload["output_path"], str(output_dir / "deck.zip"))
            self.assertEqual(payload["output_size"], len(b"zip-content"))
            self.assertIsNone(payload["error"])


class RenderCliMainTests(unittest.TestCase):
    def run_main(self, request_path: Path, output_dir: Path) -> tuple[int, dict]:
        stream = io.StringIO()
        argv = ["render_presentation.py", "--request", str(request_path), "--output-dir", str(output_dir)]
        with mock.patch.object(sys, "argv", argv), redirect_stdout(stream):
            code = render_cli.main()
        line = stream.getvalue().strip().splitlines()[-1]
        self.assertTrue(line.startswith(render_cli.RESULT_PREFIX))
        return code, json.loads(line[len(render_cli.RESULT_PREFIX) :])

    def test_main_returns_zero_for_successful_render(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            request_path = root / "request.json"
            output_dir = root / "output"
            request_path.write_text("{}", encoding="utf-8")
            with mock.patch.object(
                render_cli,
                "render_from_file",
                mock.AsyncMock(return_value={"file_name": "deck.zip", "error": None}),
            ):
                code, payload = self.run_main(request_path, output_dir)

        self.assertEqual(code, 0)
        self.assertEqual(payload, {"file_name": "deck.zip", "error": None})

    def test_main_returns_validation_error_status_for_bad_json_or_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            request_path = root / "request.json"
            output_dir = root / "output"
            request_path.write_text("{not-json", encoding="utf-8")
            code, payload = self.run_main(request_path, output_dir)

        self.assertEqual(code, 2)
        self.assertEqual(payload["error_type"], "JSONDecodeError")
        self.assertIn("error", payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            request_path = root / "request.json"
            output_dir = root / "output"
            request_path.write_text("{}", encoding="utf-8")
            with mock.patch.object(render_cli, "render_from_file", mock.AsyncMock(side_effect=RenderValidationError("bad request"))):
                code, payload = self.run_main(request_path, output_dir)

        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "bad request")
        self.assertEqual(payload["error_type"], "RenderValidationError")

    def test_main_returns_execution_error_status_for_renderer_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            request_path = root / "request.json"
            output_dir = root / "output"
            request_path.write_text("{}", encoding="utf-8")
            with mock.patch.object(render_cli, "render_from_file", mock.AsyncMock(side_effect=RenderExecutionError("renderer down"))):
                code, payload = self.run_main(request_path, output_dir)

        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "renderer down")
        self.assertEqual(payload["error_type"], "RenderExecutionError")

    def test_main_hides_unexpected_exception_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            request_path = root / "request.json"
            output_dir = root / "output"
            request_path.write_text("{}", encoding="utf-8")
            with mock.patch.object(render_cli, "render_from_file", mock.AsyncMock(side_effect=RuntimeError("secret detail"))):
                stream = io.StringIO()
                argv = ["render_presentation.py", "--request", str(request_path), "--output-dir", str(output_dir)]
                with mock.patch.object(sys, "argv", argv), redirect_stdout(stream), mock.patch.object(render_cli.traceback, "print_exc"):
                    code = render_cli.main()

        line = stream.getvalue().strip().splitlines()[-1]
        payload = json.loads(line[len(render_cli.RESULT_PREFIX) :])
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "rendering failed")
        self.assertEqual(payload["error_type"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
