#!/usr/bin/env python3
"""
Shared HTTP client helpers for local verification scripts.

The repository contains several smoke and integration checks that talk to the
gateway over HTTP. Centralizing the common request and authentication logic
keeps those scripts small and avoids subtle behavior drift between them.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


JsonDict = dict[str, Any]


def env_flag(name: str, default: bool = False) -> bool:
    """Parse a boolean feature flag from the environment."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def resolve_token() -> str | None:
    """Resolve the bearer token from environment variables."""
    for env_name in ("API_TOKEN", "API_KEY"):
        value = os.getenv(env_name)
        if value:
            return value

    api_keys = os.getenv("API_KEYS", "")
    if not api_keys:
        return None

    first = api_keys.split(",", 1)[0].strip()
    if ":" in first:
        return first.split(":", 1)[1]
    return first or None


@dataclass(slots=True)
class GatewayClient:
    """Small wrapper around the gateway API used by verification scripts."""

    base_url: str
    token: str | None = None

    @classmethod
    def from_environment(cls, *, base_url: str | None = None) -> "GatewayClient":
        """Build a client from environment defaults used throughout the repo."""
        return cls(
            base_url=(base_url or os.getenv("BASE_URL", "http://localhost:8000")).rstrip("/"),
            token=resolve_token(),
        )

    def request(
        self,
        method: str,
        path: str,
        payload: JsonDict | None = None,
        *,
        timeout: int = 90,
    ) -> tuple[int, JsonDict]:
        """Send an HTTP request and return the status code plus parsed JSON body."""
        data = None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, self._read_json_body(response)
        except urllib.error.HTTPError as exc:
            return exc.code, self._read_json_body(exc)

    @staticmethod
    def _read_json_body(response: Any) -> JsonDict:
        """Read a JSON response body, tolerating empty bodies."""
        body = response.read().decode("utf-8")
        return json.loads(body) if body else {}
