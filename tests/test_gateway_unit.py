#!/usr/bin/env python3
import asyncio
import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import docker.errors
from fastapi import HTTPException
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


class GatewayConfigurationTests(unittest.TestCase):
    def _base_overrides(self, **extra):
        overrides = {
            "DEFAULT_TIMEOUT": 30,
            "MAX_TIMEOUT": 120,
            "REQUIRE_AUTH": False,
            "JWT_SECRET": None,
            "STATIC_API_KEYS": [],
            "ENABLE_CORS": False,
            "IS_PRODUCTION": False,
            "DOCKER_HOST": "",
            "USE_DOCKER_DEFAULT_SECCOMP": True,
            "SECCOMP_PROFILE_DAEMON_PATH": "",
            "SANDBOX_NETWORK_MODE": "bridge",
            "REQUIRE_SHARED_STATE": False,
            "MAX_CONTAINERS_PER_PRINCIPAL": 1,
            "MAX_ACTIVE_SESSIONS": 1,
            "CONTAINER_CREATE_GUARD_TIMEOUT": 1,
        }
        overrides.update(extra)
        return overrides

    def test_validate_runtime_configuration_rejects_plain_tcp_in_production(self) -> None:
        overrides = self._base_overrides(
            REQUIRE_AUTH=True,
            JWT_SECRET="secret",
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
            JWT_SECRET="secret",
            IS_PRODUCTION=True,
            DOCKER_HOST="tcp://docker-proxy:2376",
        )

        with ExitStack() as stack:
            for name, value in overrides.items():
                stack.enter_context(mock.patch.object(gateway_app, name, value))

            with self.assertRaisesRegex(RuntimeError, "dedicated remote Docker daemon"):
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
