#!/usr/bin/env python3
import asyncio
import json
import sys
import tarfile
import time
import unittest
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import docker.errors
from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError


PROJECT_DIR = Path(__file__).resolve().parent.parent
GATEWAY_DIR = PROJECT_DIR / "gateway"
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import app as gateway_app
from state import InMemoryStateBackend, RedisStateBackend, SessionInfo


class GatewaySafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.state_backend = InMemoryStateBackend()
        gateway_app.state_backend = self.state_backend
        gateway_app.local_docker_daemon_id = "daemon-local"
        gateway_app.docker_client = SimpleNamespace(
            containers=SimpleNamespace(
                get=mock.Mock(side_effect=docker.errors.NotFound("missing")),
            )
        )

    async def test_ensure_session_access_rejects_remote_daemon(self) -> None:
        await self.state_backend.save_session(
            "abc123",
            SessionInfo(
                created_at=1.0,
                last_activity=1.0,
                network_enabled=True,
                owner_subject="subject-1",
                owner_tenant=None,
                docker_daemon_id="daemon-remote",
            ),
            session_timeout_seconds=60,
        )

        with self.assertRaises(HTTPException) as ctx:
            await gateway_app.ensure_session_access(
                "abc123",
                gateway_app.AuthContext(subject="subject-1", tenant=None, auth_type="api_key"),
            )

        self.assertEqual(ctx.exception.status_code, 409)
        gateway_app.docker_client.containers.get.assert_not_called()

    async def test_remove_container_keeps_remote_session_state(self) -> None:
        await self.state_backend.save_session(
            "abc123",
            SessionInfo(
                created_at=1.0,
                last_activity=1.0,
                network_enabled=True,
                owner_subject="subject-1",
                owner_tenant=None,
                docker_daemon_id="daemon-remote",
            ),
            session_timeout_seconds=60,
        )

        await gateway_app.remove_container("abc123", reason="unit-test")

        session = await self.state_backend.get_session("abc123")
        self.assertIsNotNone(session)
        gateway_app.docker_client.containers.get.assert_not_called()

    async def test_remove_container_keeps_state_on_removal_failure(self) -> None:
        await self.state_backend.save_session(
            "ctr-fail",
            SessionInfo(
                created_at=1.0,
                last_activity=1.0,
                network_enabled=True,
                owner_subject="subject-1",
                owner_tenant=None,
                docker_daemon_id="daemon-local",
            ),
            session_timeout_seconds=60,
        )

        fake_container = SimpleNamespace(
            kill=mock.Mock(),
            remove=mock.Mock(side_effect=docker.errors.APIError("removal failed")),
        )
        gateway_app.docker_client.containers.get = mock.Mock(return_value=fake_container)

        await gateway_app.remove_container("ctr-fail", reason="unit-test")

        session = await self.state_backend.get_session("ctr-fail")
        self.assertIsNotNone(session, "Session state must be preserved when container removal fails")

    async def test_recover_or_remove_managed_container_preserves_existing_budget_state(self) -> None:
        await self.state_backend.save_session(
            "ctr-1",
            SessionInfo(
                created_at=1.0,
                last_activity=2.0,
                network_enabled=False,
                owner_subject="subject-1",
                owner_tenant=None,
                docker_daemon_id="daemon-local",
                expires_at=100.0,
                execution_count=7,
            ),
            session_timeout_seconds=60,
        )
        fake_container = SimpleNamespace(
            id="ctr-1",
            labels={
                "managed-by": "code-execution-gateway",
                "owner-subject": "subject-1",
                "execution-count": "0",
            },
            attrs={
                "Created": "2025-01-15T10:00:00Z",
                "HostConfig": {"NetworkMode": "none"},
            },
        )

        session = await gateway_app.recover_or_remove_managed_container(
            fake_container,
            missing_state_reason="unit-test-missing-state",
        )

        self.assertIsNotNone(session)
        self.assertEqual(session.execution_count, 7)
        self.assertEqual(session.expires_at, 100.0)
        saved = await self.state_backend.get_session("ctr-1")
        self.assertIsNotNone(saved)
        self.assertEqual(saved.execution_count, 7)

    async def test_ensure_session_access_removes_managed_container_when_shared_state_missing(self) -> None:
        fake_container = SimpleNamespace(
            id="ctr-missing-state",
            labels={
                "managed-by": "code-execution-gateway",
                "owner-subject": "subject-1",
            },
        )
        gateway_app.docker_client = SimpleNamespace(
            containers=SimpleNamespace(get=mock.Mock(return_value=fake_container)),
        )

        with mock.patch.object(gateway_app, "REQUIRE_SHARED_STATE", True):
            with mock.patch.object(gateway_app, "remove_container", mock.AsyncMock()) as remove_mock:
                with self.assertRaises(HTTPException) as ctx:
                    await gateway_app.ensure_session_access(
                        "ctr-missing-state",
                        gateway_app.AuthContext(subject="subject-1", tenant=None, auth_type="api_key"),
                    )

        self.assertEqual(ctx.exception.status_code, 404)
        remove_mock.assert_awaited_once()
        self.assertEqual(remove_mock.await_args.kwargs["reason"], "missing-shared-session-state")

    async def test_create_container_session_cleans_up_when_state_save_fails(self) -> None:
        fake_container = SimpleNamespace(id="ctr-save-fail")
        gateway_app.docker_client = SimpleNamespace(
            containers=SimpleNamespace(run=mock.Mock(return_value=fake_container))
        )

        save_error = RuntimeError("state backend unavailable")
        with mock.patch.object(
            self.state_backend,
            "save_session",
            mock.AsyncMock(side_effect=save_error),
        ):
            with mock.patch.object(gateway_app, "remove_container", mock.AsyncMock()) as remove_mock:
                with self.assertRaises(RuntimeError):
                    await gateway_app.create_container_session(
                        enable_network=False,
                        auth=gateway_app.AuthContext(
                            subject="subject-1",
                            tenant=None,
                            auth_type="api_key",
                        ),
                        inject_sandbox_env=False,
                    )

        remove_mock.assert_awaited_once()
        self.assertEqual(remove_mock.await_args.args[0], "ctr-save-fail")
        self.assertEqual(remove_mock.await_args.kwargs["reason"], "state-save-failed")

    async def test_create_container_session_uses_read_only_rootfs_and_effective_network(self) -> None:
        fake_container = SimpleNamespace(id="ctr-safe-defaults")
        gateway_app.docker_client = SimpleNamespace(
            containers=SimpleNamespace(run=mock.Mock(return_value=fake_container))
        )

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(gateway_app, "SANDBOX_NETWORK_MODE", "none"))
            stack.enter_context(mock.patch.object(gateway_app, "SANDBOX_READ_ONLY_ROOTFS", True))

            container_id = await gateway_app.create_container_session(
                enable_network=True,
                auth=gateway_app.AuthContext(
                    subject="subject-1",
                    tenant=None,
                    auth_type="api_key",
                ),
                inject_sandbox_env=False,
            )

        self.assertEqual(container_id, "ctr-safe-defaults")
        run_kwargs = gateway_app.docker_client.containers.run.call_args.kwargs
        self.assertTrue(run_kwargs["read_only"])
        self.assertEqual(run_kwargs["network_mode"], "none")
        self.assertIn("/tmp", run_kwargs["tmpfs"])
        self.assertIn("/home/sandbox", run_kwargs["tmpfs"])

        session = await self.state_backend.get_session(container_id)
        self.assertIsNotNone(session)
        self.assertFalse(session.network_enabled)

    async def test_run_code_in_sandbox_prefers_prefixed_executor_payload(self) -> None:
        await self.state_backend.save_session(
            "ctr-1",
            SessionInfo(
                created_at=1.0,
                last_activity=1.0,
                network_enabled=False,
                owner_subject="subject-1",
                owner_tenant=None,
                docker_daemon_id="daemon-local",
            ),
            session_timeout_seconds=60,
        )

        fake_container = SimpleNamespace(id="ctr-1")
        gateway_app.docker_client = SimpleNamespace(
            containers=SimpleNamespace(get=mock.Mock(return_value=fake_container)),
            api=SimpleNamespace(exec_create=mock.Mock(return_value={"Id": "exec-1"})),
        )

        raw_output = "\n".join(
            [
                "{not-json}",
                '{"stdout":"old","stderr":"","error":"x","error_type":"Legacy","files":[],"execution_time":0}',
                '__EXECUTOR_RESULT__:{"stdout":"new","stderr":"","error":null,"error_type":null,"files":[],"execution_time":0.25}',
            ]
        )
        with mock.patch.object(
            gateway_app,
            "run_exec_with_timeout",
            mock.AsyncMock(return_value=(raw_output, 0, False)),
        ):
            result = await gateway_app.run_code_in_sandbox(
                container_id="ctr-1",
                language="python",
                code="print('ok')",
                timeout=10,
                execution_id="exec-123",
            )

        self.assertEqual(result.stdout, "new")
        self.assertIsNone(result.error)

    async def test_run_code_in_sandbox_provisions_input_files_via_exec(self) -> None:
        await self.state_backend.save_session(
            "ctr-1",
            SessionInfo(
                created_at=1.0,
                last_activity=1.0,
                network_enabled=False,
                owner_subject="subject-1",
                owner_tenant=None,
                docker_daemon_id="daemon-local",
            ),
            session_timeout_seconds=60,
        )

        fake_container = SimpleNamespace(id="ctr-1")
        gateway_app.docker_client = SimpleNamespace(
            containers=SimpleNamespace(get=mock.Mock(return_value=fake_container)),
            api=SimpleNamespace(exec_create=mock.Mock(return_value={"Id": "exec-1"})),
        )

        raw_output = (
            '__EXECUTOR_RESULT__:{"stdout":"ok","stderr":"","error":null,'
            '"error_type":null,"files":[],"execution_time":0.1}'
        )
        with mock.patch.object(
            gateway_app,
            "provision_files_in_container",
            mock.AsyncMock(),
        ) as provision_mock:
            with mock.patch.object(
                gateway_app,
                "run_exec_with_timeout",
                mock.AsyncMock(return_value=(raw_output, 0, False)),
            ):
                result = await gateway_app.run_code_in_sandbox(
                    container_id="ctr-1",
                    language="python",
                    code="print('ok')",
                    timeout=10,
                    execution_id="exec-123",
                    files=[gateway_app.FileInput(name="input.txt", content="aGVsbG8=")],
                )

        self.assertEqual(result.stdout, "ok")
        provision_mock.assert_awaited_once()
        self.assertIs(provision_mock.await_args.args[0], fake_container)
        self.assertEqual(provision_mock.await_args.kwargs["target_dir"], "/home/sandbox")
        self.assertEqual(provision_mock.await_args.kwargs["files"][0].name, "input.txt")

    async def test_run_code_in_sandbox_removes_session_after_execution_budget(self) -> None:
        await self.state_backend.save_session(
            "ctr-1",
            SessionInfo(
                created_at=1.0,
                last_activity=1.0,
                network_enabled=False,
                owner_subject="subject-1",
                owner_tenant=None,
                docker_daemon_id="daemon-local",
                execution_count=1,
            ),
            session_timeout_seconds=60,
        )

        fake_container = SimpleNamespace(id="ctr-1")
        gateway_app.docker_client = SimpleNamespace(
            containers=SimpleNamespace(get=mock.Mock(return_value=fake_container)),
        )

        with mock.patch.object(gateway_app, "MAX_EXECUTIONS_PER_SESSION", 1):
            with mock.patch.object(gateway_app, "remove_container", mock.AsyncMock()) as remove_mock:
                with self.assertRaises(HTTPException) as ctx:
                    await gateway_app.run_code_in_sandbox(
                        container_id="ctr-1",
                        language="python",
                        code="print('ok')",
                        timeout=10,
                        execution_id="exec-123",
                    )

        self.assertEqual(ctx.exception.status_code, 429)
        remove_mock.assert_awaited_once()
        self.assertEqual(remove_mock.await_args.kwargs["reason"], "max-executions")

    async def test_ensure_sandbox_env_file_provisions_via_exec(self) -> None:
        fake_container = SimpleNamespace(id="ctr-1")

        with mock.patch.object(gateway_app, "_read_env_source_bytes", return_value=b"SECRET=1\n"):
            with mock.patch.object(
                gateway_app,
                "provision_files_in_container",
                mock.AsyncMock(),
            ) as provision_mock:
                await gateway_app.ensure_sandbox_env_file(
                    fake_container,
                    inject_sandbox_env=True,
                )

        provision_mock.assert_awaited_once()
        self.assertIs(provision_mock.await_args.args[0], fake_container)
        self.assertEqual(provision_mock.await_args.kwargs["target_dir"], "/home/sandbox")
        provisioned = provision_mock.await_args.kwargs["files"]
        self.assertEqual(provisioned[0].name, ".env")
        self.assertEqual(provisioned[0].content, b"SECRET=1\n")

    async def test_render_endpoint_returns_archive_headers(self) -> None:
        gateway_app.render_semaphore = asyncio.Semaphore(1)
        payload = gateway_app.RenderRequest(html="<section class='slide'>Hello</section>")
        auth = gateway_app.AuthContext(subject="subject-1", tenant=None, auth_type="api_key")
        fake_result = gateway_app.RenderSandboxResult(
            render_id="render-123",
            file_name="presentation_v2_test.zip",
            rendering_version="v2",
            content=b"zip-bytes",
            media_type="application/zip",
            slide_count=2,
            execution_time=0.5,
        )

        with mock.patch.object(gateway_app, "enforce_rate_limit", mock.AsyncMock()):
            with mock.patch.object(
                gateway_app,
                "render_presentation_in_sandbox",
                mock.AsyncMock(return_value=fake_result),
            ) as render_mock:
                response = await gateway_app.render_endpoint(payload, auth)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"zip-bytes")
        self.assertEqual(response.media_type, "application/zip")
        self.assertEqual(response.headers["x-rendering-version"], "v2")
        self.assertEqual(response.headers["x-slide-count"], "2")
        self.assertEqual(response.headers["x-render-id"], "render-123")
        self.assertIn("presentation_v2_test.zip", response.headers["content-disposition"])
        render_mock.assert_awaited_once()

    async def test_latex_endpoint_applies_throttles_before_decoding_assets(self) -> None:
        gateway_app.render_semaphore = asyncio.Semaphore(1)
        payload = gateway_app.LatexRenderRequest(
            tex="hello",
            input_files=[
                gateway_app.LatexInputFile(
                    file_name="asset.bin",
                    base64_content="not base64",
                )
            ],
        )
        auth = gateway_app.AuthContext(subject="subject-1", tenant=None, auth_type="api_key")

        with mock.patch.object(
            gateway_app,
            "enforce_rate_limit",
            mock.AsyncMock(side_effect=HTTPException(status_code=429, detail="slow down")),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await gateway_app.latex_render_endpoint(payload, auth)

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.detail, "slow down")

    async def test_create_render_container_uses_render_resource_limits(self) -> None:
        auth = gateway_app.AuthContext(subject="subject-1", tenant=None, auth_type="api_key")

        with mock.patch.object(gateway_app, "enforce_container_creation_limits", mock.AsyncMock()):
            with mock.patch.object(
                gateway_app,
                "create_container_session",
                mock.AsyncMock(return_value="ctr-render"),
            ) as create_mock:
                container_id = await gateway_app.create_render_container(auth)

        self.assertEqual(container_id, "ctr-render")
        create_mock.assert_awaited_once()
        kwargs = create_mock.await_args.kwargs
        self.assertEqual(kwargs["purpose"], "slide-render")
        self.assertEqual(kwargs["name_prefix"], "render")
        self.assertFalse(kwargs["enable_network"])
        self.assertEqual(kwargs["mem_limit"], gateway_app.RENDER_SANDBOX_MEM_LIMIT)


