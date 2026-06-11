#!/usr/bin/env python3
import asyncio
import base64
import json
import os
import tarfile
import unittest
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException
from pydantic import ValidationError

import sys


PROJECT_DIR = Path(__file__).resolve().parent.parent
GATEWAY_DIR = PROJECT_DIR / "gateway"
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import app as gateway_app
from state import InMemoryStateBackend, SessionInfo


class GatewayPrimitiveHelperTests(unittest.TestCase):
    def test_str_to_bool_uses_default_for_missing_or_blank_values(self) -> None:
        self.assertTrue(gateway_app.str_to_bool(None, default=True))
        self.assertFalse(gateway_app.str_to_bool(None, default=False))
        self.assertTrue(gateway_app.str_to_bool("", default=True))
        self.assertFalse(gateway_app.str_to_bool("  ", default=False))
        for value in ("1", "true", "TRUE", " yes ", "on"):
            self.assertTrue(gateway_app.str_to_bool(value, default=False))
        for value in ("0", "false", "no", "off", "unexpected"):
            self.assertFalse(gateway_app.str_to_bool(value, default=True))

    def test_split_csv_strips_empty_items(self) -> None:
        self.assertEqual(gateway_app.split_csv(None), [])
        self.assertEqual(gateway_app.split_csv(""), [])
        self.assertEqual(gateway_app.split_csv(" alpha, beta ,, gamma "), ["alpha", "beta", "gamma"])

    def test_int_from_env_reads_first_configured_name_and_clamps_minimum(self) -> None:
        with mock.patch.dict(os.environ, {"PRIMARY": "0", "FALLBACK": "99"}, clear=False):
            self.assertEqual(gateway_app.int_from_env(("PRIMARY", "FALLBACK"), 12, min_value=5), 5)

    def test_int_from_env_uses_default_for_invalid_values(self) -> None:
        with mock.patch.dict(os.environ, {"BROKEN_INT": "not-an-int"}, clear=False):
            self.assertEqual(gateway_app.int_from_env("BROKEN_INT", 12, min_value=5), 12)

    def test_int_from_env_uses_default_when_missing(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gateway_app.int_from_env("MISSING_INT", 42, min_value=1), 42)

    def test_resolve_slide_rendering_version_prefers_explicit_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "SLIDE_RENDERING_VERSION": " V2 ",
                "RENDERING_VERSION": "v1",
                "ACTIVE_RENDERING_VERSION": "v1",
                "BETA": "0",
            },
            clear=False,
        ):
            self.assertEqual(gateway_app.resolve_slide_rendering_version(), "v2")

    def test_resolve_slide_rendering_version_uses_beta_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"BETA": "yes"}, clear=True):
            self.assertEqual(gateway_app.resolve_slide_rendering_version(), "v2")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gateway_app.resolve_slide_rendering_version(), "v1")

    def test_auth_mode_summary_reports_configured_modes(self) -> None:
        with mock.patch.object(gateway_app, "JWT_SECRET", None), mock.patch.object(gateway_app, "STATIC_API_KEYS", []):
            self.assertEqual(gateway_app.auth_mode_summary(), "disabled")
        with mock.patch.object(gateway_app, "JWT_SECRET", "secret"), mock.patch.object(gateway_app, "STATIC_API_KEYS", []):
            self.assertEqual(gateway_app.auth_mode_summary(), "jwt")
        with mock.patch.object(gateway_app, "JWT_SECRET", None), mock.patch.object(
            gateway_app,
            "STATIC_API_KEYS",
            [gateway_app.StaticApiKey(key_id="one", secret="secret")],
        ):
            self.assertEqual(gateway_app.auth_mode_summary(), "api_key")
        with mock.patch.object(gateway_app, "JWT_SECRET", "secret"), mock.patch.object(
            gateway_app,
            "STATIC_API_KEYS",
            [gateway_app.StaticApiKey(key_id="one", secret="secret")],
        ):
            self.assertEqual(gateway_app.auth_mode_summary(), "jwt+api_key")

    def test_docker_host_hostname_extracts_and_normalizes_hosts(self) -> None:
        self.assertIsNone(gateway_app.docker_host_hostname(""))
        self.assertIsNone(gateway_app.docker_host_hostname("unix:///var/run/docker.sock"))
        self.assertEqual(gateway_app.docker_host_hostname("tcp://Docker-Proxy:2376"), "docker-proxy")

    def test_image_reference_is_immutable_detects_digests_and_non_latest_tags(self) -> None:
        self.assertTrue(gateway_app.image_reference_is_immutable("repo/image@sha256:" + "a" * 64))
        self.assertTrue(gateway_app.image_reference_is_immutable("registry.local:5000/repo/image:1.2.3"))
        self.assertFalse(gateway_app.image_reference_is_immutable("repo/image"))
        self.assertFalse(gateway_app.image_reference_is_immutable("repo/image:latest"))

    def test_strong_sandbox_runtime_configured_matches_allowed_runtime_names(self) -> None:
        with mock.patch.object(gateway_app, "SANDBOX_RUNTIME", ""):
            self.assertFalse(gateway_app.strong_sandbox_runtime_configured())
        with mock.patch.object(gateway_app, "SANDBOX_RUNTIME", "runsc"), mock.patch.object(
            gateway_app,
            "STRONG_SANDBOX_RUNTIMES",
            ["runsc", "kata"],
        ):
            self.assertTrue(gateway_app.strong_sandbox_runtime_configured())
        with mock.patch.object(gateway_app, "SANDBOX_RUNTIME", "runc"), mock.patch.object(
            gateway_app,
            "STRONG_SANDBOX_RUNTIMES",
            ["runsc", "kata"],
        ):
            self.assertFalse(gateway_app.strong_sandbox_runtime_configured())

    def test_optional_number_parsers_return_defaults_for_missing_or_invalid_values(self) -> None:
        self.assertEqual(gateway_app.parse_optional_float("3.5"), 3.5)
        self.assertEqual(gateway_app.parse_optional_float("broken", 1.25), 1.25)
        self.assertIsNone(gateway_app.parse_optional_float(""))
        self.assertEqual(gateway_app.parse_optional_int("7"), 7)
        self.assertEqual(gateway_app.parse_optional_int("broken", 9), 9)
        self.assertEqual(gateway_app.parse_optional_int("", 4), 4)

    def test_principal_scope_includes_tenant_when_present(self) -> None:
        self.assertEqual(
            gateway_app.principal_scope(gateway_app.AuthContext("subject", None, "api_key")),
            "subject:-",
        )
        self.assertEqual(
            gateway_app.principal_scope(gateway_app.AuthContext("subject", "tenant", "jwt")),
            "subject:tenant",
        )


