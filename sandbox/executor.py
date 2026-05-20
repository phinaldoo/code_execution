#!/usr/bin/env python3
"""
Secure Python Code Executor for LLM Sandbox.

This script runs inside the sandbox container. It:
1. Reads base64-encoded source code from the `CODE_B64` environment variable
2. Executes it with stdout/stderr capture
3. Auto-patches matplotlib to save all figures to /tmp/output/
4. Scans /tmp/output/ for generated files
5. Returns a structured JSON response with results + base64-encoded files
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import site
import signal
import shutil
import subprocess  # nosec
import sys
import time
import traceback
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

CODE_PATH = Path("/tmp/code/main.py")  # nosec
OUTPUT_DIR = Path("/tmp/output")  # nosec
MISC_DIR = Path("/tmp/misc")  # nosec
MAX_OUTPUT_LENGTH = 100_000  # Max chars for stdout/stderr
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB per file
MAX_TOTAL_FILES_SIZE = 25 * 1024 * 1024  # 25MB total
RESULT_PREFIX = "__EXECUTOR_RESULT__:"


def get_exec_timeout(default: int = 120) -> int:
    """Get execution timeout from environment variable or return default."""
    raw_value = os.environ.get("EXEC_TIMEOUT", "").strip()
    if not raw_value:
        return default
    try:
        return max(1, int(raw_value))
    except ValueError:
        return default


def build_result(
    *,
    stdout: str = "",
    stderr: str = "",
    error: str | None = None,
    error_type: str | None = None,
    files: list[dict[str, Any]] | None = None,
    execution_time: float = 0,
    install_time: float | None = None,
) -> dict[str, Any]:
    """Build a normalized executor result payload."""
    result: dict[str, Any] = {
        "stdout": truncate_output(stdout),
        "stderr": truncate_output(stderr),
        "error": error,
        "error_type": error_type,
        "files": files or [],
        "execution_time": execution_time,
    }
    if install_time is not None:
        result["install_time"] = install_time
    return result


def setup_output_dir() -> None:
    """Ensure the output directory exists and is clean."""
    clear_output_dir()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MISC_DIR.mkdir(parents=True, exist_ok=True)


def clear_output_dir() -> None:
    """Clear the per-execution output and misc directories after execution."""
    if OUTPUT_DIR.exists():
        for child in OUTPUT_DIR.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    if MISC_DIR.exists():
        shutil.rmtree(MISC_DIR, ignore_errors=True)


def patch_matplotlib() -> None:
    """
    Patch matplotlib to:
    - Use the non-interactive Agg backend
    - Auto-save all figures to OUTPUT_DIR when plt.show() is called
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

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


def collect_output_files() -> list[dict[str, Any]]:
    """
    Scan OUTPUT_DIR for generated files and return them as base64-encoded entries.
    Respects size limits to prevent memory issues.
    """
    files: list[dict[str, Any]] = []
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
        content = filepath.read_bytes()

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


def _read_parent_pid(proc_entry: Path) -> int | None:
    """Read a process parent PID from `/proc/<pid>/status`."""
    try:
        for line in proc_entry.joinpath("status").read_text(encoding="utf-8").splitlines():
            if line.startswith("PPid:"):
                return int(line.split(":", 1)[1].strip())
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
        return None
    return None


def _list_residual_pids() -> list[int]:
    """List descendant process PIDs spawned by the current executor process."""
    proc_root = Path("/proc")
    if not proc_root.exists():
        return []

    current_pid = os.getpid()
    parent_map: dict[int, int] = {}

    for proc_entry in proc_root.iterdir():
        if not proc_entry.name.isdigit():
            continue

        pid = int(proc_entry.name)
        if pid in {1, current_pid}:
            continue

        parent_pid = _read_parent_pid(proc_entry)
        if parent_pid is None:
            continue

        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            continue

        parent_map[pid] = parent_pid

    residual_pids: list[int] = []
    frontier = {current_pid}
    while frontier:
        next_frontier: set[int] = set()
        for pid, parent_pid in parent_map.items():
            if parent_pid not in frontier:
                continue
            residual_pids.append(pid)
            next_frontier.add(pid)
        frontier = next_frontier

    return residual_pids