class GatewayConfigurationTests(unittest.TestCase):
    def _base_overrides(self, **extra):
        overrides = {
            "DEFAULT_TIMEOUT": 30,
            "MAX_TIMEOUT": 120,
            "DOCKER_CLIENT_TIMEOUT": 30,
            "SESSION_TIMEOUT_SECONDS": 1200,
            "MAX_SESSION_LIFETIME_SECONDS": 3600,
            "MAX_EXECUTIONS_PER_SESSION": 100,
            "REQUIRE_AUTH": False,
            "JWT_SECRET": None,
            "STATIC_API_KEYS": [],
            "ENABLE_CORS": False,
            "ENABLE_DOCS": False,
            "IS_PRODUCTION": False,
            "PUBLIC_BETA_MODE": False,
            "DOCKER_HOST": "",
            "SANDBOX_IMAGE": "code-sandbox:1.1.0",
            "SANDBOX_RUNTIME": "",
            "STRONG_SANDBOX_RUNTIMES": ["runsc", "kata-runtime"],
            "REQUIRE_STRONG_SANDBOX_ISOLATION": False,
            "USE_DOCKER_DEFAULT_SECCOMP": True,
            "SECCOMP_PROFILE_DAEMON_PATH": "",
            "SANDBOX_NETWORK_MODE": "bridge",
            "ALLOW_PIP_INSTALLS": False,
            "ALLOW_SANDBOX_ENV_INJECTION": False,
            "REQUIRE_SHARED_STATE": False,
            "MAX_CONTAINERS_PER_PRINCIPAL": 1,
            "MAX_ACTIVE_SESSIONS": 1,
            "CONTAINER_CREATE_GUARD_TIMEOUT": 1,
        }
        overrides.update(extra)
        return overrides

    def test_health_routes_require_auth_dependency(self) -> None:
        routes = {
            route.path: route
            for route in gateway_app.app.routes
            if isinstance(route, APIRoute) and "GET" in route.methods
        }

        for path in ("/health", "/healthz", "/readyz"):
            with self.subTest(path=path):
                dependant = routes[path].dependant
                self.assertTrue(
                    any(dep.call is gateway_app.verify_health_auth for dep in dependant.dependencies),
                    f"{path} must require verify_health_auth",
                )

    def test_validate_runtime_configuration_rejects_short_hmac_jwt_secret_in_production(self) -> None:
        overrides = self._base_overrides(
            REQUIRE_AUTH=True,
            JWT_SECRET="short-secret",
            IS_PRODUCTION=True,
            DOCKER_HOST="tcp://remote-docker:2376",
            REQUIRE_SHARED_STATE=True,
            REDIS_URL="redis://redis:6379/0",
        )

        with ExitStack() as stack:
            for name, value in overrides.items():
                stack.enter_context(mock.patch.object(gateway_app, name, value))

            with self.assertRaisesRegex(RuntimeError, "JWT_SECRET"):
                gateway_app.validate_runtime_configuration()

    def test_validate_runtime_configuration_allows_asymmetric_jwt_key_material_in_production(self) -> None:
        overrides = self._base_overrides(
            REQUIRE_AUTH=True,
            JWT_SECRET="public-key",
            JWT_ALGORITHMS=["RS256"],
            IS_PRODUCTION=True,
            DOCKER_HOST="tcp://remote-docker:2376",
            REQUIRE_SHARED_STATE=True,
            REDIS_URL="redis://redis:6379/0",
        )

        with ExitStack() as stack:
            for name, value in overrides.items():
                stack.enter_context(mock.patch.object(gateway_app, name, value))

            gateway_app.validate_runtime_configuration()

    def test_validate_runtime_configuration_rejects_plain_tcp_in_production(self) -> None:
        overrides = self._base_overrides(
            REQUIRE_AUTH=True,
            JWT_SECRET="a" * 32,
            IS_PRODUCTION=True,
            DOCKER_HOST="tcp://remote-docker:2375",
        )

        with ExitStack() as stack:
            for name, value in overrides.items():
                stack.enter_context(mock.patch.object(gateway_app, name, value))

            with self.assertRaisesRegex(RuntimeError, "TLS"):
                gateway_app.validate_runtime_configuration()

    def test_validate_runtime_configuration_rejects_local_proxy_in_production(self) -> None:
        overrides = self._base_overrides(
            REQUIRE_AUTH=True,
            JWT_SECRET="a" * 32,
            IS_PRODUCTION=True,
            DOCKER_HOST="tcp://docker-proxy:2376",
        )

        with ExitStack() as stack:
            for name, value in overrides.items():
                stack.enter_context(mock.patch.object(gateway_app, name, value))

            with self.assertRaisesRegex(RuntimeError, "dedicated remote Docker daemon"):
                gateway_app.validate_runtime_configuration()

    def test_validate_runtime_configuration_rejects_docs_in_production(self) -> None:
        overrides = self._base_overrides(
            REQUIRE_AUTH=True,
            JWT_SECRET="a" * 32,
            IS_PRODUCTION=True,
            ENABLE_DOCS=True,
            DOCKER_HOST="tcp://remote-docker:2376",
            REQUIRE_SHARED_STATE=True,
            REDIS_URL="redis://redis:6379/0",
        )

        with ExitStack() as stack:
            for name, value in overrides.items():
                stack.enter_context(mock.patch.object(gateway_app, name, value))

            with self.assertRaisesRegex(RuntimeError, "ENABLE_DOCS"):
                gateway_app.validate_runtime_configuration()

    def test_validate_runtime_configuration_rejects_latest_image_in_production(self) -> None:
        overrides = self._base_overrides(
            REQUIRE_AUTH=True,
            JWT_SECRET="a" * 32,
            IS_PRODUCTION=True,
            DOCKER_HOST="tcp://remote-docker:2376",
            REQUIRE_SHARED_STATE=True,
            REDIS_URL="redis://redis:6379/0",
            SANDBOX_IMAGE="code-sandbox:latest",
        )

        with ExitStack() as stack:
            for name, value in overrides.items():
                stack.enter_context(mock.patch.object(gateway_app, name, value))

            with self.assertRaisesRegex(RuntimeError, "SANDBOX_IMAGE"):
                gateway_app.validate_runtime_configuration()

    def test_validate_runtime_configuration_requires_strong_runtime_for_public_beta(self) -> None:
        overrides = self._base_overrides(
            REQUIRE_AUTH=True,
            JWT_SECRET="a" * 32,
            PUBLIC_BETA_MODE=True,
            SANDBOX_NETWORK_MODE="none",
            SANDBOX_IMAGE="code-sandbox:1.1.0",
            SANDBOX_RUNTIME="",
            DOCKER_HOST="tcp://remote-docker:2376",
            REQUIRE_SHARED_STATE=True,
            REDIS_URL="redis://redis:6379/0",
        )

        with ExitStack() as stack:
            for name, value in overrides.items():
                stack.enter_context(mock.patch.object(gateway_app, name, value))

            with self.assertRaisesRegex(RuntimeError, "Public beta mode requires"):
                gateway_app.validate_runtime_configuration()

    def test_validate_runtime_configuration_accepts_public_beta_with_runsc(self) -> None:
        overrides = self._base_overrides(
            REQUIRE_AUTH=True,
            JWT_SECRET="a" * 32,
            PUBLIC_BETA_MODE=True,
            SANDBOX_NETWORK_MODE="none",
            SANDBOX_IMAGE="code-sandbox:1.1.0",
            SANDBOX_RUNTIME="runsc",
            DOCKER_HOST="tcp://remote-docker:2376",
            REQUIRE_SHARED_STATE=True,
            REDIS_URL="redis://redis:6379/0",
        )

        with ExitStack() as stack:
            for name, value in overrides.items():
                stack.enter_context(mock.patch.object(gateway_app, name, value))

            gateway_app.validate_runtime_configuration()

    def test_validate_runtime_configuration_requires_daemon_visible_seccomp_path(self) -> None:
        overrides = self._base_overrides(
            USE_DOCKER_DEFAULT_SECCOMP=False,
            SECCOMP_PROFILE_DAEMON_PATH="",
        )

        with ExitStack() as stack:
            for name, value in overrides.items():
                stack.enter_context(mock.patch.object(gateway_app, name, value))

            with self.assertRaisesRegex(RuntimeError, "SECCOMP_PROFILE_DAEMON_PATH"):
                gateway_app.validate_runtime_configuration()

    def test_parse_static_api_keys_falls_back_when_key_id_is_empty(self) -> None:
        parsed = gateway_app.parse_static_api_keys([":my-secret"])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].key_id, "key-1")
        self.assertEqual(parsed[0].secret, "my-secret")

    def test_execute_request_rejects_unknown_language(self) -> None:
        with self.assertRaises(ValidationError):
            gateway_app.ExecuteRequest(
                container_id="ctr-1",
                language="ruby",
                code="puts 'nope'",
            )

    def test_file_input_rejects_backslash_paths(self) -> None:
        with self.assertRaises(ValidationError):
            gateway_app.FileInput(name="folder\\file.txt", content="aGVsbG8=")

    def test_create_container_request_network_defaults_to_off(self) -> None:
        self.assertFalse(gateway_app.CreateContainerRequest().enable_network)

    def test_render_request_accepts_base64_alias(self) -> None:
        request = gateway_app.RenderRequest(
            html="<section class='slide'>Hello</section>",
            input_files=[{"file_name": "image.png", "base64": "aGVsbG8="}],
        )

        self.assertEqual(request.input_files[0].base64_content, "aGVsbG8=")

    def test_render_request_rejects_duplicate_input_names(self) -> None:
        with self.assertRaises(ValidationError):
            gateway_app.RenderRequest(
                html="<section class='slide'>Hello</section>",
                input_files=[
                    {"file_name": "image.png", "base64_content": "aGVsbG8="},
                    {"file_name": "image.png", "base64_content": "aGVsbG8="},
                ],
            )

    def test_validate_render_payload_limits_rejects_too_many_files(self) -> None:
        payload = gateway_app.RenderRequest(
            html="<section class='slide'>Hello</section>",
            input_files=[{"file_name": "a.png", "base64_content": "aGVsbG8="}],
        )

        with mock.patch.object(gateway_app, "RENDER_MAX_INPUT_FILES", 0):
            with self.assertRaises(HTTPException) as ctx:
                gateway_app.validate_render_payload_limits(payload)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("too many input_files", ctx.exception.detail)


class GatewayVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_returns_service_metadata(self) -> None:
        response = await gateway_app.root()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.body),
            {
                "message": "Code Execution Gateway",
                "version": gateway_app.APP_VERSION_TAG,
                "execute_endpoint": "/execute",
                "render_endpoint": "/api/render",
                "latex_render_endpoint": "/api/latex/render",
                "version_endpoint": "/version",
            },
        )
        self.assertEqual(gateway_app.app.version, gateway_app.APP_VERSION)

    async def test_version_endpoint_returns_execution_metadata(self) -> None:
        response = await gateway_app.version()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.body),
            {
                "version": gateway_app.APP_VERSION,
                "tag": gateway_app.APP_VERSION_TAG,
                "api_contract_version": 1,
                "beta": False,
                "active_execution_version": "v1",
                "default_execution_version": "v1",
                "supported_execution_versions": ["v1"],
                "available_execution_versions": ["v1"],
                "active_rendering_version": "v1",
                "default_rendering_version": "v1",
                "supported_rendering_versions": ["v1", "v2"],
                "available_rendering_versions": ["v1", "v2"],
                "features": {
                    "gateway_version_headers": True,
                    "persistent_sessions": True,
                    "input_files": True,
                    "pip_packages": True,
                    "slide_rendering": True,
                    "slide_renderer_version_headers": True,
                },
            },
        )


