#!/usr/bin/env python3
"""
Secure Python Code Executor for LLM Sandbox.

This script runs inside the sandbox container. It:
1. Reads Python code from a mounted file (/tmp/code/main.py)
2. Executes it with stdout/stderr capture
3. Auto-patches matplotlib to save all figures to /tmp/output/
4. Scans /tmp/output/ for generated files
5. Returns a structured JSON response with results + base64-encoded files
"""

import base64
import io
import json
import mimetypes
import os
import signal
import sys
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# --- Configuration ---
CODE_PATH = Path("/tmp/code/main.py")
OUTPUT_DIR = Path("/tmp/output")
MAX_OUTPUT_LENGTH = 100_000  # Max chars for stdout/stderr
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB per file
MAX_TOTAL_FILES_SIZE = 100 * 1024 * 1024  # 100MB total


def setup_output_dir():
    """Ensure the output directory exists and is clean."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def patch_matplotlib():
    """
    Patch matplotlib to:
    - Use the non-interactive Agg backend
    - Auto-save all figures to OUTPUT_DIR when plt.show() is called
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        _original_show = plt.show
        _figure_counter = [0]

        def patched_show(*args, **kwargs):
            """Save all open figures to OUTPUT_DIR instead of displaying."""
            for fig_num in plt.get_fignums():
                fig = plt.figure(fig_num)
                _figure_counter[0] += 1
                filename = f"figure_{_figure_counter[0]}.png"
                filepath = OUTPUT_DIR / filename
                fig.savefig(
                    str(filepath),
                    format="png",
                    dpi=150,
                    bbox_inches="tight",
                    facecolor=fig.get_facecolor(),
                    edgecolor="none",
                )
            plt.close("all")

        plt.show = patched_show

    except ImportError:
        # matplotlib not available — that's fine
        pass


def collect_output_files():
    """
    Scan OUTPUT_DIR for generated files and return them as base64-encoded entries.
    Respects size limits to prevent memory issues.
    """
    files = []
    total_size = 0

    if not OUTPUT_DIR.exists():
        return files

    for filepath in sorted(OUTPUT_DIR.rglob("*")):
        if not filepath.is_file():
            continue

        file_size = filepath.stat().st_size
        if file_size > MAX_FILE_SIZE:
            files.append({
                "name": filepath.name,
                "content": None,
                "mime_type": "application/octet-stream",
                "error": f"File too large ({file_size} bytes, max {MAX_FILE_SIZE})",
                "size": file_size,
            })
            continue

        if total_size + file_size > MAX_TOTAL_FILES_SIZE:
            files.append({
                "name": filepath.name,
                "content": None,
                "mime_type": "application/octet-stream",
                "error": "Total file size limit exceeded",
                "size": file_size,
            })
            continue

        total_size += file_size

        # Detect MIME type
        mime_type, _ = mimetypes.guess_type(filepath.name)
        if mime_type is None:
            mime_type = "application/octet-stream"

        # Read and encode
        with open(filepath, "rb") as f:
            content = f.read()

        files.append({
            "name": filepath.name,
            "content": base64.b64encode(content).decode("ascii"),
            "mime_type": mime_type,
            "size": file_size,
        })

    return files


def truncate_output(text: str, max_length: int = MAX_OUTPUT_LENGTH) -> str:
    """Truncate output if it exceeds max length."""
    if len(text) > max_length:
        truncated_msg = f"\n\n... [OUTPUT TRUNCATED — {len(text)} chars total, showing first {max_length}]"
        return text[:max_length] + truncated_msg
    return text


def execute_code(code: str) -> dict:
    """
    Execute the given Python code and capture results.

    Returns a dict with stdout, stderr, files, error info, and timing.
    """
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()

    start_time = time.monotonic()
    error = None
    error_type = None

    try:
        # Create a clean execution namespace
        exec_globals = {
            "__builtins__": __builtins__,
            "__name__": "__main__",
            "__file__": str(CODE_PATH),
        }

        # Redirect stdout/stderr and execute
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            exec(compile(code, "<user_code>", "exec"), exec_globals)

    except SyntaxError as e:
        error_type = "SyntaxError"
        error = f"SyntaxError: {e.msg} (line {e.lineno}, col {e.offset})"
    except SystemExit as e:
        error_type = "SystemExit"
        error = f"SystemExit with code: {e.code}"
    except Exception:
        error_type = type(sys.exc_info()[1]).__name__
        # Format nice traceback but filter out executor frames
        tb_lines = traceback.format_exception(*sys.exc_info())
        # Remove frames from this executor script
        filtered = []
        skip = False
        for line in tb_lines:
            if 'executor.py' in line:
                skip = True
                continue
            if skip and line.startswith("  "):
                continue
            skip = False
            filtered.append(line)
        error = "".join(filtered).strip()

    execution_time = round(time.monotonic() - start_time, 4)

    # Collect any generated files
    files = collect_output_files()

    return {
        "stdout": truncate_output(stdout_capture.getvalue()),
        "stderr": truncate_output(stderr_capture.getvalue()),
        "error": error,
        "error_type": error_type,
        "files": files,
        "execution_time": execution_time,
    }


def main():
    """Main entry point for the executor."""
    setup_output_dir()
    patch_matplotlib()

    # Read code from base64-encoded environment variable (preferred)
    # or fall back to file-based loading
    code_b64 = os.environ.get("CODE_B64", "")
    
    if code_b64:
        try:
            code = base64.b64decode(code_b64).decode("utf-8")
        except Exception as e:
            result = {
                "stdout": "",
                "stderr": "",
                "error": f"Failed to decode CODE_B64: {e}",
                "error_type": "ValueError",
                "files": [],
                "execution_time": 0,
            }
            print(json.dumps(result))
            sys.exit(1)
    elif CODE_PATH.exists():
        code = CODE_PATH.read_text(encoding="utf-8")
    else:
        result = {
            "stdout": "",
            "stderr": "",
            "error": "No code provided (set CODE_B64 env var or mount code at /tmp/code/main.py)",
            "error_type": "FileNotFoundError",
            "files": [],
            "execution_time": 0,
        }
        print(json.dumps(result))
        sys.exit(1)

    if not code.strip():
        result = {
            "stdout": "",
            "stderr": "",
            "error": "Empty code provided",
            "error_type": "ValueError",
            "files": [],
            "execution_time": 0,
        }
        print(json.dumps(result))
        sys.exit(1)

    # Execute the code
    result = execute_code(code)

    # Output as JSON — stdout was captured, so real stdout is clean
    print(json.dumps(result))


if __name__ == "__main__":
    main()

