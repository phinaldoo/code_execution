#!/usr/bin/env python3
import asyncio
import base64
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import sys


PROJECT_DIR = Path(__file__).resolve().parent.parent
GATEWAY_DIR = PROJECT_DIR / "gateway"
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

from state import InMemoryStateBackend, RedisStateBackend, SessionInfo


class InMemoryStateBackendTests(unittest.IsolatedAsyncioTestCase):
    def _session(self, subject: str = "subject", tenant: str | None = None, **overrides) -> SessionInfo:
        values = {
            "created_at": 1.0,
            "last_activity": 2.0,
            "network_enabled": False,
            "owner_subject": subject,
            "owner_tenant": tenant,
            "docker_daemon_id": "daemon",
            "inject_sandbox_env": False,
            "expires_at": None,
            "execution_count": 0,
        }
        values.update(overrides)
        return SessionInfo(**values)

    async def test_save_get_list_count_and_delete_session(self) -> None:
        backend = InMemoryStateBackend()
        session = self._session()

        await backend.save_session("ctr-1", session, session_timeout_seconds=60)

        self.assertIs(await backend.get_session("ctr-1"), session)
        self.assertEqual(await backend.count_sessions_total(), 1)
        listed = await backend.list_sessions()
        self.assertEqual(list(listed), ["ctr-1"])
        listed["ctr-2"] = self._session()
        self.assertEqual(await backend.count_sessions_total(), 1, "list_sessions must return a copy")

        await backend.delete_session("ctr-1")
        self.assertIsNone(await backend.get_session("ctr-1"))
        await backend.delete_session("ctr-1")
        self.assertEqual(await backend.count_sessions_total(), 0)

    async def test_touch_session_updates_last_activity_and_handles_missing_session(self) -> None:
        backend = InMemoryStateBackend()
        session = self._session(last_activity=1.0)
        await backend.save_session("ctr-1", session, session_timeout_seconds=60)

        with mock.patch("state.time.time", return_value=123.45):
            touched = await backend.touch_session("ctr-1", session_timeout_seconds=60)

        self.assertIs(touched, session)
        self.assertEqual(session.last_activity, 123.45)
        self.assertIsNone(await backend.touch_session("missing", session_timeout_seconds=60))

    async def test_count_sessions_for_owner_matches_subject_and_tenant(self) -> None:
        backend = InMemoryStateBackend()
        await backend.save_session("a", self._session("subject", None), session_timeout_seconds=60)
        await backend.save_session("b", self._session("subject", "tenant"), session_timeout_seconds=60)
        await backend.save_session("c", self._session("other", None), session_timeout_seconds=60)

        self.assertEqual(await backend.count_sessions_for_owner("subject", None), 1)
        self.assertEqual(await backend.count_sessions_for_owner("subject", "tenant"), 1)
        self.assertEqual(await backend.count_sessions_for_owner("missing", None), 0)

    async def test_rate_limit_allows_unlimited_or_empty_buckets(self) -> None:
        backend = InMemoryStateBackend()
        self.assertTrue(await backend.allow_within_rate_limit("", limit=1, window_seconds=60))
        self.assertTrue(await backend.allow_within_rate_limit("bucket", limit=0, window_seconds=60))

    async def test_rate_limit_rejects_after_limit_within_window(self) -> None:
        backend = InMemoryStateBackend()
        with mock.patch("state.time.time", return_value=100.0):
            self.assertTrue(await backend.allow_within_rate_limit("bucket", limit=2, window_seconds=60))
            self.assertTrue(await backend.allow_within_rate_limit("bucket", limit=2, window_seconds=60))
            self.assertFalse(await backend.allow_within_rate_limit("bucket", limit=2, window_seconds=60))

    async def test_rate_limit_prunes_hits_outside_window(self) -> None:
        backend = InMemoryStateBackend()
        with mock.patch("state.time.time", return_value=100.0):
            self.assertTrue(await backend.allow_within_rate_limit("bucket", limit=1, window_seconds=10))
        with mock.patch("state.time.time", return_value=111.0):
            self.assertTrue(await backend.allow_within_rate_limit("bucket", limit=1, window_seconds=10))

    async def test_container_creation_guard_serializes_callers(self) -> None:
        backend = InMemoryStateBackend()
        order: list[str] = []

        async def worker(name: str, delay: float) -> None:
            async with backend.container_creation_guard(timeout_seconds=2):
                order.append(f"{name}-start")
                await asyncio.sleep(delay)
                order.append(f"{name}-end")

        first = asyncio.create_task(worker("a", 0.02))
        await asyncio.sleep(0.005)
        second = asyncio.create_task(worker("b", 0.0))
        await asyncio.gather(first, second)

        self.assertEqual(order, ["a-start", "a-end", "b-start", "b-end"])