class FilePreparationTests(unittest.TestCase):
    def test_prepare_files_rejects_duplicate_names(self) -> None:
        files = [
            gateway_app.FileInput(name="input.txt", content="aGVsbG8="),
            gateway_app.FileInput(name="input.txt", content="d29ybGQ="),
        ]

        with self.assertRaises(HTTPException) as ctx:
            gateway_app.prepare_files(files)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Duplicate input file name", ctx.exception.detail)

    def test_prepare_files_rejects_invalid_base64(self) -> None:
        files = [gateway_app.FileInput(name="input.txt", content="%%%")]

        with self.assertRaises(HTTPException) as ctx:
            gateway_app.prepare_files(files)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("not valid base64", ctx.exception.detail)

    def test_parse_executor_result_rejects_empty_output(self) -> None:
        with self.assertRaises(gateway_app.ExecutorOutputError):
            gateway_app.parse_executor_result("")

    def test_parse_render_result_reads_prefixed_payload(self) -> None:
        result = gateway_app.parse_render_result(
            'noise\n__RENDER_RESULT__:{"file_name":"deck.zip","error":null}'
        )

        self.assertEqual(result["file_name"], "deck.zip")

    def test_read_single_file_from_container_archive(self) -> None:
        tar_buffer = BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
            content = b"zip-bytes"
            info = tarfile.TarInfo(name="deck.zip")
            info.size = len(content)
            archive.addfile(info, BytesIO(content))

        fake_container = SimpleNamespace(
            get_archive=mock.Mock(
                return_value=(
                    [tar_buffer.getvalue()],
                    {"size": len(content)},
                )
            )
        )

        content = gateway_app._read_single_file_from_container_archive(
            fake_container,
            path="/tmp/output/render-1/deck.zip",
            max_bytes=100,
        )

        self.assertEqual(content, b"zip-bytes")

    def test_normalize_render_output_path_rejects_traversal(self) -> None:
        with self.assertRaises(RuntimeError):
            gateway_app.normalize_render_output_path("/tmp/output/render-1/../../secret")


class RequestLimitAndMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_limited_receive_rejects_streaming_body_over_limit(self) -> None:
        messages = [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": False},
        ]

        async def receive():
            return messages.pop(0)

        limited_receive = gateway_app.limited_receive_factory(receive, max_bytes=6)
        self.assertEqual(await limited_receive(), {"type": "http.request", "body": b"1234", "more_body": True})
        with self.assertRaises(gateway_app.RequestBodyTooLarge):
            await limited_receive()

    def test_request_content_length_limit_handles_large_and_invalid_values(self) -> None:
        large = SimpleNamespace(headers={"content-length": "100"})
        invalid = SimpleNamespace(headers={"content-length": "not-an-int"})
        missing = SimpleNamespace(headers={})

        with mock.patch.object(gateway_app, "MAX_REQUEST_BODY_SIZE", 10):
            self.assertTrue(gateway_app.request_content_length_exceeds_limit(large))
            self.assertFalse(gateway_app.request_content_length_exceeds_limit(invalid))
            self.assertFalse(gateway_app.request_content_length_exceeds_limit(missing))

    def test_render_path_uses_render_body_limit(self) -> None:
        self.assertLess(
            gateway_app.LATEX_RENDER_MAX_REQUEST_BODY_SIZE,
            gateway_app.RENDER_MAX_REQUEST_BODY_SIZE,
        )
        self.assertEqual(
            gateway_app.request_body_limit_for_path("/api/render"),
            gateway_app.RENDER_MAX_REQUEST_BODY_SIZE,
        )
        self.assertEqual(
            gateway_app.request_body_limit_for_path("/api/latex/render"),
            gateway_app.LATEX_RENDER_MAX_REQUEST_BODY_SIZE,
        )
        self.assertEqual(
            gateway_app.request_body_limit_for_path("/api/v1/latex/render"),
            gateway_app.LATEX_RENDER_MAX_REQUEST_BODY_SIZE,
        )
        self.assertEqual(
            gateway_app.request_body_limit_for_path("/execute"),
            gateway_app.MAX_REQUEST_BODY_SIZE,
        )

    def test_metrics_path_label_uses_route_template(self) -> None:
        request = SimpleNamespace(
            scope={"route": SimpleNamespace(path="/containers/{container_id}")},
            url=SimpleNamespace(path="/containers/abc123"),
        )

        self.assertEqual(
            gateway_app.metrics_path_label(request),
            "/containers/{container_id}",
        )

    def test_metrics_path_label_collapses_unmatched_routes(self) -> None:
        request = SimpleNamespace(
            scope={},
            url=SimpleNamespace(path="/made-up/abc123"),
        )

        self.assertEqual(gateway_app.metrics_path_label(request), "__unmatched__")


