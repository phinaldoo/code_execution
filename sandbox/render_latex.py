#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import shutil
import subprocess  # nosec
import time
import traceback
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RESULT_PREFIX = "__LATEX_RENDER_RESULT__:"
MAX_TEX_CHARS = int(os.getenv("LATEX_MAX_TEX_CHARS", "250000"))
MAX_INPUT_FILES = int(os.getenv("LATEX_MAX_INPUT_FILES", "20"))
MAX_ASSET_BYTES = int(os.getenv("LATEX_MAX_ASSET_BYTES", str(10 * 1024 * 1024)))
MAX_TOTAL_ASSET_BYTES = int(os.getenv("LATEX_MAX_TOTAL_ASSET_BYTES", str(25 * 1024 * 1024)))
MAX_OUTPUT_BYTES = int(os.getenv("LATEX_MAX_OUTPUT_BYTES", str(25 * 1024 * 1024)))
PDFLATEX_TIMEOUT_SECONDS = int(os.getenv("LATEX_PDFLATEX_TIMEOUT_SECONDS", "90"))


class LatexValidationError(ValueError):
    pass


class LatexExecutionError(RuntimeError):
    def __init__(self, message: str, log_text: str = "") -> None:
        super().__init__(message)
        self.log_text = log_text


@dataclass
class PreparedInput:
    name: str
    content: bytes


def emit_result(payload: dict[str, Any]) -> None:
    print(f"{RESULT_PREFIX}{json.dumps(payload, separators=(',', ':'))}")


def _safe_name(name: str) -> str:
    cleaned = str(name or "").strip()
    if not cleaned:
        raise LatexValidationError("input file name cannot be empty")
    if cleaned in {".", ".."}:
        raise LatexValidationError("input file name is invalid")
    if "/" in cleaned or "\\" in cleaned:
        raise LatexValidationError("input file name must be a same-directory asset name")
    if len(cleaned) > 180:
        raise LatexValidationError("input file name is too long")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise LatexValidationError("input file name contains control characters")
    return cleaned


def _decode_base64(value: str) -> bytes:
    text = str(value or "").strip()
    if "," in text and text.lower().startswith("data:"):
        text = text.split(",", 1)[1]
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise LatexValidationError("input file content is not valid base64") from exc


def parse_request(path: Path) -> tuple[str, str, list[PreparedInput]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise LatexValidationError("request must be a JSON object")

    tex = str(payload.get("tex") or "")
    if not tex.strip():
        raise LatexValidationError("tex is required")
    if len(tex) > MAX_TEX_CHARS:
        raise LatexValidationError(f"tex is too large (>{MAX_TEX_CHARS} characters)")

    job_name = str(payload.get("job_name") or "document").strip() or "document"
    job_name = "".join(ch for ch in job_name if ch.isalnum() or ch in "._-").strip("._-") or "document"
    job_name = job_name[:64]

    input_files_raw = payload.get("input_files") or []
    if not isinstance(input_files_raw, list):
        raise LatexValidationError("input_files must be an array")
    if len(input_files_raw) > MAX_INPUT_FILES:
        raise LatexValidationError(f"too many input_files (max {MAX_INPUT_FILES})")

    total_asset_bytes = 0
    seen: set[str] = set()
    input_files: list[PreparedInput] = []
    for raw in input_files_raw:
        if not isinstance(raw, dict):
            raise LatexValidationError("each input file must be an object")
        name = _safe_name(str(raw.get("file_name") or raw.get("name") or ""))
        if name in seen:
            raise LatexValidationError(f"duplicate input file name: {name}")
        seen.add(name)
        content = _decode_base64(str(raw.get("base64_content") or raw.get("base64") or ""))
        if len(content) > MAX_ASSET_BYTES:
            raise LatexValidationError(f"input file {name} is too large")
        total_asset_bytes += len(content)
        if total_asset_bytes > MAX_TOTAL_ASSET_BYTES:
            raise LatexValidationError("input files exceed total size limit")
        input_files.append(PreparedInput(name=name, content=content))

    return tex, job_name, input_files


def write_request_files(work_dir: Path, tex: str, input_files: list[PreparedInput]) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    for item in input_files:
        target = (work_dir / item.name).resolve()
        try:
            target.relative_to(work_dir.resolve())
        except ValueError as exc:
            raise LatexValidationError("input file resolves outside the workspace") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item.content)

    tex_path = work_dir / "main.tex"
    tex_path.write_text(tex, encoding="utf-8")
    return tex_path


