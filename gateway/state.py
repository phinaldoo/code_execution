import asyncio
import base64
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import AsyncIterator, Deque, Dict, Optional

import redis.asyncio as redis_asyncio


SESSION_TTL_GRACE_SECONDS = 300


@dataclass
class SessionInfo:
    created_at: float
    last_activity: float
    network_enabled: bool
    owner_subject: str
    owner_tenant: Optional[str]
    docker_daemon_id: Optional[str] = None
    inject_sandbox_env: bool = False


class StateBackend:
    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def get_session(self, container_id: str) -> Optional[SessionInfo]:
        raise NotImplementedError

    async def save_session(
        self,
        container_id: str,
        session: SessionInfo,
        *,
        session_timeout_seconds: int,
    ) -> None:
        raise NotImplementedError

    async def touch_session(
        self,
        container_id: str,
        *,
        session_timeout_seconds: int,
    ) -> Optional[SessionInfo]:
        raise NotImplementedError

    async def delete_session(self, container_id: str) -> None:
        raise NotImplementedError

    async def list_sessions(self) -> dict[str, SessionInfo]:
        raise NotImplementedError

    async def count_sessions_total(self) -> int:
        return len(await self.list_sessions())

    async def count_sessions_for_owner(self, subject: str, tenant: Optional[str]) -> int:
        sessions = await self.list_sessions()
        return sum(
            1
            for session in sessions.values()
            if session.owner_subject == subject and session.owner_tenant == tenant
        )

    async def allow_within_rate_limit(
        self,
        bucket: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> bool:
        raise NotImplementedError

    @asynccontextmanager
    async def container_creation_guard(self, *, timeout_seconds: int) -> AsyncIterator[None]:
        del timeout_seconds
        yield

    @asynccontextmanager
    async def execution_lock(
        self, container_id: str, *, timeout_seconds: int
    ) -> AsyncIterator[None]:
        del container_id, timeout_seconds
        yield


class InMemoryStateBackend(StateBackend):
    def __init__(self) -> None:
        self.sessions: dict[str, SessionInfo] = {}
        self.rate_limit_state: Dict[str, Deque[float]] = {}
        self._lock = asyncio.Lock()
        self._creation_lock = asyncio.Lock()
        self._exec_locks: Dict[str, asyncio.Lock] = {}

    async def get_session(self, container_id: str) -> Optional[SessionInfo]:
        return self.sessions.get(container_id)

    async def save_session(
        self,
        container_id: str,
        session: SessionInfo,
        *,
        session_timeout_seconds: int,
    ) -> None:
        del session_timeout_seconds
        self.sessions[container_id] = session

    async def touch_session(
        self,
        container_id: str,
        *,
        session_timeout_seconds: int,
    ) -> Optional[SessionInfo]:
        del session_timeout_seconds
        session = self.sessions.get(container_id)
        if session is None:
            return None
        session.last_activity = time.time()
        return session

    async def delete_session(self, container_id: str) -> None:
        self.sessions.pop(container_id, None)
        self._exec_locks.pop(container_id, None)

    async def list_sessions(self) -> dict[str, SessionInfo]:
        return dict(self.sessions)

    async def allow_within_rate_limit(
        self,
        bucket: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> bool:
        if limit <= 0 or not bucket:
            return True

        now = time.time()
        window_start = now - window_seconds

        async with self._lock:
            hits = self.rate_limit_state.setdefault(bucket, deque())
            while hits and hits[0] < window_start:
                hits.popleft()

            if len(hits) >= limit:
                return False

            hits.append(now)
            return True

    @asynccontextmanager
    async def container_creation_guard(self, *, timeout_seconds: int) -> AsyncIterator[None]:
        try:
            await asyncio.wait_for(self._creation_lock.acquire(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("Timed out waiting for the container creation guard") from exc
        try:
            yield
        finally:
            self._creation_lock.release()

    @asynccontextmanager
    async def execution_lock(
        self, container_id: str, *, timeout_seconds: int
    ) -> AsyncIterator[None]:
        lock = self._exec_locks.setdefault(container_id, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("Timed out waiting for container execution lock") from exc
        try:
            yield
        finally:
            lock.release()


class RedisStateBackend(StateBackend):
    _RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - window_ms)
local count = redis.call('ZCARD', key)
if count >= limit then
  redis.call('EXPIRE', key, math.ceil(window_ms / 1000) + 5)
  return 0
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, math.ceil(window_ms / 1000) + 5)
return 1
"""

    def __init__(self, redis_url: str) -> None:
        self.client = redis_asyncio.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        self._session_index_key = "gateway:sessions:index"
        self._creation_lock_key = "gateway:lock:container-create"

    async def connect(self) -> None:
        await self.client.ping()

    async def close(self) -> None:
        await self.client.aclose()

    async def health_check(self) -> bool:
        try:
            await self.client.ping()
            return True
        except Exception:
            return False

    async def get_session(self, container_id: str) -> Optional[SessionInfo]:
        data = await self.client.hgetall(self._session_key(container_id))
        if not data:
            return None
        return self._session_from_mapping(data)

    async def save_session(
        self,
        container_id: str,
        session: SessionInfo,
        *,
        session_timeout_seconds: int,
    ) -> None:
        ttl = session_timeout_seconds + SESSION_TTL_GRACE_SECONDS
        pipeline = self.client.pipeline()
        pipeline.hset(self._session_key(container_id), mapping=self._session_to_mapping(session))
        pipeline.expire(self._session_key(container_id), ttl)
        pipeline.sadd(self._session_index_key, container_id)
        await pipeline.execute()

    async def touch_session(
        self,
        container_id: str,
        *,
        session_timeout_seconds: int,
    ) -> Optional[SessionInfo]:
        session = await self.get_session(container_id)
        if session is None:
            return None

        session.last_activity = time.time()
        ttl = session_timeout_seconds + SESSION_TTL_GRACE_SECONDS
        pipeline = self.client.pipeline()
        pipeline.hset(self._session_key(container_id), mapping=self._session_to_mapping(session))
        pipeline.expire(self._session_key(container_id), ttl)
        await pipeline.execute()
        return session

    async def delete_session(self, container_id: str) -> None:
        pipeline = self.client.pipeline()
        pipeline.delete(self._session_key(container_id))
        pipeline.srem(self._session_index_key, container_id)
        await pipeline.execute()

    async def list_sessions(self) -> dict[str, SessionInfo]:
        container_ids = sorted(await self.client.smembers(self._session_index_key))
        if not container_ids:
            return {}

        pipeline = self.client.pipeline()
        for container_id in container_ids:
            pipeline.hgetall(self._session_key(container_id))
        raw_sessions = await pipeline.execute()

        sessions: dict[str, SessionInfo] = {}
        stale_ids: list[str] = []
        for container_id, raw in zip(container_ids, raw_sessions, strict=False):
            if not raw:
                stale_ids.append(container_id)
                continue
            sessions[container_id] = self._session_from_mapping(raw)

        if stale_ids:
            await self.client.srem(self._session_index_key, *stale_ids)

        return sessions

    async def allow_within_rate_limit(
        self,
        bucket: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> bool:
        if limit <= 0 or not bucket:
            return True

        now_ms = int(time.time() * 1000)
        allowed = await self.client.eval(
            self._RATE_LIMIT_SCRIPT,
            1,
            self._rate_limit_key(bucket),
            now_ms,
            window_seconds * 1000,
            limit,
            f"{now_ms}:{uuid.uuid4().hex}",
        )
        return bool(allowed)

    @asynccontextmanager
    async def container_creation_guard(self, *, timeout_seconds: int) -> AsyncIterator[None]:
        lock = self.client.lock(
            self._creation_lock_key,
            timeout=max(timeout_seconds + 5, 10),
            blocking_timeout=timeout_seconds,
            sleep=0.1,
        )
        acquired = await lock.acquire()
        if not acquired:
            raise TimeoutError("Timed out waiting for the container creation guard")

        try:
            yield
        finally:
            with suppress(Exception):
                await lock.release()

    @asynccontextmanager
    async def execution_lock(
        self, container_id: str, *, timeout_seconds: int
    ) -> AsyncIterator[None]:
        lock_key = f"gateway:lock:exec:{container_id}"
        lock = self.client.lock(
            lock_key,
            timeout=max(timeout_seconds + 10, 30),
            blocking_timeout=timeout_seconds,
            sleep=0.1,
        )
        acquired = await lock.acquire()
        if not acquired:
            raise TimeoutError("Timed out waiting for container execution lock")

        try:
            yield
        finally:
            with suppress(Exception):
                await lock.release()

    @staticmethod
    def _session_to_mapping(session: SessionInfo) -> dict[str, str]:
        return {
            "created_at": str(session.created_at),
            "last_activity": str(session.last_activity),
            "network_enabled": "1" if session.network_enabled else "0",
            "owner_subject": session.owner_subject,
            "owner_tenant": session.owner_tenant or "",
            "docker_daemon_id": session.docker_daemon_id or "",
            "inject_sandbox_env": "1" if session.inject_sandbox_env else "0",
        }

    @staticmethod
    def _session_from_mapping(mapping: dict[str, str]) -> SessionInfo:
        return SessionInfo(
            created_at=float(mapping["created_at"]),
            last_activity=float(mapping["last_activity"]),
            network_enabled=mapping.get("network_enabled", "0") == "1",
            owner_subject=mapping["owner_subject"],
            owner_tenant=mapping.get("owner_tenant") or None,
            docker_daemon_id=mapping.get("docker_daemon_id") or None,
            inject_sandbox_env=mapping.get("inject_sandbox_env", "0") == "1",
        )

    @staticmethod
    def _session_key(container_id: str) -> str:
        return f"gateway:session:{container_id}"

    @staticmethod
    def _rate_limit_key(bucket: str) -> str:
        encoded = base64.urlsafe_b64encode(bucket.encode("utf-8")).decode("ascii")
        return f"gateway:rate:{encoded}"
