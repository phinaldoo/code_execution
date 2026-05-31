#!/usr/bin/env python3
import base64
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parent.parent
SANDBOX_DIR = PROJECT_DIR / "sandbox"
if str(SANDBOX_DIR) not in sys.path:
    sys.path.insert(0, str(SANDBOX_DIR))

import executor


class ExecutorConfigurationTests(unittest.TestCase):
    def test_get_exec_timeout_uses_default_for_missing_or_invalid_values(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(executor.get_exec_timeout(default=17), 17)
        with mock.patch.dict(os.environ, {"EXEC_TIMEOUT": ""}, clear=True):
            self.assertEqual(executor.get_exec_timeout(default=17), 17)
        with mock.patch.dict(os.environ, {"EXEC_TIMEOUT": "bad"}, clear=True):
            self.assertEqual(executor.get_exec_timeout(default=17), 17)

    def test_get_exec_timeout_clamps_to_at_least_one_second(self) -> None:
        with mock.patch.dict(os.environ, {"EXEC_TIMEOUT": "0"}, clear=True):
            self.assertEqual(executor.get_exec_timeout(default=17), 1)
        with mock.patch.dict(os.environ, {"EXEC_TIMEOUT": "-5"}, clear=True):
            self.assertEqual(executor.get_exec_timeout(default=17), 1)
        with mock.patch.dict(os.environ, {"EXEC_TIMEOUT": "9"}, clear=True):
            self.assertEqual(executor.get_exec_timeout(default=17), 9)

    def test_decode_code_from_environment_decodes_valid_utf8_base64(self) -> None:
        encoded = base64.b64encode("print('hi')".encode("utf-8")).decode("ascii")
        with mock.patch.dict(os.environ, {"CODE_B64": encoded}, clear=True):
            self.assertEqual(executor.decode_code_from_environment(), "print('hi')")

    def test_decode_code_from_environment_rejects_missing_invalid_and_empty_code(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "No code provided"):
                executor.decode_code_from_environment()
        with mock.patch.dict(os.environ, {"CODE_B64": "%%%"}, clear=True):
            with self.assertRaisesRegex(ValueError, "Failed to decode"):
                executor.decode_code_from_environment()
        with mock.patch.dict(os.environ, {"CODE_B64": base64.b64encode(b"   ").decode("ascii")}, clear=True):
            with self.assertRaisesRegex(ValueError, "Empty code"):
                executor.decode_code_from_environment()

    def test_install_pip_packages_noops_without_packages(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(executor.install_pip_packages(), (None, None))
        with mock.patch.dict(os.environ, {"PIP_PACKAGES": " , "}, clear=True):
            self.assertEqual(executor.install_pip_packages(), (None, None))

    def test_install_pip_packages_reports_subprocess_failures(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["pip"],
            returncode=2,
            stdout="stdout details",
            stderr="stderr details",
        )
        with mock.patch.dict(os.environ, {"PIP_PACKAGES": "demo"}, clear=True):
            with mock.patch.object(executor.subprocess, "run", return_value=completed) as run:
                error, install_time = executor.install_pip_packages()

        self.assertIn("Pip install failed with code 2", error or "")
        self.assertIn("stderr details", error or "")
        self.assertIsInstance(install_time, float)
        self.assertIn("demo", run.call_args.args[0])

    def test_install_pip_packages_reports_timeout_and_exceptions(self) -> None:
        with mock.patch.dict(os.environ, {"PIP_PACKAGES": "demo", "EXEC_TIMEOUT": "3"}, clear=True):
            with mock.patch.object(executor.subprocess, "run", side_effect=subprocess.TimeoutExpired(["pip"], 3)):
                error, install_time = executor.install_pip_packages()
        self.assertEqual(error, "Pip install timed out after 3 seconds")
        self.assertIsInstance(install_time, float)

        with mock.patch.dict(os.environ, {"PIP_PACKAGES": "demo"}, clear=True):
            with mock.patch.object(executor.subprocess, "run", side_effect=RuntimeError("boom")):
                error, install_time = executor.install_pip_packages()
        self.assertIn("Pip install exception: boom", error or "")
        self.assertIsInstance(install_time, float)


class ExecutorResultPayloadTests(unittest.TestCase):
    def test_truncate_output_leaves_short_output_unchanged(self) -> None:
        self.assertEqual(executor.truncate_output("short", max_length=10), "short")
        self.assertEqual(executor.truncate_output("12345", max_length=5), "12345")

    def test_truncate_output_appends_marker_for_long_output(self) -> None:
        truncated = executor.truncate_output("abcdef", max_length=3)
        self.assertTrue(truncated.startswith("abc"))
        self.assertIn("OUTPUT TRUNCATED", truncated)
        self.assertIn("6 chars total", truncated)

    def test_build_result_normalizes_defaults_and_optional_install_time(self) -> None:
        result = executor.build_result(stdout="hello", stderr="err", files=None, execution_time=1.25)
        self.assertEqual(
            result,
            {
                "stdout": "hello",
                "stderr": "err",
                "error": None,
                "error_type": None,
                "files": [],
                "execution_time": 1.25,
            },
        )

        result = executor.build_result(error="bad", error_type="ValueError", install_time=0.5)
        self.assertEqual(result["install_time"], 0.5)
        self.assertEqual(result["error_type"], "ValueError")

    def test_emit_result_prints_prefixed_json_payload(self) -> None:
        stream = io.StringIO()
        with redirect_stdout(stream):
            executor.emit_result({"stdout": "ok"})

        line = stream.getvalue().strip()
        self.assertTrue(line.startswith(executor.RESULT_PREFIX))
        self.assertEqual(json.loads(line[len(executor.RESULT_PREFIX) :]), {"stdout": "ok"})


class ExecutorOutputDirectoryTests(unittest.TestCase):
    def with_temp_dirs(self):
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        stack = mock.patch.object(executor, "OUTPUT_DIR", root / "output")
        stack2 = mock.patch.object(executor, "MISC_DIR", root / "misc")
        return temp_dir, stack, stack2

    def test_setup_output_dir_cleans_existing_output_and_creates_output_and_misc(self) -> None:
        temp_dir, output_patch, misc_patch = self.with_temp_dirs()
        with temp_dir, output_patch, misc_patch:
            executor.OUTPUT_DIR.mkdir(parents=True)
            executor.MISC_DIR.mkdir(parents=True)
            (executor.OUTPUT_DIR / "old.txt").write_text("old", encoding="utf-8")
            (executor.OUTPUT_DIR / "nested").mkdir()
            (executor.OUTPUT_DIR / "nested" / "old.txt").write_text("old", encoding="utf-8")
            (executor.MISC_DIR / "old.txt").write_text("old", encoding="utf-8")

            executor.setup_output_dir()

            self.assertTrue(executor.OUTPUT_DIR.exists())
            self.assertTrue(executor.MISC_DIR.exists())
            self.assertEqual(list(executor.OUTPUT_DIR.iterdir()), [])
            self.assertEqual(list(executor.MISC_DIR.iterdir()), [])

    def test_clear_output_dir_handles_missing_directories(self) -> None:
        temp_dir, output_patch, misc_patch = self.with_temp_dirs()
        with temp_dir, output_patch, misc_patch:
            executor.clear_output_dir()
            self.assertFalse(executor.OUTPUT_DIR.exists())
            self.assertFalse(executor.MISC_DIR.exists())

    def test_collect_output_files_returns_empty_when_output_dir_is_missing(self) -> None:
        temp_dir, output_patch, misc_patch = self.with_temp_dirs()
        with temp_dir, output_patch, misc_patch:
            self.assertEqual(executor.collect_output_files(), [])

    def test_collect_output_files_encodes_regular_files_with_mime_types(self) -> None:
        temp_dir, output_patch, misc_patch = self.with_temp_dirs()
        with temp_dir, output_patch, misc_patch:
            (executor.OUTPUT_DIR / "nested").mkdir(parents=True)
            (executor.OUTPUT_DIR / "nested" / "data.txt").write_text("hello", encoding="utf-8")
            (executor.OUTPUT_DIR / "plot.png").write_bytes(b"\x89PNG")

            files = executor.collect_output_files()

        self.assertEqual([item["name"] for item in files], ["nested/data.txt", "plot.png"])
        text_file = files[0]
        self.assertEqual(base64.b64decode(text_file["content"]), b"hello")
        self.assertEqual(text_file["mime_type"], "text/plain")
        self.assertEqual(text_file["size"], 5)
        self.assertEqual(files[1]["mime_type"], "image/png")

    def test_collect_output_files_reports_symlink_outputs(self) -> None:
        temp_dir, output_patch, misc_patch = self.with_temp_dirs()
        with temp_dir, output_patch, misc_patch:
            outside = Path(temp_dir.name) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            executor.OUTPUT_DIR.mkdir(parents=True)
            (executor.OUTPUT_DIR / "link.txt").symlink_to(outside)

            files = executor.collect_output_files()

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["name"], "link.txt")
        self.assertIsNone(files[0]["content"])
        self.assertIn("Symlink", files[0]["error"])

    def test_collect_output_files_reports_per_file_size_limit(self) -> None:
        temp_dir, output_patch, misc_patch = self.with_temp_dirs()
        with temp_dir, output_patch, misc_patch, mock.patch.object(executor, "MAX_FILE_SIZE", 3):
            executor.OUTPUT_DIR.mkdir(parents=True)
            (executor.OUTPUT_DIR / "big.bin").write_bytes(b"1234")

            files = executor.collect_output_files()

        self.assertEqual(files[0]["name"], "big.bin")
        self.assertIsNone(files[0]["content"])
        self.assertIn("File too large", files[0]["error"])

    def test_collect_output_files_reports_total_size_limit(self) -> None:
        temp_dir, output_patch, misc_patch = self.with_temp_dirs()
        with temp_dir, output_patch, misc_patch, mock.patch.object(executor, "MAX_TOTAL_FILES_SIZE", 5):
            executor.OUTPUT_DIR.mkdir(parents=True)
            (executor.OUTPUT_DIR / "a.bin").write_bytes(b"123")
            (executor.OUTPUT_DIR / "b.bin").write_bytes(b"456")

            files = executor.collect_output_files()

        self.assertEqual(files[0]["name"], "a.bin")
        self.assertIsNotNone(files[0]["content"])
        self.assertEqual(files[1]["name"], "b.bin")
        self.assertIsNone(files[1]["content"])
        self.assertIn("Total file size limit exceeded", files[1]["error"])


class ExecutorProcessErrorInferenceTests(unittest.TestCase):
    def test_decode_process_output_handles_none_bytes_and_text(self) -> None:
        self.assertEqual(executor._decode_process_output(None), "")
        self.assertEqual(executor._decode_process_output("text"), "text")
        self.assertEqual(executor._decode_process_output(b"text"), "text")
        self.assertEqual(executor._decode_process_output(b"\xff"), "\ufffd")

    def test_python_signal_error_uses_signal_name_when_available(self) -> None:
        error, error_type = executor._python_signal_error(-signal.SIGTERM)
        self.assertIn("SIGTERM", error)
        self.assertEqual(error_type, "ProcessSignalError")

    def test_infer_python_error_from_return_code_without_stderr(self) -> None:
        error, error_type = executor._infer_python_error("", 2)
        self.assertEqual(error, "Python process exited with code 2")
        self.assertEqual(error_type, "SystemExit")

    def test_infer_python_error_extracts_exception_type_from_traceback_last_line(self) -> None:
        stderr = "Traceback (most recent call last):\n  ...\nValueError: bad value\n"
        error, error_type = executor._infer_python_error(stderr, 1)
        self.assertEqual(error, stderr.strip())
        self.assertEqual(error_type, "ValueError")

    def test_infer_python_error_extracts_bare_exception_name(self) -> None:
        error, error_type = executor._infer_python_error("CustomError\n", 1)
        self.assertEqual(error, "CustomError")
        self.assertEqual(error_type, "CustomError")

    def test_infer_python_error_falls_back_for_unstructured_stderr(self) -> None:
        error, error_type = executor._infer_python_error("not structured\n", 1)
        self.assertEqual(error, "not structured")
        self.assertEqual(error_type, "PythonExitError")

    def test_infer_python_error_delegates_negative_return_codes_to_signal_error(self) -> None:
        error, error_type = executor._infer_python_error("", -signal.SIGKILL)
        self.assertIn("SIGKILL", error)
        self.assertEqual(error_type, "ProcessSignalError")


class ExecutorExecutionTests(unittest.TestCase):
    def with_temp_dirs(self):
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        return (
            temp_dir,
            mock.patch.object(executor, "OUTPUT_DIR", root / "output"),
            mock.patch.object(executor, "MISC_DIR", root / "misc"),
        )

    def test_execute_bash_captures_stdout_and_successfully_collects_files(self) -> None:
        temp_dir, output_patch, misc_patch = self.with_temp_dirs()
        with temp_dir, output_patch, misc_patch, mock.patch.object(executor, "terminate_residual_processes"):
            executor.OUTPUT_DIR.mkdir(parents=True)
            result = executor.execute_bash("echo hello\nprintf data > \"$OUTPUT_FILE\"".replace("$OUTPUT_FILE", str(executor.OUTPUT_DIR / "data.txt")))

        self.assertEqual(result["stdout"], "hello\n")
        self.assertIsNone(result["error"])
        self.assertEqual(result["files"][0]["name"], "data.txt")
        self.assertEqual(base64.b64decode(result["files"][0]["content"]), b"data")

    def test_execute_bash_reports_nonzero_exit(self) -> None:
        temp_dir, output_patch, misc_patch = self.with_temp_dirs()
        with temp_dir, output_patch, misc_patch, mock.patch.object(executor, "terminate_residual_processes"):
            result = executor.execute_bash("echo bad >&2\nexit 4")

        self.assertIn("bad", result["stderr"])
        self.assertEqual(result["error"], "Bash script exited with code 4")
        self.assertEqual(result["error_type"], "BashExitError")

    def test_execute_code_reports_child_runtime_errors(self) -> None:
        with mock.patch.object(executor, "collect_output_files", return_value=[]), mock.patch.object(
            executor,
            "terminate_residual_processes",
        ):
            result = executor.execute_code("raise RuntimeError('boom')")

        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertIn("RuntimeError: boom", result["error"])

    def test_execute_code_captures_stdout_on_success(self) -> None:
        with mock.patch.object(executor, "collect_output_files", return_value=[]), mock.patch.object(
            executor,
            "terminate_residual_processes",
        ):
            result = executor.execute_code("print('hello from child')")

        self.assertIsNone(result["error"])
        self.assertEqual(result["stdout"], "hello from child\n")

    def test_execute_code_reports_subprocess_creation_errors(self) -> None:
        with mock.patch.object(executor.subprocess, "Popen", side_effect=OSError("cannot start")):
            result = executor.execute_code("print('nope')")

        self.assertEqual(result["error_type"], "OSError")
        self.assertIn("cannot start", result["error"])


class ExecutorMainTests(unittest.TestCase):
    def test_main_emits_value_error_payload_when_code_is_missing(self) -> None:
        stream = io.StringIO()
        argv = ["executor.py", "--lang", "python", "--exec-id", "unit-test"]
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(stream):
            with self.assertRaises(SystemExit) as ctx:
                executor.main()

        self.assertEqual(ctx.exception.code, 1)
        line = stream.getvalue().strip().splitlines()[-1]
        self.assertTrue(line.startswith(executor.RESULT_PREFIX))
        payload = json.loads(line[len(executor.RESULT_PREFIX) :])
        self.assertEqual(payload["error_type"], "ValueError")
        self.assertIn("No code provided", payload["error"])

    def test_main_dispatches_bash_execution_and_cleans_output_dir(self) -> None:
        encoded = base64.b64encode(b"echo ok").decode("ascii")
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            misc_root = Path(temp_dir) / "misc"
            argv = ["executor.py", "--lang", "bash", "--exec-id", "unit-test"]
            with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {"CODE_B64": encoded}, clear=True):
                with mock.patch.object(executor, "OUTPUT_DIR", output_dir), mock.patch.object(executor, "MISC_DIR", misc_root):
                    with mock.patch.object(executor, "terminate_residual_processes"), redirect_stdout(stream):
                        executor.main()

            self.assertTrue(output_dir.exists())
            self.assertEqual(list(output_dir.iterdir()), [])

        line = stream.getvalue().strip().splitlines()[-1]
        payload = json.loads(line[len(executor.RESULT_PREFIX) :])
        self.assertEqual(payload["stdout"], "ok\n")
        self.assertIsNone(payload["error"])


if __name__ == "__main__":
    unittest.main()
