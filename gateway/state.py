import asyncio
import base64
import os
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
    expires_at: Optional[float] = None
    execution_count: int = 0


class StateBackend:
    async def connect(self) -> None:
        """Connect to the state backend."""
        return None

    async def close(self) -> None:
        """Close the state backend connection."""
        return None

    async def health_check(self) -> bool:
        """Check if the state backend is healthy."""
        return True

    async def get_session(self, container_id: str) -> Optional[SessionInfo]:
        """Get session information by container ID."""
        raise NotImplementedError

    async def save_session(
        self,
        container_id: str,
        session: SessionInfo,
        *,
        session_timeout_seconds: int,
    ) -> None:
        """Save session information with timeout."""
        raise NotImplementedError

    async def touch_session(
        self,
        container_id: str,
        *,
        session_timeout_seconds: int,
    ) -> Optional[SessionInfo]:
        """Update session last activity timestamp and return session info."""
        raise NotImplementedError

    async def delete_session(self, container_id: str) -> None:
        """Delete session by container ID."""
        raise NotImplementedError

    async def list_sessions(self) -> dict[str, SessionInfo]:
        """List all active sessions."""
        raise NotImplementedError

    async def count_sessions_total(self) -> int:
        """Count total number of sessions."""
        return len(await self.list_sessions())

    async def count_sessions_for_owner(self, subject: str, tenant: Optional[str]) -> int:
        """Count sessions for a specific owner."""
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
        """Check if request is within rate limit for the bucket."""
        raise NotImplementedError

    @asynccontextmanager
    async def container_creation_guard(self, *, timeout_seconds: int) -> AsyncIterator[None]:
        """Context manager to serialize container creation."""
        del timeout_seconds
        yield

    @asynccontextmanager
    async def execution_lock(
        self, container_id: str, *, timeout_seconds: int
    ) -> AsyncIterator[None]:
        """Context manager to serialize execution per container."""
        del container_id, timeout_seconds
        yield