class RedisConfigurationTests(unittest.TestCase):
    def test_redis_backend_configures_socket_timeouts(self) -> None:
        with mock.patch("state.redis_asyncio.from_url") as from_url:
            with mock.patch.dict(
                "os.environ",
                {
                    "REDIS_SOCKET_CONNECT_TIMEOUT": "1.5",
                    "REDIS_SOCKET_TIMEOUT": "2.5",
                    "REDIS_HEALTH_CHECK_INTERVAL": "15",
                },
            ):
                RedisStateBackend("redis://example.test:6379/0")

        _, kwargs = from_url.call_args
        self.assertEqual(kwargs["socket_connect_timeout"], 1.5)
        self.assertEqual(kwargs["socket_timeout"], 2.5)
        self.assertEqual(kwargs["health_check_interval"], 15)
        self.assertTrue(kwargs["retry_on_timeout"])


class ExecutionLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_memory_execution_lock_serializes(self) -> None:
        backend = InMemoryStateBackend()
        order: list[str] = []

        async def worker(name: str, delay: float) -> None:
            async with backend.execution_lock("ctr-1", timeout_seconds=10):
                order.append(f"{name}-start")
                await asyncio.sleep(delay)
                order.append(f"{name}-end")

        t1 = asyncio.create_task(worker("a", 0.05))
        await asyncio.sleep(0.01)
        t2 = asyncio.create_task(worker("b", 0.01))
        await asyncio.gather(t1, t2)

        self.assertEqual(order, ["a-start", "a-end", "b-start", "b-end"])

    async def test_execution_lock_allows_different_containers(self) -> None:
        backend = InMemoryStateBackend()
        active: list[str] = []
        concurrent = False

        async def worker(container_id: str) -> None:
            nonlocal concurrent
            async with backend.execution_lock(container_id, timeout_seconds=10):
                active.append(container_id)
                if len(active) > 1:
                    concurrent = True
                await asyncio.sleep(0.02)
                active.remove(container_id)

        await asyncio.gather(worker("ctr-1"), worker("ctr-2"))
        self.assertTrue(concurrent, "Different containers should execute concurrently")

    async def test_execution_lock_cleaned_on_session_delete(self) -> None:
        backend = InMemoryStateBackend()
        await backend.save_session(
            "ctr-1",
            SessionInfo(
                created_at=1.0,
                last_activity=1.0,
                network_enabled=True,
                owner_subject="s",
                owner_tenant=None,
            ),
            session_timeout_seconds=60,
        )

        async with backend.execution_lock("ctr-1", timeout_seconds=5):
            pass

        self.assertIn("ctr-1", backend._exec_locks)
        await backend.delete_session("ctr-1")
        self.assertNotIn("ctr-1", backend._exec_locks)

    async def test_container_creation_guard_timeout_raises_timeout_error(self) -> None:
        backend = InMemoryStateBackend()
        await backend._creation_lock.acquire()
        try:
            with self.assertRaises(TimeoutError):
                async with backend.container_creation_guard(timeout_seconds=0.01):
                    pass
        finally:
            backend._creation_lock.release()

    async def test_execution_lock_timeout_raises_timeout_error(self) -> None:
        backend = InMemoryStateBackend()
        lock = backend._exec_locks.setdefault("ctr-1", asyncio.Lock())
        await lock.acquire()
        try:
            with self.assertRaises(TimeoutError):
                async with backend.execution_lock("ctr-1", timeout_seconds=0.01):
                    pass
        finally:
            lock.release()


class RecoveryTimestampTests(unittest.TestCase):
    def test_recover_session_preserves_creation_time(self) -> None:
        created_iso = "2025-01-15T10:00:00Z"
        container = SimpleNamespace(
            labels={"managed-by": "code-execution-gateway", "owner-subject": "user-1"},
            attrs={
                "Created": created_iso,
                "HostConfig": {"NetworkMode": "none"},
            },
        )

        with mock.patch.object(gateway_app, "local_docker_daemon_id", "daemon-1"):
            session = gateway_app.recover_session_info(container)

        self.assertAlmostEqual(session.created_at, session.last_activity, places=2)
        self.assertLess(session.last_activity, time.time())


if __name__ == "__main__":
    unittest.main()
