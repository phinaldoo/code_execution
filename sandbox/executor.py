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

import argparse
import base64
import io
import json
import mimetypes
import os
import signal
import subprocess
import shutil
import sys
import time
import traceback
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# --- Configuration ---
CODE_PATH = Path("/tmp/code/main.py")
# Module-level defaults, updated in main() with per-execution scoping
OUTPUT_DIR = Path("/tmp/output")
MISC_DIR = Path("/tmp/misc")
MAX_OUTPUT_LENGTH = 100_000  # Max chars for stdout/stderr
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB per file
MAX_TOTAL_FILES_SIZE = 25 * 1024 * 1024  # 25MB total
RESULT_PREFIX = "__EXECUTOR_RESULT__:"


def get_exec_timeout(default: int = 120) -> int:
    raw_value = os.environ.get("EXEC_TIMEOUT", "").strip()
    if not raw_value:
        return default
    try:
        return max(1, int(raw_value))
    except ValueError:
        return default


def setup_output_dir():
    """Ensure the output directory exists and is clean."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    clear_output_dir()


def clear_output_dir():
    """Clear the per-execution output and misc directories after execution."""
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    if MISC_DIR.exists():
        shutil.rmtree(MISC_DIR, ignore_errors=True)
    # Remove empty parent dirs if possible
    for parent in (OUTPUT_DIR.parent, MISC_DIR.parent):
        try:
            parent.rmdir()
        except OSError:
            pass


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

    output_root = OUTPUT_DIR.resolve()

    for filepath in sorted(OUTPUT_DIR.rglob("*")):
        if not filepath.is_file():
            continue

        relative_name = str(filepath.relative_to(OUTPUT_DIR))
        if filepath.is_symlink():
            files.append({
                "name": relative_name,
                "content": None,
                "mime_type": "application/octet-stream",
                "error": "Symlink outputs are not supported",
                "size": 0,
            })
            continue

        try:
            resolved_path = filepath.resolve(strict=True)
            resolved_path.relative_to(output_root)
        except (FileNotFoundError, RuntimeError, ValueError):
            files.append({
                "name": relative_name,
                "content": None,
                "mime_type": "application/octet-stream",
                "error": "Output file resolves outside the sandbox output directory",
                "size": 0,
            })
            continue

        file_size = filepath.stat().st_size
        if file_size > MAX_FILE_SIZE:
            files.append({
                "name": relative_name,
                "content": None,
                "mime_type": "application/octet-stream",
                "error": f"File too large ({file_size} bytes, max {MAX_FILE_SIZE})",
                "size": file_size,
            })
            continue

        if total_size + file_size > MAX_TOTAL_FILES_SIZE:
            files.append({
                "name": relative_name,
                "content": None,
                "mime_type": "application/octet-stream",
                "error": "Total file size limit exceeded",
                "size": file_size,
            })
            continue

        total_size += file_size

        # Detect MIME type
        mime_type, _ = mimetypes.guess_type(relative_name)
        if mime_type is None:
            mime_type = "application/octet-stream"

        # Read and encode
        with open(filepath, "rb") as f:
            content = f.read()

        files.append({
            "name": relative_name,
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


def _list_residual_pids() -> list[int]:
    current_pid = os.getpid()
    pids: list[int] = []

    for proc_entry in Path("/proc").iterdir():
        if not proc_entry.name.isdigit():
            continue

        pid = int(proc_entry.name)
        if pid in {1, current_pid}:
            continue

        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            continue
        pids.append(pid)

    return pids


def terminate_residual_processes() -> None:
    # NOTE: This kills ALL processes except PID 1 and ourselves. This is safe
    # because the gateway serializes execution per container (one exec at a time).
    residual_pids = _list_residual_pids()
    if not residual_pids:
        return

    for sig, deadline_seconds in ((signal.SIGTERM, 0.5), (signal.SIGKILL, 0.5)):
        for pid in residual_pids:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                continue

        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            remaining = []
            for pid in residual_pids:
                try:
                    os.kill(pid, 0)
                    remaining.append(pid)
                except (ProcessLookupError, PermissionError):
                    continue

            if not remaining:
                return

            residual_pids = remaining
            time.sleep(0.05)


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

    terminate_residual_processes()
    files = collect_output_files()

    return {
        "stdout": truncate_output(stdout_capture.getvalue()),
        "stderr": truncate_output(stderr_capture.getvalue()),
        "error": error,
        "error_type": error_type,
        "files": files,
        "execution_time": execution_time,
    }


def execute_bash(code: str) -> dict:
    """
    Execute the given Bash code and capture results.
    """
    start_time = time.monotonic()
    error = None
    error_type = None

    # Write code to a temporary script file inside the tmpfs mount
    script_path = MISC_DIR / "script.sh"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(code, encoding="utf-8")
    script_path.chmod(0o755)

    try:
        # Run the script
        result = subprocess.run(
            ["/bin/bash", str(script_path)],
            capture_output=True,
            text=True,
            timeout=get_exec_timeout(),
        )
        
        stdout_text = result.stdout
        stderr_text = result.stderr
        
        if result.returncode != 0:
            error = f"Bash script exited with code {result.returncode}"
            error_type = "BashExitError"

    except subprocess.TimeoutExpired as e:
        error = "Bash execution timed out"
        error_type = "TimeoutError"
        stdout_text = e.stdout.decode() if e.stdout else ""
        stderr_text = e.stderr.decode() if e.stderr else ""
    except Exception as e:
        error = f"Bash execution failed: {str(e)}"
        error_type = type(e).__name__
        stdout_text = ""
        stderr_text = ""
    finally:
        # Cleanup script
        if script_path.exists():
            script_path.unlink()

    execution_time = round(time.monotonic() - start_time, 4)

    terminate_residual_processes()
    files = collect_output_files()

    return {
        "stdout": truncate_output(stdout_text),
        "stderr": truncate_output(stderr_text),
        "error": error,
        "error_type": error_type,
        "files": files,
        "execution_time": execution_time,
    }


def install_pip_packages():
    """Install packages specified in PIP_PACKAGES environment variable."""
    packages_str = os.environ.get("PIP_PACKAGES", "").strip()
    if not packages_str:
        return None, None

    packages = [p.strip() for p in packages_str.split(",") if p.strip()]
    if not packages:
        return None, None

    import subprocess
    import importlib
    install_timeout = get_exec_timeout()
    start_install = time.monotonic()
    try:
        # Install to user directory to avoid permission issues
        # --no-cache-dir to keep it clean and fast
        cmd = [sys.executable, "-m", "pip", "install", "--user", "--no-cache-dir", "--quiet"] + packages
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=install_timeout)
        
        install_time = round(time.monotonic() - start_install, 2)
        
        # Invalidate caches so new packages are found
        importlib.invalidate_caches()
        
        if result.returncode != 0:
            error_msg = f"Pip install failed with code {result.returncode}:\n{result.stderr or result.stdout}"
            return error_msg, install_time
            
        return None, install_time
    except subprocess.TimeoutExpired:
        return f"Pip install timed out after {install_timeout} seconds", round(time.monotonic() - start_install, 2)
    except Exception as e:
        return f"Pip install exception: {str(e)}", round(time.monotonic() - start_install, 2)


def emit_result(result: dict) -> None:
    payload = json.dumps(result)
    print(f"{RESULT_PREFIX}{payload}")


def main():
    """Main entry point for the executor."""
    global OUTPUT_DIR, MISC_DIR

    parser = argparse.ArgumentParser(description="Code Executor")
    parser.add_argument("--lang", type=str, choices=["python", "bash"], default="python", help="Language to execute")
    parser.add_argument("--exec-id", type=str, default=None, help="Unique execution identifier for output isolation")
    args = parser.parse_args()

    exec_id = args.exec_id or uuid.uuid4().hex[:12]
    OUTPUT_DIR = Path(f"/tmp/output/{exec_id}")
    MISC_DIR = Path(f"/tmp/misc/{exec_id}")

    setup_output_dir()

    if args.lang == "python":
        patch_matplotlib()

        # Dynamic package installation
        install_error, install_time = install_pip_packages()

        # Ensure user site-packages are in sys.path
        import site
        user_site = site.getusersitepackages()
        if user_site and user_site not in sys.path:
            sys.path.append(user_site)
    else:
        install_error, install_time = None, None

    # Read code from base64-encoded environment variable
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
            emit_result(result)
            sys.exit(1)
    else:
        result = {
            "stdout": "",
            "stderr": "",
            "error": "No code provided (set CODE_B64 env var)",
            "error_type": "ValueError",
            "files": [],
            "execution_time": 0,
        }
        emit_result(result)
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
        emit_result(result)
        sys.exit(1)

    # Execute the code
    if args.lang == "python":
        result = execute_code(code)
    elif args.lang == "bash":
        result = execute_bash(code)

    # Add install info if any for python
    if install_time:
        result["install_time"] = install_time
    
    if install_error:
        # If install failed, we prepend the error to stderr
        result["stderr"] = f"--- PIP INSTALL ERROR ---\n{install_error}\n------------------------\n" + result["stderr"]
        if not result["error"]:
            result["error"] = "Pip installation failed"
            result["error_type"] = "InstallationError"

    # Clean up output directory after collecting files
    clear_output_dir()

    # Output as JSON — stdout was captured, so real stdout is clean
    emit_result(result)


if __name__ == "__main__":
    main()