class GatewaySessionHelperTests(unittest.TestCase):
    def _session(self, **overrides) -> SessionInfo:
        values = {
            "created_at": 10.0,
            "last_activity": 20.0,
            "network_enabled": False,
            "owner_subject": "subject-1",
            "owner_tenant": None,
            "docker_daemon_id": "daemon-local",
            "expires_at": 100.0,
            "execution_count": 0,
        }
        values.update(overrides)
        return SessionInfo(**values)

    def test_session_is_local_when_no_daemon_id_was_recorded(self) -> None:
        with mock.patch.object(gateway_app, "local_docker_daemon_id", None):
            self.assertTrue(gateway_app.session_is_local(self._session(docker_daemon_id=None)))

    def test_session_is_not_local_when_local_daemon_identity_is_missing(self) -> None:
        with mock.patch.object(gateway_app, "local_docker_daemon_id", None):
            self.assertFalse(gateway_app.session_is_local(self._session(docker_daemon_id="daemon-a")))

    def test_session_is_local_only_for_matching_daemon_id(self) -> None:
        with mock.patch.object(gateway_app, "local_docker_daemon_id", "daemon-local"):
            self.assertTrue(gateway_app.session_is_local(self._session(docker_daemon_id="daemon-local")))
            self.assertFalse(gateway_app.session_is_local(self._session(docker_daemon_id="daemon-other")))

    def test_hard_and_idle_expiration_checks_are_boundary_aware(self) -> None:
        self.assertFalse(gateway_app.session_hard_expired(self._session(expires_at=None), now=999.0))
        self.assertFalse(gateway_app.session_hard_expired(self._session(expires_at=100.0), now=99.9))
        self.assertTrue(gateway_app.session_hard_expired(self._session(expires_at=100.0), now=100.0))
        with mock.patch.object(gateway_app, "SESSION_TIMEOUT_SECONDS", 30):
            self.assertFalse(gateway_app.session_idle_expired(self._session(last_activity=70.0), now=100.0))
            self.assertTrue(gateway_app.session_idle_expired(self._session(last_activity=69.0), now=100.0))

    def test_session_is_active_rejects_idle_or_hard_expired_sessions(self) -> None:
        with mock.patch.object(gateway_app, "SESSION_TIMEOUT_SECONDS", 30):
            self.assertTrue(
                gateway_app.session_is_active(
                    self._session(last_activity=80.0, expires_at=150.0),
                    now=100.0,
                )
            )
            self.assertFalse(
                gateway_app.session_is_active(
                    self._session(last_activity=69.0, expires_at=150.0),
                    now=100.0,
                )
            )
            self.assertFalse(
                gateway_app.session_is_active(
                    self._session(last_activity=80.0, expires_at=99.0),
                    now=100.0,
                )
            )

    def test_enforce_session_daemon_affinity_rejects_remote_sessions(self) -> None:
        with mock.patch.object(gateway_app, "local_docker_daemon_id", "daemon-local"):
            with self.assertRaises(HTTPException) as ctx:
                gateway_app.enforce_session_daemon_affinity(self._session(docker_daemon_id="daemon-other"))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_infer_network_enabled_uses_container_host_config(self) -> None:
        self.assertFalse(
            gateway_app.infer_network_enabled(SimpleNamespace(attrs={"HostConfig": {"NetworkMode": "none"}}))
        )
        self.assertTrue(
            gateway_app.infer_network_enabled(SimpleNamespace(attrs={"HostConfig": {"NetworkMode": "bridge"}}))
        )

    def test_infer_network_enabled_falls_back_to_gateway_network_mode_on_bad_attrs(self) -> None:
        class BrokenContainer:
            @property
            def attrs(self):
                raise RuntimeError("boom")

        with mock.patch.object(gateway_app, "SANDBOX_NETWORK_MODE", "bridge"):
            self.assertTrue(gateway_app.infer_network_enabled(BrokenContainer()))
        with mock.patch.object(gateway_app, "SANDBOX_NETWORK_MODE", "none"):
            self.assertFalse(gateway_app.infer_network_enabled(BrokenContainer()))

    def test_recover_session_info_reads_container_labels_and_attrs(self) -> None:
        container = SimpleNamespace(
            labels={
                "owner-subject": "subject-1",
                "owner-tenant": "tenant-1",
                "docker-daemon-id": "daemon-label",
                "inject-sandbox-env": "1",
                "expires-at": "333.5",
                "execution-count": "7",
            },
            attrs={"Created": "2025-01-15T10:00:00Z", "HostConfig": {"NetworkMode": "bridge"}},
        )

        session = gateway_app.recover_session_info(container)

        self.assertTrue(session.network_enabled)
        self.assertEqual(session.owner_subject, "subject-1")
        self.assertEqual(session.owner_tenant, "tenant-1")
        self.assertEqual(session.docker_daemon_id, "daemon-label")
        self.assertTrue(session.inject_sandbox_env)
        self.assertEqual(session.expires_at, 333.5)
        self.assertEqual(session.execution_count, 7)
        self.assertEqual(session.created_at, session.last_activity)

    def test_recover_session_info_defaults_invalid_optional_fields(self) -> None:
        container = SimpleNamespace(
            labels={"owner-subject": "subject-1", "expires-at": "bad", "execution-count": "bad"},
            attrs={"Created": "not-a-date", "HostConfig": {"NetworkMode": "none"}},
        )
        with mock.patch.object(gateway_app, "MAX_SESSION_LIFETIME_SECONDS", 50), mock.patch.object(
            gateway_app,
            "local_docker_daemon_id",
            "daemon-local",
        ):
            session = gateway_app.recover_session_info(container)

        self.assertFalse(session.network_enabled)
        self.assertEqual(session.docker_daemon_id, "daemon-local")
        self.assertEqual(session.execution_count, 0)
        self.assertAlmostEqual(session.expires_at or 0, session.created_at + 50, delta=1.0)


