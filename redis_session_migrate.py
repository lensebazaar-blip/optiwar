"""Session store migration: filesystem -> Redis, with authentication-safe
outage handling (option A).

Design goals (per security review):
- Redis is the single authoritative session store.
- Existing filesystem sessions migrate transparently (no forced logout) but
  exactly once: on the first Redis-up read that is satisfied from filesystem,
  the record is written through to Redis and the filesystem copy is DELETED.
  => nothing lingers to "resurrect" a revoked/expired session.
- A clean Redis miss (key evicted/expired/revoked) does NOT fall back to
  filesystem once the session has been migrated => revocation/expiry honored.
- During a genuine Redis OUTAGE (connection error):
    * not-yet-migrated sessions may still be read from filesystem (availability
      for the shrinking legacy set) but are NEVER re-persisted to filesystem;
    * already-migrated sessions have no filesystem copy, so they degrade to
      ANONYMOUS (fail-safe) rather than being served stale auth state;
    * writes are dropped (best-effort) rather than creating durable filesystem
      auth records.
- Observability: every filesystem-served lookup is logged (backend, endpoint,
  method, authed flag, request id) WITHOUT full URLs/PII, so filesystem traffic
  can be proven to reach zero.
- Retirement: after ``retire_at_epoch`` (one max session lifetime past cutover)
  the filesystem fallback is disabled entirely and Redis is the sole authority.

Constraint: do not run a second app host while the filesystem fallback is still
active (it is local to this box).
"""
import pickle
import time

from cachelib.file import FileSystemCache
from flask import has_request_context, request
from flask_session.base import Serializer
from flask_session.redis import RedisSessionInterface
from redis.exceptions import RedisError


class _PickleSerializer(Serializer):
    def __init__(self, app):
        self.app = app

    def encode(self, session):
        return pickle.dumps(dict(session), protocol=pickle.HIGHEST_PROTOCOL)

    def decode(self, serialized_data):
        return pickle.loads(serialized_data)


class DualReadRedisSessionInterface(RedisSessionInterface):
    def __init__(self, *args, legacy_dir=None, legacy_threshold=500,
                 retire_at_epoch=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.serializer = _PickleSerializer(self.app)
        self._legacy = (
            FileSystemCache(legacy_dir, threshold=legacy_threshold)
            if legacy_dir
            else None
        )
        self._retire_at = retire_at_epoch  # epoch seconds, or None = no retirement

    # ---- helpers ---------------------------------------------------------
    def _legacy_active(self):
        if self._legacy is None:
            return False
        if self._retire_at is not None and time.time() >= self._retire_at:
            return False
        return True

    def _ttl(self):
        try:
            return int(self.app.permanent_session_lifetime.total_seconds())
        except Exception:
            return 7 * 24 * 3600

    def _log_backend(self, backend, data):
        try:
            authed = bool(isinstance(data, dict) and data.get("user_id"))
            if has_request_context():
                self.app.logger.warning(
                    "session-backend backend=%s endpoint=%s method=%s authed=%s rid=%s",
                    backend, request.endpoint, request.method, authed,
                    request.headers.get("X-Request-ID", "-"),
                )
            else:
                self.app.logger.warning(
                    "session-backend backend=%s authed=%s (no-request-ctx)",
                    backend, authed,
                )
        except Exception:
            pass

    # ---- storage overrides ----------------------------------------------
    def _retrieve_session_data(self, store_id):
        redis_up = True
        raw = None
        try:
            raw = self.client.get(store_id)
        except RedisError as e:
            redis_up = False
            self.app.logger.error(f"Session Redis read failed ({e}); outage mode")

        if raw is not None:
            try:
                return self.serializer.decode(raw)  # authoritative Redis hit
            except Exception:
                pass  # corrupt value -> treat as miss below

        if not self._legacy_active():
            return None

        if redis_up:
            # clean Redis miss: one-time migration of a legacy filesystem session.
            legacy = self._legacy.get(store_id)
            if legacy is None:
                return None  # post-migration/never-existed -> honor revocation/expiry
            migrated = False
            try:
                self.client.set(store_id, self.serializer.encode(legacy), ex=self._ttl())
                migrated = True
            except RedisError as e:
                self.app.logger.error(f"Session migrate write failed ({e}); keeping filesystem record")
            if migrated:
                try:
                    self._legacy.delete(store_id)  # no lingering copy to resurrect
                except Exception:
                    pass
            self._log_backend("filesystem-migrated" if migrated else "filesystem-migrate-deferred", legacy)
            return legacy

        # Redis OUTAGE: serve not-yet-migrated legacy sessions read-only (availability);
        # already-migrated sessions have no filesystem copy -> None -> anonymous (fail-safe).
        legacy = self._legacy.get(store_id)
        if legacy is not None:
            self._log_backend("filesystem-outage-degraded", legacy)
            return legacy
        return None

    def _upsert_session(self, session_lifetime, session, store_id):
        ttl = int(session_lifetime.total_seconds())
        try:
            self.client.set(
                name=store_id,
                value=self.serializer.encode(session),
                ex=ttl,
            )
        except RedisError as e:
            # option A: do NOT create durable filesystem auth state during an outage.
            self.app.logger.error(
                f"Session Redis write failed ({e}); dropped (fail-safe, not persisted to filesystem)"
            )

    def _delete_session(self, store_id):
        try:
            self.client.delete(store_id)
        except RedisError as e:
            self.app.logger.error(f"Session Redis delete failed ({e})")
        # always attempt to purge any legacy copy so logout/regenerate is complete
        if self._legacy is not None:
            try:
                self._legacy.delete(store_id)
            except Exception:
                pass
