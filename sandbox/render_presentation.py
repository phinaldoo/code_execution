#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from slide_renderer.config import SETTINGS, validate_runtime_configuration
from slide_renderer.models import RenderRequest
from slide_renderer.render_service import (
    RenderExecutionError,
    RenderValidationError,
    render_presentation,
    validate_render_environment,
)

RESULT_PREFIX = "__RENDER_RESULT__:"


def emit_result(payload: dict[str, Any]) -> None:
    """Emit a structured render result for the gateway parser."""
    print(f"{RESULT_PREFIX}{json.dumps(payload, separators=(',', ':'))}")


def build_error_payload(error: str, error_type: str, *, execution_time: float = 0) -> dict[str, Any]:
    """Build a normalized error payload."""
    return {
        "error": error,
        "error_type": error_type,
        "execution_time": execution_time,
    }


async def render_from_file(request_path: Path, output_dir: Path) -> dict[str, Any]:
    """Read a render request JSON file and write the rendered archive to output_dir."""
    start = time.monotonic()
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    request = RenderRequest.model_validate(payload)

    validate_runtime_configuration()
    validate_render_environment()

    result = await render_presentation(request)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / result.file_name
    output_path.write_bytes(result.content)

    return {
        "file_name": result.file_name,
        "rendering_version": result.rendering_version.value,
        "media_type": result.media_type,
        "slide_count": result.slide_count,
        "output_path": str(output_path),
        "output_size": len(result.content),
        "execution_time": round(time.monotonic() - start, 4),
        "error": None,
        "error_type": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Render HTML slides to a PPTX archive")
    parser.add_argument("--request", required=True, help="Path to render request JSON")
    parser.add_argument("--output-dir", required=True, help="Directory for rendered archive")
    args = parser.parse_args()

    start = time.monotonic()
    try:
        payload = asyncio.run(
            render_from_file(Path(args.request), Path(args.output_dir))
        )
        emit_result(payload)
        return 0
    except (json.JSONDecodeError, ValidationError, RenderValidationError, ValueError) as exc:
        emit_result(
            build_error_payload(
                str(exc),
                type(exc).__name__,
                execution_time=round(time.monotonic() - start, 4),
            )
        )
        return 2
    except RenderExecutionError as exc:
        emit_result(
            build_error_payload(
                str(exc),
                type(exc).__name__,
                execution_time=round(time.monotonic() - start, 4),
            )
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        emit_result(
            build_error_payload(
                "rendering failed",
                type(exc).__name__,
                execution_time=round(time.monotonic() - start, 4),
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