class InMemoryStateBackend(StateBackend):
    def __init__(self) -> None:
        """Initialize in-memory state backend."""
        self.sessions: dict[str, SessionInfo] = {}
        self.rate_limit_state: Dict[str, Deque[float]] = {}
        self._lock = asyncio.Lock()
        self._creation_lock = asyncio.Lock()
        self._exec_locks: Dict[str, asyncio.Lock] = {}

    async def get_session(self, container_id: str) -> Optional[SessionInfo]:
        """Get session from in-memory store."""
        return self.sessions.get(container_id)

    async def save_session(
        self,
        container_id: str,
        session: SessionInfo,
        *,
        session_timeout_seconds: int,
    ) -> None:
        """Save session to in-memory store."""
        del session_timeout_seconds
        self.sessions[container_id] = session

    async def touch_session(
        self,
        container_id: str,
        *,
        session_timeout_seconds: int,
    ) -> Optional[SessionInfo]:
        """Update session last activity in in-memory store."""
        del session_timeout_seconds
        session = self.sessions.get(container_id)
        if session is None:
            return None
        session.last_activity = time.time()
        return session

    async def delete_session(self, container_id: str) -> None:
        """Delete session from in-memory store."""
        self.sessions.pop(container_id, None)
        self._exec_locks.pop(container_id, None)

    async def list_sessions(self) -> dict[str, SessionInfo]:
        """List all sessions from in-memory store."""
        return dict(self.sessions)

    async def allow_within_rate_limit(
        self,
        bucket: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> bool:
        """Check rate limit using in-memory sliding window."""
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
        """In-memory lock to serialize container creation."""
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
        """In-memory per-container lock to serialize execution."""
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
        """Initialize Redis state backend with connection URL."""
        self.client = redis_asyncio.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=float(os.getenv("REDIS_SOCKET_CONNECT_TIMEOUT", "5")),
            socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT", "5")),
            health_check_interval=int(os.getenv("REDIS_HEALTH_CHECK_INTERVAL", "30")),
            retry_on_timeout=True,
        )
        self._session_index_key = "gateway:sessions:index"
        self._creation_lock_key = "gateway:lock:container-create"

    async def connect(self) -> None:
        """Connect to Redis and verify connection."""
        await self.client.ping()

    async def close(self) -> None:
        """Close Redis connection."""
        await self.client.aclose()

    async def health_check(self) -> bool:
        """Check if Redis connection is healthy."""
        try:
            await self.client.ping()
            return True
        except Exception:
            return False

    async def get_session(self, container_id: str) -> Optional[SessionInfo]:
        """Get session from Redis by container ID."""
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
        """Save session to Redis with TTL."""
        ttl = self._session_ttl(session, session_timeout_seconds)
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
        """Update session last activity in Redis."""
        session = await self.get_session(container_id)
        if session is None:
            return None

        session.last_activity = time.time()
        ttl = self._session_ttl(session, session_timeout_seconds)
        pipeline = self.client.pipeline()
        pipeline.hset(self._session_key(container_id), mapping=self._session_to_mapping(session))
        pipeline.expire(self._session_key(container_id), ttl)
        await pipeline.execute()
        return session

    async def delete_session(self, container_id: str) -> None:
        """Delete session from Redis."""
        pipeline = self.client.pipeline()
        pipeline.delete(self._session_key(container_id))
        pipeline.srem(self._session_index_key, container_id)
        await pipeline.execute()

    async def list_sessions(self) -> dict[str, SessionInfo]:
        """List all sessions from Redis."""
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
        """Check rate limit using Redis sorted set."""
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
        """Redis-based lock to serialize container creation."""
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
        """Redis-based per-container lock to serialize execution."""
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
        """Convert SessionInfo to dictionary for Redis storage."""
        return {
            "created_at": str(session.created_at),
            "last_activity": str(session.last_activity),
            "network_enabled": "1" if session.network_enabled else "0",
            "owner_subject": session.owner_subject,
            "owner_tenant": session.owner_tenant or "",
            "docker_daemon_id": session.docker_daemon_id or "",
            "inject_sandbox_env": "1" if session.inject_sandbox_env else "0",
            "expires_at": str(session.expires_at or ""),
            "execution_count": str(session.execution_count),
        }

    @staticmethod
    def _session_from_mapping(mapping: dict[str, str]) -> SessionInfo:
        """Convert Redis dictionary to SessionInfo."""
        return SessionInfo(
            created_at=float(mapping["created_at"]),
            last_activity=float(mapping["last_activity"]),
            network_enabled=mapping.get("network_enabled", "0") == "1",
            owner_subject=mapping["owner_subject"],
            owner_tenant=mapping.get("owner_tenant") or None,
            docker_daemon_id=mapping.get("docker_daemon_id") or None,
            inject_sandbox_env=mapping.get("inject_sandbox_env", "0") == "1",
            expires_at=float(mapping["expires_at"]) if mapping.get("expires_at") else None,
            execution_count=int(mapping.get("execution_count") or 0),
        )

    @staticmethod
    def _session_ttl(session: SessionInfo, session_timeout_seconds: int) -> int:
        idle_ttl = session_timeout_seconds + SESSION_TTL_GRACE_SECONDS
        if session.expires_at is None:
            return idle_ttl
        lifetime_ttl = max(1, int(session.expires_at - time.time())) + SESSION_TTL_GRACE_SECONDS
        return max(1, min(idle_ttl, lifetime_ttl))

    @staticmethod
    def _session_key(container_id: str) -> str:
        """Generate Redis key for session storage."""
        return f"gateway:session:{container_id}"

    @staticmethod
    def _rate_limit_key(bucket: str) -> str:
        """Generate Redis key for rate limiting."""
        encoded = base64.urlsafe_b64encode(bucket.encode("utf-8")).decode("ascii")
        return f"gateway:rate:{encoded}"