class RedisStateBackendMappingTests(unittest.TestCase):
    def _session(self, **overrides) -> SessionInfo:
        values = {
            "created_at": 1.5,
            "last_activity": 2.5,
            "network_enabled": True,
            "owner_subject": "subject",
            "owner_tenant": "tenant",
            "docker_daemon_id": "daemon",
            "inject_sandbox_env": True,
            "expires_at": 999.5,
            "execution_count": 4,
        }
        values.update(overrides)
        return SessionInfo(**values)

    def test_session_mapping_round_trips_all_fields(self) -> None:
        session = self._session()

        mapping = RedisStateBackend._session_to_mapping(session)
        restored = RedisStateBackend._session_from_mapping(mapping)

        self.assertEqual(restored, session)
        self.assertEqual(mapping["network_enabled"], "1")
        self.assertEqual(mapping["inject_sandbox_env"], "1")

    def test_session_mapping_encodes_none_fields_as_empty_strings(self) -> None:
        session = self._session(
            network_enabled=False,
            owner_tenant=None,
            docker_daemon_id=None,
            inject_sandbox_env=False,
            expires_at=None,
            execution_count=0,
        )

        mapping = RedisStateBackend._session_to_mapping(session)
        restored = RedisStateBackend._session_from_mapping(mapping)

        self.assertEqual(mapping["network_enabled"], "0")
        self.assertEqual(mapping["owner_tenant"], "")
        self.assertEqual(mapping["docker_daemon_id"], "")
        self.assertEqual(mapping["inject_sandbox_env"], "0")
        self.assertEqual(mapping["expires_at"], "")
        self.assertEqual(restored, session)

    def test_session_ttl_uses_idle_timeout_without_expiration(self) -> None:
        session = self._session(expires_at=None)
        self.assertEqual(RedisStateBackend._session_ttl(session, 60), 360)

    def test_session_ttl_uses_shorter_lifetime_or_idle_ttl(self) -> None:
        with mock.patch("state.time.time", return_value=100.0):
            self.assertEqual(RedisStateBackend._session_ttl(self._session(expires_at=120.0), 60), 320)
            self.assertEqual(RedisStateBackend._session_ttl(self._session(expires_at=1000.0), 60), 360)
            self.assertEqual(RedisStateBackend._session_ttl(self._session(expires_at=99.0), 60), 301)

    def test_session_and_rate_limit_keys_are_namespaced(self) -> None:
        self.assertEqual(RedisStateBackend._session_key("ctr-1"), "gateway:session:ctr-1")
        rate_key = RedisStateBackend._rate_limit_key("subject:tenant")
        self.assertTrue(rate_key.startswith("gateway:rate:"))
        encoded = rate_key.removeprefix("gateway:rate:")
        self.assertEqual(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"), "subject:tenant")


class RedisStateBackendClientTests(unittest.IsolatedAsyncioTestCase):
    def make_backend(self, client) -> RedisStateBackend:
        backend = RedisStateBackend.__new__(RedisStateBackend)
        backend.client = client
        backend._session_index_key = "gateway:sessions:index"
        backend._creation_lock_key = "gateway:lock:container-create"
        return backend

    async def test_connect_close_and_health_check_delegate_to_client(self) -> None:
        client = SimpleNamespace(
            ping=mock.AsyncMock(),
            aclose=mock.AsyncMock(),
        )
        backend = self.make_backend(client)

        await backend.connect()
        self.assertTrue(await backend.health_check())
        await backend.close()

        client.ping.assert_awaited()
        client.aclose.assert_awaited_once()

    async def test_health_check_returns_false_on_client_failure(self) -> None:
        client = SimpleNamespace(ping=mock.AsyncMock(side_effect=RuntimeError("down")))
        backend = self.make_backend(client)
        self.assertFalse(await backend.health_check())

    async def test_get_session_returns_none_for_empty_hash(self) -> None:
        client = SimpleNamespace(hgetall=mock.AsyncMock(return_value={}))
        backend = self.make_backend(client)
        self.assertIsNone(await backend.get_session("ctr-1"))
        client.hgetall.assert_awaited_once_with("gateway:session:ctr-1")

    async def test_get_session_decodes_hash_mapping(self) -> None:
        session = SessionInfo(1.0, 2.0, False, "subject", None)
        client = SimpleNamespace(
            hgetall=mock.AsyncMock(return_value=RedisStateBackend._session_to_mapping(session))
        )
        backend = self.make_backend(client)
        self.assertEqual(await backend.get_session("ctr-1"), session)

    async def test_save_touch_and_delete_use_pipeline_operations(self) -> None:
        session = SessionInfo(1.0, 2.0, False, "subject", None, expires_at=200.0)
        pipeline = SimpleNamespace(
            hset=mock.Mock(),
            expire=mock.Mock(),
            sadd=mock.Mock(),
            delete=mock.Mock(),
            srem=mock.Mock(),
            execute=mock.AsyncMock(),
        )
        client = SimpleNamespace(pipeline=mock.Mock(return_value=pipeline), hgetall=mock.AsyncMock())
        backend = self.make_backend(client)

        with mock.patch("state.time.time", return_value=100.0):
            await backend.save_session("ctr-1", session, session_timeout_seconds=60)

        pipeline.hset.assert_called_with("gateway:session:ctr-1", mapping=RedisStateBackend._session_to_mapping(session))
        pipeline.expire.assert_called_with("gateway:session:ctr-1", 360)
        pipeline.sadd.assert_called_with("gateway:sessions:index", "ctr-1")
        pipeline.execute.assert_awaited_once()

        client.hgetall.return_value = RedisStateBackend._session_to_mapping(session)
        pipeline.execute.reset_mock()
        with mock.patch("state.time.time", return_value=123.0):
            touched = await backend.touch_session("ctr-1", session_timeout_seconds=60)
        self.assertIsNotNone(touched)
        self.assertEqual(touched.last_activity, 123.0)
        pipeline.execute.assert_awaited_once()

        pipeline.execute.reset_mock()
        await backend.delete_session("ctr-1")
        pipeline.delete.assert_called_with("gateway:session:ctr-1")
        pipeline.srem.assert_called_with("gateway:sessions:index", "ctr-1")
        pipeline.execute.assert_awaited_once()

    async def test_touch_session_returns_none_when_session_is_missing(self) -> None:
        client = SimpleNamespace(hgetall=mock.AsyncMock(return_value={}))
        backend = self.make_backend(client)
        self.assertIsNone(await backend.touch_session("ctr-1", session_timeout_seconds=60))

    async def test_list_sessions_decodes_existing_sessions_and_removes_stale_ids(self) -> None:
        session = SessionInfo(1.0, 2.0, False, "subject", None)
        pipeline = SimpleNamespace(
            hgetall=mock.Mock(),
            execute=mock.AsyncMock(return_value=[RedisStateBackend._session_to_mapping(session), {}]),
        )
        client = SimpleNamespace(
            smembers=mock.AsyncMock(return_value={"stale", "ctr-1"}),
            pipeline=mock.Mock(return_value=pipeline),
            srem=mock.AsyncMock(),
        )
        backend = self.make_backend(client)

        sessions = await backend.list_sessions()

        self.assertEqual(sessions, {"ctr-1": session})
        self.assertEqual(pipeline.hgetall.call_args_list, [mock.call("gateway:session:ctr-1"), mock.call("gateway:session:stale")])
        client.srem.assert_awaited_once_with("gateway:sessions:index", "stale")

    async def test_list_sessions_returns_empty_without_index_entries(self) -> None:
        client = SimpleNamespace(smembers=mock.AsyncMock(return_value=set()))
        backend = self.make_backend(client)
        self.assertEqual(await backend.list_sessions(), {})

    async def test_rate_limit_allows_empty_bucket_or_non_positive_limit_without_redis(self) -> None:
        client = SimpleNamespace(eval=mock.AsyncMock())
        backend = self.make_backend(client)

        self.assertTrue(await backend.allow_within_rate_limit("", limit=1, window_seconds=60))
        self.assertTrue(await backend.allow_within_rate_limit("bucket", limit=0, window_seconds=60))
        client.eval.assert_not_called()

    async def test_rate_limit_evaluates_redis_script_for_real_buckets(self) -> None:
        client = SimpleNamespace(eval=mock.AsyncMock(return_value=1))
        backend = self.make_backend(client)

        with mock.patch("state.time.time", return_value=12.345):
            self.assertTrue(await backend.allow_within_rate_limit("bucket", limit=2, window_seconds=60))

        args = client.eval.await_args.args
        self.assertEqual(args[1], 1)
        self.assertEqual(args[2], RedisStateBackend._rate_limit_key("bucket"))
        self.assertEqual(args[3], 12345)
        self.assertEqual(args[4], 60000)
        self.assertEqual(args[5], 2)


if __name__ == "__main__":
    unittest.main()
