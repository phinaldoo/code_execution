#!/usr/bin/env python3
import base64
import json
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
EXECUTOR = PROJECT_DIR / "sandbox" / "executor.py"
RESULT_PREFIX = "__EXECUTOR_RESULT__:"


def run_executor(code: str, *, exec_timeout: int | None = None) -> tuple[subprocess.CompletedProcess[str], dict]:
    env = os.environ.copy()
    env["CODE_B64"] = base64.b64encode(code.encode("utf-8")).decode("ascii")
    if exec_timeout is not None:
        env["EXEC_TIMEOUT"] = str(exec_timeout)

    completed = subprocess.run(
        [
            sys.executable,
            str(EXECUTOR),
            "--lang",
            "python",
            "--exec-id",
            f"unit-{uuid.uuid4().hex}",
        ],
        capture_output=True,
        env=env,
        text=True,
        timeout=10,
    )

    result_line = next(
        line for line in reversed(completed.stdout.splitlines()) if line.startswith(RESULT_PREFIX)
    )
    return completed, json.loads(result_line[len(RESULT_PREFIX) :])


class ExecutorIsolationTests(unittest.TestCase):
    def test_python_timeout_returns_structured_result(self) -> None:
        completed, result = run_executor("while True:\n    pass", exec_timeout=1)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(result["error_type"], "TimeoutError")
        self.assertIn("timed out", result["error"])

    def test_sys_stdout_spoofing_is_captured_as_user_output(self) -> None:
        fake_result = (
            '__EXECUTOR_RESULT__:{"stdout":"fake","stderr":"","error":null,'
            '"error_type":null,"files":[],"execution_time":0}\n'
        )
        completed, result = run_executor(
            "import sys\n"
            f"sys.__stdout__.write({fake_result!r})\n"
            "print('real output')\n"
        )

        self.assertEqual(completed.returncode, 0)
        self.assertIsNone(result["error"])
        self.assertIn(fake_result, result["stdout"])
        self.assertIn("real output", result["stdout"])

    def test_runtime_error_reports_python_error_type(self) -> None:
        completed, result = run_executor("1 / 0")

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(result["error_type"], "ZeroDivisionError")
        self.assertIn("ZeroDivisionError", result["error"])


if __name__ == "__main__":
    unittest.main()