class GatewayParsingAndValidationTests(unittest.TestCase):
    def test_parse_executor_result_prefers_last_valid_prefixed_payload(self) -> None:
        raw = "\n".join(
            [
                '__EXECUTOR_RESULT__:{"stdout":"old"}',
                "__EXECUTOR_RESULT__:{broken",
                '__EXECUTOR_RESULT__:{"stdout":"new","files":[]}',
            ]
        )
        self.assertEqual(gateway_app.parse_executor_result(raw)["stdout"], "new")

    def test_parse_executor_result_falls_back_to_legacy_json_lines(self) -> None:
        raw = "noise\n{\"stdout\":\"legacy\",\"files\":[]}\n"
        self.assertEqual(gateway_app.parse_executor_result(raw)["stdout"], "legacy")

    def test_parse_executor_result_rejects_noise_only_output(self) -> None:
        with self.assertRaises(gateway_app.ExecutorOutputError):
            gateway_app.parse_executor_result("plain text only")

    def test_parse_prefixed_result_skips_invalid_and_empty_payloads(self) -> None:
        raw = "PREFIX:\nPREFIX:{broken\nPREFIX:{\"ok\": true}"
        self.assertEqual(gateway_app.parse_prefixed_result(raw, prefix="PREFIX:", empty_error="empty"), {"ok": True})

    def test_parse_prefixed_result_raises_custom_empty_error(self) -> None:
        with self.assertRaisesRegex(gateway_app.ExecutorOutputError, "empty renderer"):
            gateway_app.parse_prefixed_result("", prefix="PREFIX:", empty_error="empty renderer")

    def test_normalize_render_output_path_allows_nested_output_files(self) -> None:
        self.assertEqual(
            gateway_app.normalize_render_output_path("/tmp/output/render-1/deck.zip"),
            "/tmp/output/render-1/deck.zip",
        )

    def test_normalize_render_output_path_rejects_sibling_prefixes(self) -> None:
        with self.assertRaises(RuntimeError):
            gateway_app.normalize_render_output_path("/tmp/output-malicious/deck.zip")

    def test_file_input_accepts_nested_forward_slash_paths(self) -> None:
        file_input = gateway_app.FileInput(name="data/input.txt", content="aGVsbG8=")
        self.assertEqual(file_input.name, "data/input.txt")

    def test_file_input_rejects_empty_dot_parent_absolute_and_too_long_names(self) -> None:
        invalid_names = ["", ".", "./file.txt", "dir//file.txt", "../file.txt", "/file.txt", "\\"]
        for invalid_name in invalid_names:
            with self.subTest(invalid_name=invalid_name):
                with self.assertRaises(ValidationError):
                    gateway_app.FileInput(name=invalid_name, content="aGVsbG8=")

        with mock.patch.object(gateway_app, "MAX_FILE_NAME_LENGTH", 3):
            with self.assertRaises(ValidationError):
                gateway_app.FileInput(name="abcd", content="aGVsbG8=")

    def test_execute_request_normalizes_none_pip_packages_to_empty_list(self) -> None:
        request = gateway_app.ExecuteRequest(container_id="ctr-1", code="print(1)", pip_packages=None)
        self.assertEqual(request.pip_packages, [])

    def test_execute_request_validates_pip_package_names_and_feature_flag(self) -> None:
        with mock.patch.object(gateway_app, "ALLOW_PIP_INSTALLS", True):
            request = gateway_app.ExecuteRequest(
                container_id="ctr-1",
                code="print(1)",
                pip_packages=[" numpy==1.26.4 ", "requests[security]>=2.0"],
            )
        self.assertEqual(request.pip_packages, ["numpy==1.26.4", "requests[security]>=2.0"])

        with mock.patch.object(gateway_app, "ALLOW_PIP_INSTALLS", False):
            with self.assertRaises(ValidationError):
                gateway_app.ExecuteRequest(container_id="ctr-1", code="print(1)", pip_packages=["numpy"])

        with mock.patch.object(gateway_app, "ALLOW_PIP_INSTALLS", True):
            with self.assertRaises(ValidationError):
                gateway_app.ExecuteRequest(container_id="ctr-1", code="print(1)", pip_packages=["bad package"])

    def test_execute_request_rejects_too_many_pip_packages(self) -> None:
        with mock.patch.object(gateway_app, "ALLOW_PIP_INSTALLS", True), mock.patch.object(
            gateway_app,
            "MAX_PIP_PACKAGES",
            1,
        ):
            with self.assertRaises(ValidationError):
                gateway_app.ExecuteRequest(container_id="ctr-1", code="print(1)", pip_packages=["a", "b"])

    def test_render_input_file_rejects_unsafe_names_and_extra_fields(self) -> None:
        invalid_names = ["../image.png", "dir/image.png", "dir\\image.png", "space name.png", ".", ".."]
        for invalid_name in invalid_names:
            with self.subTest(invalid_name=invalid_name):
                with self.assertRaises(ValidationError):
                    gateway_app.RenderInputFile(file_name=invalid_name, base64_content="aGVsbG8=")

        with self.assertRaises(ValidationError):
            gateway_app.RenderInputFile(file_name="image.png", base64_content="aGVsbG8=", extra=True)

    def test_latex_input_file_preserves_common_asset_names(self) -> None:
        for file_name in ["chart 1.png", "figure(2).jpg"]:
            with self.subTest(file_name=file_name):
                input_file = gateway_app.LatexInputFile(file_name=file_name, base64_content="aGVsbG8=")
                self.assertEqual(input_file.file_name, file_name)

        for invalid_name in ["../image.png", "dir/image.png", "dir\\image.png", ".", "..", "bad\nname.png"]:
            with self.subTest(invalid_name=invalid_name):
                with self.assertRaises(ValidationError):
                    gateway_app.LatexInputFile(file_name=invalid_name, base64_content="aGVsbG8=")

    def test_validate_render_payload_limits_rejects_oversized_html(self) -> None:
        payload = gateway_app.RenderRequest(html="x" * 5)
        with mock.patch.object(gateway_app, "RENDER_MAX_HTML_CHARS", 4):
            with self.assertRaises(HTTPException) as ctx:
                gateway_app.validate_render_payload_limits(payload)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("html is too large", ctx.exception.detail)