def terminate_residual_processes() -> None:
    """Terminate any descendant processes left behind by executed user code."""
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


def terminate_process_group(process_group_id: int) -> None:
    """Terminate a process group and any background jobs it still owns."""
    for sig, deadline_seconds in ((signal.SIGTERM, 0.5), (signal.SIGKILL, 0.5)):
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process_group_id, sig)

        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group_id, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.05)


def _decode_process_output(value: str | bytes | None) -> str:
    """Decode subprocess output that may be text, bytes, or absent."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _python_signal_error(returncode: int) -> tuple[str, str]:
    """Build error fields for a Python child terminated by a signal."""
    signum = -returncode
    signal_name = f"signal {signum}"
    with suppress(ValueError):
        signal_name = signal.Signals(signum).name
    return f"Python process terminated by {signal_name}", "ProcessSignalError"


def _infer_python_error(stderr_text: str, returncode: int) -> tuple[str, str]:
    """Infer stable error fields from Python child stderr and exit status."""
    if returncode < 0:
        return _python_signal_error(returncode)

    stderr = stderr_text.strip()
    if not stderr:
        return f"Python process exited with code {returncode}", "SystemExit"

    last_line = next((line.strip() for line in reversed(stderr.splitlines()) if line.strip()), "")
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*):", last_line)
    if match:
        return stderr, match.group(1).split(".")[-1]
    if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", last_line):
        return stderr, last_line.split(".")[-1]
    return stderr, "PythonExitError"


def execute_python_child() -> None:
    """Run user Python in the child process; the parent owns result emission."""
    patch_matplotlib()

    user_site = site.getusersitepackages()
    if user_site and user_site not in sys.path:
        sys.path.append(user_site)

    try:
        code = decode_code_from_environment()
        exec_globals = {
            "__builtins__": __builtins__,
            "__name__": "__main__",
            "__file__": str(CODE_PATH),
        }
        exec(compile(code, "<user_code>", "exec"), exec_globals)  # nosec
    except SystemExit:
        raise
    except BaseException:
        tb_lines = traceback.format_exception(*sys.exc_info())
        filtered = []
        skip = False
        for line in tb_lines:
            if "executor.py" in line:
                skip = True
                continue
            if skip and line.startswith("    "):
                continue
            skip = False
            filtered.append(line)
        sys.stderr.write("".join(filtered).strip() + "\n")
        sys.exit(1)


def execute_code(code: str) -> dict[str, Any]:
    """
    Execute Python code in a child process and capture results.

    Returns a dict with stdout, stderr, files, error info, and timing.
    """
    start_time = time.monotonic()
    error = None
    error_type = None
    stdout_text = ""
    stderr_text = ""
    process: subprocess.Popen[str] | None = None

    try:
        child_env = os.environ.copy()
        child_env["CODE_B64"] = base64.b64encode(code.encode("utf-8")).decode("ascii")
        process = subprocess.Popen(  # nosec
            [sys.executable, str(Path(__file__)), "--python-child"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            text=True,
            start_new_session=True,
        )
        try:
            stdout_text, stderr_text = process.communicate(timeout=get_exec_timeout())
        except subprocess.TimeoutExpired as exc:
            stdout_text = _decode_process_output(exc.stdout)
            stderr_text = _decode_process_output(exc.stderr)
            terminate_process_group(process.pid)
            with suppress(subprocess.TimeoutExpired):
                remaining_stdout, remaining_stderr = process.communicate(timeout=1)
                stdout_text += _decode_process_output(remaining_stdout)
                stderr_text += _decode_process_output(remaining_stderr)
            error = "Python execution timed out"
            error_type = "TimeoutError"

        if error is None and process.returncode != 0:
            error, error_type = _infer_python_error(stderr_text, process.returncode)

    except Exception as exc:
        error = f"Python execution failed: {str(exc)}"
        error_type = type(exc).__name__
        if process is not None:
            terminate_process_group(process.pid)

    execution_time = round(time.monotonic() - start_time, 4)

    terminate_residual_processes()
    files = collect_output_files()

    return build_result(
        stdout=stdout_text,
        stderr=stderr_text,
        error=error,
        error_type=error_type,
        files=files,
        execution_time=execution_time,
    )


def execute_bash(code: str) -> dict[str, Any]:
    """Execute Bash code and capture stdout, stderr, files, and timing."""
    start_time = time.monotonic()
    error = None
    error_type = None
    stdout_text = ""
    stderr_text = ""

    # Write code to a temporary script file inside the tmpfs mount
    script_path = MISC_DIR / "script.sh"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(code, encoding="utf-8")
    script_path.chmod(0o755)

    try:
        process = subprocess.Popen(  # nosec
            ["/bin/bash", str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout_text, stderr_text = process.communicate(timeout=get_exec_timeout())
        finally:
            terminate_process_group(process.pid)

        if process.returncode != 0:
            error = f"Bash script exited with code {process.returncode}"
            error_type = "BashExitError"

    except subprocess.TimeoutExpired as e:
        error = "Bash execution timed out"
        error_type = "TimeoutError"
        stdout_text = e.stdout if isinstance(e.stdout, str) else (e.stdout.decode() if e.stdout else "")
        stderr_text = e.stderr if isinstance(e.stderr, str) else (e.stderr.decode() if e.stderr else "")
        terminate_process_group(process.pid)
    except Exception as e:
        error = f"Bash execution failed: {str(e)}"
        error_type = type(e).__name__
    finally:
        # Cleanup script
        if script_path.exists():
            script_path.unlink()

    execution_time = round(time.monotonic() - start_time, 4)

    terminate_residual_processes()
    files = collect_output_files()

    return build_result(
        stdout=stdout_text,
        stderr=stderr_text,
        error=error,
        error_type=error_type,
        files=files,
        execution_time=execution_time,
    )


def install_pip_packages() -> tuple[str | None, float | None]:
    """Install packages specified in PIP_PACKAGES environment variable."""
    packages_str = os.environ.get("PIP_PACKAGES", "").strip()
    if not packages_str:
        return None, None

    packages = [p.strip() for p in packages_str.split(",") if p.strip()]
    if not packages:
        return None, None

    import importlib
    install_timeout = get_exec_timeout()
    start_install = time.monotonic()
    try:
        # Install to user directory to avoid permission issues
        # --no-cache-dir to keep it clean and fast
        cmd = [sys.executable, "-m", "pip", "install", "--user", "--no-cache-dir", "--quiet"] + packages
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=install_timeout)  # nosec

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


def decode_code_from_environment() -> str:
    """Decode the base64-encoded source code passed in the environment."""
    code_b64 = os.environ.get("CODE_B64", "")
    if not code_b64:
        raise ValueError("No code provided (set CODE_B64 env var)")

    try:
        code = base64.b64decode(code_b64, validate=True).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"Failed to decode CODE_B64: {exc}") from exc

    if not code.strip():
        raise ValueError("Empty code provided")
    return code


def emit_result(result: dict[str, Any]) -> None:
    """Emit the structured executor result with a stable prefix for the gateway."""
    payload = json.dumps(result)
    print(f"{RESULT_PREFIX}{payload}")


def main() -> None:
    """Main entry point for the executor."""
    global OUTPUT_DIR, MISC_DIR

    parser = argparse.ArgumentParser(description="Code Executor")
    parser.add_argument("--lang", type=str, choices=["python", "bash"], default="python", help="Language to execute")
    parser.add_argument("--exec-id", type=str, default=None, help="Unique execution identifier for output isolation")
    parser.add_argument("--python-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.python_child:
        execute_python_child()
        return

    exec_id = args.exec_id or uuid.uuid4().hex[:12]
    MISC_DIR = Path(f"/tmp/misc/{exec_id}")  # nosec

    setup_output_dir()

    if args.lang == "python":
        patch_matplotlib()

        # Dynamic package installation
        install_error, install_time = install_pip_packages()

        # Ensure user site-packages are in sys.path
        user_site = site.getusersitepackages()
        if user_site and user_site not in sys.path:
            sys.path.append(user_site)
    else:
        install_error, install_time = None, None

    try:
        code = decode_code_from_environment()
    except ValueError as exc:
        emit_result(build_result(error=str(exc), error_type="ValueError"))
        sys.exit(1)

    # Execute the code
    if args.lang == "python":
        result = execute_code(code)
    else:
        result = execute_bash(code)

    if install_time is not None:
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
