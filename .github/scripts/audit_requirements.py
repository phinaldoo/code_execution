#!/usr/bin/env python3
"""Run pip-audit with repository-scoped vulnerability exceptions."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any


DEFAULT_EXCEPTIONS_FILE = Path(".github/vulnerability-exceptions.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requirements", type=Path, help="Requirements file to audit.")
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=DEFAULT_EXCEPTIONS_FILE,
        help=f"Exceptions JSON file. Defaults to {DEFAULT_EXCEPTIONS_FILE}.",
    )
    return parser.parse_args()


def load_json_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return payload


def active_exception_ids(path: Path, requirements: Path) -> list[str]:
    payload = load_json_object(path)
    exceptions = payload.get("exceptions")
    if not isinstance(exceptions, list):
        raise ValueError(f"{path} must contain an 'exceptions' list.")

    today = date.today()
    requirements_name = requirements.as_posix()
    ignored: list[str] = []

    for idx, exception in enumerate(exceptions):
        if not isinstance(exception, dict):
            raise ValueError(f"{path} exception #{idx + 1} must be an object.")

        vuln_id = exception.get("id")
        scoped_requirements = exception.get("requirements")
        expires = exception.get("expires")
        reason = exception.get("reason")

        if not isinstance(vuln_id, str) or not vuln_id.strip():
            raise ValueError(f"{path} exception #{idx + 1} is missing a string 'id'.")
        if not isinstance(scoped_requirements, list) or not all(
            isinstance(item, str) for item in scoped_requirements
        ):
            raise ValueError(
                f"{path} exception {vuln_id} must include a string 'requirements' list."
            )
        if not isinstance(expires, str):
            raise ValueError(f"{path} exception {vuln_id} is missing a string 'expires'.")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{path} exception {vuln_id} is missing a string 'reason'.")

        expiry = date.fromisoformat(expires)
        if expiry < today:
            raise ValueError(f"{path} exception {vuln_id} expired on {expires}.")

        if requirements_name in scoped_requirements:
            ignored.append(vuln_id)

    return ignored


def main() -> int:
    args = parse_args()
    try:
        ignored = active_exception_ids(args.exceptions, args.requirements)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    command = [sys.executable, "-m", "pip_audit"]
    for vuln_id in ignored:
        command.extend(["--ignore-vuln", vuln_id])
    command.extend(["-r", args.requirements.as_posix()])
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