def run_pdflatex(work_dir: Path, tex_path: Path) -> tuple[str, float]:
    if shutil.which("pdflatex") is None:
        raise LatexExecutionError("pdflatex is not installed in the sandbox image")

    combined_log = []
    started = time.monotonic()
    command = [
        "pdflatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        "-no-shell-escape",
        tex_path.name,
    ]

    for pass_number in (1, 2):
        try:
            completed = subprocess.run(  # nosec
                command,
                cwd=work_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PDFLATEX_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            combined_log.append(f"=== pdflatex pass {pass_number} timed out ===\n{stdout}\n{stderr}")
            raise LatexExecutionError(
                f"pdflatex timed out after {PDFLATEX_TIMEOUT_SECONDS} seconds",
                "\n".join(combined_log),
            )

        combined_log.append(
            f"=== pdflatex pass {pass_number} exit {completed.returncode} ===\n"
            f"{completed.stdout or ''}\n{completed.stderr or ''}"
        )
        if completed.returncode != 0:
            raise LatexExecutionError("pdflatex failed", "\n".join(combined_log))

    return "\n".join(combined_log), round(time.monotonic() - started, 4)


def _read_log(work_dir: Path, fallback: str) -> str:
    log_path = work_dir / "main.log"
    if log_path.exists():
        try:
            return log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return fallback
    return fallback


def build_archive(output_dir: Path, job_name: str, tex: str, pdf_path: Path, log_text: str, execution_time: float) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_bytes = pdf_path.read_bytes()
    if len(pdf_bytes) > MAX_OUTPUT_BYTES:
        raise LatexExecutionError("rendered PDF exceeds output size limit")

    archive_path = output_dir / f"{job_name}.zip"
    metadata = {
        "file_name": f"{job_name}.pdf",
        "media_type": "application/pdf",
        "compiler": "pdflatex",
        "compiled_at": datetime.now(timezone.utc).isoformat(),
        "execution_time": execution_time,
        "pdf_size": len(pdf_bytes),
    }
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{job_name}.pdf", pdf_bytes)
        archive.writestr("source/main.tex", tex.encode("utf-8"))
        archive.writestr("logs/pdflatex.log", log_text.encode("utf-8", errors="replace"))
        archive.writestr("metadata.json", json.dumps(metadata, separators=(",", ":")).encode("utf-8"))

    if archive_path.stat().st_size > MAX_OUTPUT_BYTES:
        raise LatexExecutionError("render archive exceeds output size limit")
    return archive_path


def render_from_file(request_path: Path, output_dir: Path) -> dict[str, Any]:
    start = time.monotonic()
    tex, job_name, input_files = parse_request(request_path)
    work_dir = output_dir / "work"
    tex_path = write_request_files(work_dir, tex, input_files)
    log_text = ""
    try:
        command_log, compile_time = run_pdflatex(work_dir, tex_path)
        log_text = _read_log(work_dir, command_log)
    except LatexExecutionError as exc:
        log_text = _read_log(work_dir, exc.log_text)
        raise LatexExecutionError(str(exc), log_text) from exc

    pdf_path = work_dir / "main.pdf"
    if not pdf_path.exists():
        raise LatexExecutionError("pdflatex completed but did not produce a PDF")

    archive_path = build_archive(
        output_dir=output_dir,
        job_name=job_name,
        tex=tex,
        pdf_path=pdf_path,
        log_text=log_text,
        execution_time=compile_time,
    )

    return {
        "file_name": archive_path.name,
        "media_type": "application/zip",
        "output_path": str(archive_path),
        "output_size": archive_path.stat().st_size,
        "execution_time": round(time.monotonic() - start, 4),
        "compiler": "pdflatex",
        "pdf_file_name": f"{job_name}.pdf",
        "log_excerpt": log_text[-4000:],
        "error": None,
        "error_type": None,
    }


def build_error_payload(error: str, error_type: str, *, execution_time: float = 0, log_excerpt: str = "") -> dict[str, Any]:
    return {
        "error": error,
        "error_type": error_type,
        "execution_time": execution_time,
        "log_excerpt": log_excerpt[-4000:] if log_excerpt else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Render LaTeX source to a PDF archive")
    parser.add_argument("--request", required=True, help="Path to render request JSON")
    parser.add_argument("--output-dir", required=True, help="Directory for rendered archive")
    args = parser.parse_args()

    start = time.monotonic()
    try:
        payload = render_from_file(Path(args.request), Path(args.output_dir))
        emit_result(payload)
        return 0
    except (json.JSONDecodeError, LatexValidationError) as exc:
        emit_result(build_error_payload(str(exc), type(exc).__name__, execution_time=round(time.monotonic() - start, 4)))
        return 2
    except LatexExecutionError as exc:
        log_excerpt = getattr(exc, "log_text", "") or ""
        try:
            if not log_excerpt:
                log_excerpt = (Path(args.output_dir) / "work" / "main.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            log_excerpt = ""
        emit_result(
            build_error_payload(
                str(exc),
                type(exc).__name__,
                execution_time=round(time.monotonic() - start, 4),
                log_excerpt=log_excerpt,
            )
        )
        return 1
    except Exception as exc:
        traceback.print_exc()
        emit_result(build_error_payload("latex rendering failed", type(exc).__name__, execution_time=round(time.monotonic() - start, 4)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