class GatewayFilePreparationTests(unittest.TestCase):
    def test_prepare_files_decodes_base64_and_tracks_sizes(self) -> None:
        files = [
            gateway_app.FileInput(name="alpha.txt", content=base64.b64encode(b"alpha").decode("ascii")),
            gateway_app.FileInput(name="nested/beta.bin", content=base64.b64encode(b"beta").decode("ascii")),
        ]

        prepared = gateway_app.prepare_files(files)

        self.assertEqual(prepared, [gateway_app.PreparedFile("alpha.txt", b"alpha"), gateway_app.PreparedFile("nested/beta.bin", b"beta")])

    def test_prepare_files_rejects_per_file_size_limit(self) -> None:
        files = [gateway_app.FileInput(name="big.bin", content=base64.b64encode(b"12345").decode("ascii"))]
        with mock.patch.object(gateway_app, "MAX_INPUT_FILE_SIZE", 4):
            with self.assertRaises(HTTPException) as ctx:
                gateway_app.prepare_files(files)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("too large", ctx.exception.detail)

    def test_prepare_files_rejects_total_size_limit(self) -> None:
        files = [
            gateway_app.FileInput(name="one.bin", content=base64.b64encode(b"123").decode("ascii")),
            gateway_app.FileInput(name="two.bin", content=base64.b64encode(b"456").decode("ascii")),
        ]
        with mock.patch.object(gateway_app, "MAX_INPUT_TOTAL_SIZE", 5):
            with self.assertRaises(HTTPException) as ctx:
                gateway_app.prepare_files(files)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Total size", ctx.exception.detail)

    def test_create_tar_archive_from_files_preserves_names_modes_and_content(self) -> None:
        archive_bytes = gateway_app.create_tar_archive_from_files(
            [
                gateway_app.PreparedFile("alpha.txt", b"alpha"),
                gateway_app.PreparedFile("nested/beta.txt", b"beta"),
            ]
        )

        with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r:*") as archive:
            names = archive.getnames()
            self.assertEqual(names, ["alpha.txt", "nested/beta.txt"])
            for member in archive.getmembers():
                self.assertEqual(member.mode, 0o600)
            self.assertEqual(archive.extractfile("alpha.txt").read(), b"alpha")  # type: ignore[union-attr]
            self.assertEqual(archive.extractfile("nested/beta.txt").read(), b"beta")  # type: ignore[union-attr]

    def test_build_tmpfs_config_includes_configured_owner_and_sizes(self) -> None:
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(gateway_app, "SANDBOX_UID", 123))
            stack.enter_context(mock.patch.object(gateway_app, "SANDBOX_GID", 456))
            stack.enter_context(mock.patch.object(gateway_app, "SANDBOX_HOME_TMPFS_SIZE", "111m"))
            stack.enter_context(mock.patch.object(gateway_app, "SANDBOX_TMP_ROOT_SIZE", "222m"))
            tmpfs = gateway_app.build_tmpfs_config()

        self.assertEqual(tmpfs["/home/sandbox"], "size=111m,mode=0700,uid=123,gid=456")
        self.assertEqual(tmpfs["/tmp"], "size=222m,mode=1777,uid=123,gid=456")
        self.assertEqual(
            gateway_app.build_tmpfs_config(tmp_root_size="1g", home_tmpfs_size="2g")["/tmp"],
            "size=1g,mode=1777,uid=10001,gid=10001",
        )

    def test_build_render_exec_environment_contains_renderer_limits_and_runtime_flags(self) -> None:
        with mock.patch.object(gateway_app, "SLIDE_RENDERING_VERSION", "v2"):
            env = gateway_app.build_render_exec_environment(77)

        self.assertEqual(env["BETA"], "1")
        self.assertEqual(env["SLIDE_RENDERING_VERSION"], "v2")
        self.assertEqual(env["ALLOWED_HOSTS"], "127.0.0.1,localhost")
        self.assertEqual(env["RENDER_TIMEOUT_SECONDS"], "77")
        self.assertEqual(env["MAX_CONCURRENT_RENDERS"], "1")
        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], "/ms-playwright")

    def test_build_render_exec_environment_can_forward_insecure_override(self) -> None:
        with mock.patch.object(gateway_app, "RENDER_ALLOW_INSECURE_PRODUCTION_CONFIGURATION", "true"):
            env = gateway_app.build_render_exec_environment(77)

        self.assertEqual(env["ALLOW_INSECURE_PRODUCTION_CONFIGURATION"], "true")


class GatewayAsyncBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.backend = InMemoryStateBackend()
        gateway_app.state_backend = self.backend

    async def test_enforce_rate_limit_allows_when_backend_allows(self) -> None:
        with mock.patch.object(self.backend, "allow_within_rate_limit", mock.AsyncMock(return_value=True)) as allow:
            await gateway_app.enforce_rate_limit("bucket", limit=1, window_seconds=60, message="slow down")
        allow.assert_awaited_once_with("bucket", limit=1, window_seconds=60)

    async def test_enforce_rate_limit_raises_http_429_when_backend_rejects(self) -> None:
        with mock.patch.object(self.backend, "allow_within_rate_limit", mock.AsyncMock(return_value=False)):
            with self.assertRaises(HTTPException) as ctx:
                await gateway_app.enforce_rate_limit("bucket", limit=1, window_seconds=60, message="slow down")
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.detail, "slow down")

    async def test_provision_files_uses_isolated_python_startup(self) -> None:
        exec_create = mock.Mock(return_value={"Id": "exec-1"})
        gateway_app.docker_client = SimpleNamespace(api=SimpleNamespace(exec_create=exec_create))
        fake_container = SimpleNamespace(id="ctr-1")

        with mock.patch.object(gateway_app, "_run_exec_with_stdin", return_value=(b"", b"", 0)):
            await gateway_app.provision_files_in_container(
                fake_container,
                target_dir="/home/sandbox",
                files=[gateway_app.PreparedFile("input.txt", b"hello")],
            )

        kwargs = exec_create.call_args.kwargs
        self.assertEqual(kwargs["cmd"][:4], ["python", "-I", "-S", "-c"])
        self.assertEqual(kwargs["workdir"], "/tmp")

    async def test_enforce_container_creation_limits_allows_under_both_limits(self) -> None:
        await self.backend.save_session(
            "ctr-existing",
            SessionInfo(1.0, 90.0, False, "other", None, expires_at=1000.0),
            session_timeout_seconds=60,
        )
        with mock.patch.object(gateway_app, "MAX_ACTIVE_SESSIONS", 2), mock.patch.object(
            gateway_app,
            "MAX_CONTAINERS_PER_PRINCIPAL",
            1,
        ), mock.patch.object(gateway_app, "time", wraps=gateway_app.time) as mocked_time:
            mocked_time.time.return_value = 100.0
            await gateway_app.enforce_container_creation_limits(
                gateway_app.AuthContext("subject-1", None, "api_key")
            )

    async def test_enforce_container_creation_limits_rejects_total_limit(self) -> None:
        await self.backend.save_session(
            "ctr-existing",
            SessionInfo(1.0, 90.0, False, "other", None, expires_at=1000.0),
            session_timeout_seconds=60,
        )
        with mock.patch.object(gateway_app, "MAX_ACTIVE_SESSIONS", 1), mock.patch.object(
            gateway_app,
            "time",
            wraps=gateway_app.time,
        ) as mocked_time:
            mocked_time.time.return_value = 100.0
            with self.assertRaises(HTTPException) as ctx:
                await gateway_app.enforce_container_creation_limits(
                    gateway_app.AuthContext("subject-1", None, "api_key")
                )
        self.assertEqual(ctx.exception.status_code, 429)

    async def test_enforce_container_creation_limits_rejects_owner_limit(self) -> None:
        await self.backend.save_session(
            "ctr-existing",
            SessionInfo(1.0, 90.0, False, "subject-1", "tenant-1", expires_at=1000.0),
            session_timeout_seconds=60,
        )
        with mock.patch.object(gateway_app, "MAX_ACTIVE_SESSIONS", 10), mock.patch.object(
            gateway_app,
            "MAX_CONTAINERS_PER_PRINCIPAL",
            1,
        ), mock.patch.object(gateway_app, "time", wraps=gateway_app.time) as mocked_time:
            mocked_time.time.return_value = 100.0
            with self.assertRaises(HTTPException) as ctx:
                await gateway_app.enforce_container_creation_limits(
                    gateway_app.AuthContext("subject-1", "tenant-1", "jwt")
                )
        self.assertEqual(ctx.exception.status_code, 429)

    async def test_enforce_container_creation_limits_ignores_expired_sessions(self) -> None:
        await self.backend.save_session(
            "ctr-expired",
            SessionInfo(1.0, 1.0, False, "subject-1", "tenant-1", expires_at=2.0),
            session_timeout_seconds=60,
        )
        with mock.patch.object(gateway_app, "MAX_ACTIVE_SESSIONS", 10), mock.patch.object(
            gateway_app,
            "MAX_CONTAINERS_PER_PRINCIPAL",
            1,
        ), mock.patch.object(gateway_app, "SESSION_TIMEOUT_SECONDS", 30), mock.patch.object(
            gateway_app.time,
            "time",
            return_value=100.0,
        ):
            await gateway_app.enforce_container_creation_limits(
                gateway_app.AuthContext("subject-1", "tenant-1", "jwt")
            )

    async def test_touch_session_delegates_timeout_to_state_backend(self) -> None:
        session = SessionInfo(1.0, 1.0, False, "subject", None)
        with mock.patch.object(
            self.backend,
            "touch_session",
            mock.AsyncMock(return_value=session),
        ) as touch:
            with mock.patch.object(gateway_app, "SESSION_TIMEOUT_SECONDS", 123):
                self.assertIs(await gateway_app.touch_session("ctr-1"), session)
        touch.assert_awaited_once_with("ctr-1", session_timeout_seconds=123)

    async def test_run_exec_with_timeout_returns_decoded_output_and_exit_code(self) -> None:
        fake_container = SimpleNamespace(id="ctr-1")
        gateway_app.docker_client = SimpleNamespace(
            api=SimpleNamespace(
                exec_start=mock.Mock(return_value=b"hello"),
                exec_inspect=mock.Mock(return_value={"ExitCode": 7}),
            )
        )

        output, exit_code, timed_out = await gateway_app.run_exec_with_timeout(
            container=fake_container,
            container_id="ctr-1",
            exec_id="exec-1",
            timeout=5,
            execution_id="exec-id",
        )

        self.assertEqual((output, exit_code, timed_out), ("hello", 7, False))

    async def test_run_exec_with_timeout_removes_container_on_timeout(self) -> None:
        fake_container = SimpleNamespace(id="ctr-1")

        def slow_exec_start(_exec_id):
            import time

            time.sleep(1)
            return b"late"

        gateway_app.docker_client = SimpleNamespace(api=SimpleNamespace(exec_start=mock.Mock(side_effect=slow_exec_start)))

        with mock.patch.object(gateway_app, "remove_container", mock.AsyncMock()) as remove:
            output, exit_code, timed_out = await gateway_app.run_exec_with_timeout(
                container=fake_container,
                container_id="ctr-1",
                exec_id="exec-1",
                timeout=0.01,
                execution_id="exec-id",
            )

        self.assertEqual((output, exit_code, timed_out), ("", -1, True))
        remove.assert_awaited_once()
        self.assertEqual(remove.await_args.kwargs["reason"], "execution-timeout")


if __name__ == "__main__":
    unittest.main()
