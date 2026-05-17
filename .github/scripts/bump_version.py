#!/usr/bin/env python3
"""Bump the code execution gateway release version."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
VERSION_FILE = Path("gateway/version.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bump", choices=("patch", "minor", "major"))
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root. Defaults to the current working directory.",
    )
    return parser.parse_args()


def parse_semver(value: str) -> tuple[int, int, int]:
    normalized = value.strip()
    if normalized.startswith("v"):
        normalized = normalized[1:]
    match = SEMVER_RE.fullmatch(normalized)
    if not match:
        raise ValueError(f"Unsupported version '{value}'. Expected strict semver like 1.2.3.")
    return tuple(int(part) for part in match.groups())


def bump_semver(current: str, bump: str) -> str:
    major, minor, patch = parse_semver(current)
    if bump == "patch":
        patch += 1
    elif bump == "minor":
        minor += 1
        patch = 0
    else:
        major += 1
        minor = 0
        patch = 0
    return f"{major}.{minor}.{patch}"


def resolve_root(root: Path) -> Path:
    resolved = root.resolve()
    if not (resolved / VERSION_FILE).exists():
        raise FileNotFoundError(f"Repository root '{resolved}' is missing {VERSION_FILE}")
    return resolved


def read_version_payload(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return payload


def read_version(path: Path) -> str:
    payload = read_version_payload(path)
    raw_version = payload.get("version")
    raw_tag = payload.get("tag")
    if not isinstance(raw_version, str):
        raise ValueError(f"{path} is missing a string 'version' field.")
    version = ".".join(str(part) for part in parse_semver(raw_version))
    expected_tag = f"v{version}"
    if raw_tag is not None and raw_tag != expected_tag:
        raise ValueError(f"{path} has mismatched version/tag: {raw_version!r} and {raw_tag!r}.")
    return version


def write_version(path: Path, version: str) -> None:
    payload = read_version_payload(path)
    payload["version"] = version
    payload["tag"] = f"v{version}"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def main() -> int:
    args = parse_args()
    try:
        root = resolve_root(args.root)
        version_path = root / VERSION_FILE
        current_version = read_version(version_path)
        next_version = bump_semver(current_version, args.bump)
        write_version(version_path, next_version)
    except Exception as exc:  # pragma: no cover - exercised by workflow usage
        print(str(exc), file=sys.stderr)
        return 1

    print(f"version={next_version}")
    print(f"tag=v{next_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
