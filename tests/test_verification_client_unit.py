#!/usr/bin/env python3
import io
import json
import os
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

from verification_client import GatewayClient, env_flag, resolve_token


class VerificationClientEnvTests(unittest.TestCase):
    def test_env_flag_uses_default_for_missing_values(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(env_flag("MISSING", default=True))
            self.assertFalse(env_flag("MISSING", default=False))

    def test_env_flag_accepts_truthy_values_and_treats_other_values_as_false(self) -> None:
        for value in ("1", "true", "TRUE", " yes ", "on"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"FLAG": value}, clear=True):
                self.assertTrue(env_flag("FLAG"))
        for value in ("0", "false", "no", "off", "sometimes", ""):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"FLAG": value}, clear=True):
                self.assertFalse(env_flag("FLAG", default=True))

    def test_resolve_token_precedence_and_api_key_parsing(self) -> None:
        with mock.patch.dict(os.environ, {"API_TOKEN": "token", "API_KEY": "key", "API_KEYS": "id:secret"}, clear=True):
            self.assertEqual(resolve_token(), "token")
        with mock.patch.dict(os.environ, {"API_KEY": "key", "API_KEYS": "id:secret"}, clear=True):
            self.assertEqual(resolve_token(), "key")
        with mock.patch.dict(os.environ, {"API_KEYS": "id:secret,second"}, clear=True):
            self.assertEqual(resolve_token(), "secret")
        with mock.patch.dict(os.environ, {"API_KEYS": "secret,second"}, clear=True):
            self.assertEqual(resolve_token(), "secret")
        with mock.patch.dict(os.environ, {"API_KEYS": ""}, clear=True):
            self.assertIsNone(resolve_token())

    def test_client_from_environment_strips_base_url_and_resolves_token(self) -> None:
        with mock.patch.dict(os.environ, {"BASE_URL": "http://localhost:8000/", "API_KEY": "secret"}, clear=True):
            client = GatewayClient.from_environment()

        self.assertEqual(client.base_url, "http://localhost:8000")
        self.assertEqual(client.token, "secret")

    def test_client_from_environment_allows_explicit_base_url_override(self) -> None:
        with mock.patch.dict(os.environ, {"BASE_URL": "http://ignored", "API_KEY": "secret"}, clear=True):
            client = GatewayClient.from_environment(base_url="http://example.test/")

        self.assertEqual(client.base_url, "http://example.test")
        self.assertEqual(client.token, "secret")


class VerificationClientRequestTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, status: int, body: dict | None = None) -> None:
            self.status = status
            self._body = json.dumps(body or {}).encode("utf-8") if body is not None else b""

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return self._body

    def test_request_sends_json_body_and_bearer_auth(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return self.FakeResponse(201, {"ok": True})

        client = GatewayClient(base_url="http://gateway.test", token="secret")
        with mock.patch("verification_client.urllib.request.urlopen", side_effect=fake_urlopen):
            status, body = client.request("POST", "/containers", {"enable_network": False}, timeout=12)

        self.assertEqual(status, 201)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(captured["timeout"], 12)
        request = captured["request"]
        self.assertEqual(request.full_url, "http://gateway.test/containers")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"enable_network": False})

    def test_request_handles_empty_success_bodies(self) -> None:
        client = GatewayClient(base_url="http://gateway.test")
        with mock.patch("verification_client.urllib.request.urlopen", return_value=self.FakeResponse(204, None)):
            status, body = client.request("DELETE", "/containers/ctr-1")

        self.assertEqual(status, 204)
        self.assertEqual(body, {})

    def test_request_returns_http_error_status_and_json_body(self) -> None:
        error = urllib.error.HTTPError(
            "http://gateway.test/containers",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"detail":"Missing Bearer token"}'),
        )
        client = GatewayClient(base_url="http://gateway.test")

        with mock.patch("verification_client.urllib.request.urlopen", side_effect=error):
            status, body = client.request("POST", "/containers", {})

        self.assertEqual(status, 401)
        self.assertEqual(body, {"detail": "Missing Bearer token"})

    def test_read_json_body_tolerates_empty_response(self) -> None:
        self.assertEqual(GatewayClient._read_json_body(SimpleNamespace(read=lambda: b"")), {})
        self.assertEqual(
            GatewayClient._read_json_body(SimpleNamespace(read=lambda: b'{"status":"ok"}')),
            {"status": "ok"},
        )


if __name__ == "__main__":
    unittest.main()
