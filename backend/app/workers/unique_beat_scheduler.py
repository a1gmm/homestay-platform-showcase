"""Fail-closed Redis ownership for the project's single Celery Beat service."""

from __future__ import annotations

import os
import socket
from threading import Event, Lock, Thread
from uuid import uuid4

from celery.beat import PersistentScheduler
from redis import Redis

from app.core.config import settings


BEAT_OWNER_KEY = "miniapp:celery-beat:owner"
_PROCESS_OWNER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
_PROCESS_OWNER_LOCK = Lock()
_PROCESS_OWNER_REFERENCES = 0
_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class BeatOwnershipError(RuntimeError):
    pass


class BeatOwnerHeartbeat:
    """Async compare-and-heartbeat primitive used by integration checks."""

    def __init__(self, client: object, *, owner_id: str, ttl_seconds: int = 60) -> None:
        self._client = client
        self.owner_id = owner_id
        self.ttl_seconds = ttl_seconds

    async def acquire(self) -> bool:
        return bool(
            await self._client.set(
                BEAT_OWNER_KEY,
                self.owner_id,
                nx=True,
                ex=self.ttl_seconds,
            )
        )

    async def renew(self) -> bool:
        return bool(
            await self._client.eval(
                _RENEW_SCRIPT,
                1,
                BEAT_OWNER_KEY,
                self.owner_id,
                self.ttl_seconds,
            )
        )

    async def release(self) -> bool:
        return bool(
            await self._client.eval(
                _RELEASE_SCRIPT, 1, BEAT_OWNER_KEY, self.owner_id
            )
        )


class UniqueOwnerPersistentScheduler(PersistentScheduler):
    """Persistent scheduler that stops rather than tolerate a second owner."""

    owner_ttl_seconds = 60

    def __init__(self, *args, **kwargs) -> None:
        # Celery constructs the scheduler once for its startup banner and again
        # for the running service.  Both instances in one process must share a
        # fence; a different process still receives a distinct token.
        self._owner_id = _PROCESS_OWNER_ID
        self._owner_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        acquired = self._owner_client.set(
            BEAT_OWNER_KEY,
            self._owner_id,
            nx=True,
            ex=self.owner_ttl_seconds,
        )
        if not acquired:
            owner = self._owner_client.get(BEAT_OWNER_KEY)
            if owner != self._owner_id or not self._owner_client.eval(
                _RENEW_SCRIPT,
                1,
                BEAT_OWNER_KEY,
                self._owner_id,
                self.owner_ttl_seconds,
            ):
                self._owner_client.close()
                raise BeatOwnershipError("another Celery Beat owner is active")
        global _PROCESS_OWNER_REFERENCES
        with _PROCESS_OWNER_LOCK:
            _PROCESS_OWNER_REFERENCES += 1
        self._closed = False
        self._heartbeat_stop = Event()
        self._heartbeat_lost = Event()
        self._heartbeat_thread = Thread(
            target=self._heartbeat_loop,
            name="celery-beat-owner-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()
        try:
            super().__init__(*args, **kwargs)
        except BaseException:
            self._shutdown_owner()
            raise

    def _heartbeat_loop(self) -> None:
        client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        interval = max(0.1, self.owner_ttl_seconds / 3.0)
        try:
            while not self._heartbeat_stop.wait(interval):
                try:
                    renewed = client.eval(
                        _RENEW_SCRIPT,
                        1,
                        BEAT_OWNER_KEY,
                        self._owner_id,
                        self.owner_ttl_seconds,
                    )
                except Exception:
                    self._heartbeat_lost.set()
                    return
                if not renewed:
                    self._heartbeat_lost.set()
                    return
        finally:
            client.close()

    def _assert_owner(self) -> None:
        if self._heartbeat_lost.is_set() or not self._owner_client.eval(
            _RENEW_SCRIPT,
            1,
            BEAT_OWNER_KEY,
            self._owner_id,
            self.owner_ttl_seconds,
        ):
            self._heartbeat_lost.set()
            raise BeatOwnershipError("Celery Beat lost unique owner heartbeat")

    def _release_owner(self) -> bool:
        return bool(
            self._owner_client.eval(
                _RELEASE_SCRIPT, 1, BEAT_OWNER_KEY, self._owner_id
            )
        )

    def tick(self, *args, **kwargs):
        self._assert_owner()
        next_tick = super().tick(*args, **kwargs)
        self._assert_owner()
        return min(float(next_tick), self.owner_ttl_seconds / 3)

    def apply_entry(self, entry, producer=None) -> None:
        self._assert_owner()
        return super().apply_entry(entry, producer=producer)

    def _shutdown_owner(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._heartbeat_stop.set()
        self._heartbeat_thread.join(timeout=max(2.0, self.owner_ttl_seconds / 2.0))
        global _PROCESS_OWNER_REFERENCES
        with _PROCESS_OWNER_LOCK:
            _PROCESS_OWNER_REFERENCES -= 1
            release = _PROCESS_OWNER_REFERENCES == 0
        if release:
            self._release_owner()
        self._owner_client.close()

    def close(self) -> None:
        try:
            self._shutdown_owner()
        finally:
            super().close()


__all__ = [
    "BEAT_OWNER_KEY",
    "BeatOwnerHeartbeat",
    "BeatOwnershipError",
    "UniqueOwnerPersistentScheduler",
]
