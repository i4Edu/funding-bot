from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

try:  # pragma: no cover - exercised indirectly when redis is installed
    import redis
except ImportError:  # pragma: no cover - fallback for minimal environments
    redis = None


_DEFAULT_NAMESPACE_TTLS = {
    "donor-records": 300.0,
    "connector-data": 60.0,
    "deduped-profiles": 600.0,
}


def _read_bool_env(name: str, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _read_float_env(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        parsed = float(raw_value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


class _CacheBackend:
    name = "memory"

    def get(self, key: str) -> str | None:
        raise NotImplementedError

    def set(self, key: str, value: str, ttl_seconds: float) -> None:
        raise NotImplementedError

    def delete(self, *keys: str) -> None:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def sadd(self, key: str, *values: str) -> None:
        raise NotImplementedError

    def smembers(self, key: str) -> set[str]:
        raise NotImplementedError

    def srem(self, key: str, *values: str) -> None:
        raise NotImplementedError

    def ping(self) -> bool:
        return True


class MemoryCacheBackend(_CacheBackend):
    name = "memory"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[str, tuple[str, float | None]] = {}
        self._sets: dict[str, set[str]] = {}

    def _purge_key(self, key: str) -> None:
        value = self._values.get(key)
        if value is None:
            return
        _, expires_at = value
        if expires_at is not None and time.time() > expires_at:
            self._values.pop(key, None)

    def get(self, key: str) -> str | None:
        with self._lock:
            self._purge_key(key)
            value = self._values.get(key)
            return value[0] if value is not None else None

    def set(self, key: str, value: str, ttl_seconds: float) -> None:
        expires_at = time.time() + ttl_seconds if ttl_seconds > 0 else None
        with self._lock:
            self._values[key] = (value, expires_at)

    def delete(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._values.pop(key, None)
                self._sets.pop(key, None)

    def exists(self, key: str) -> bool:
        with self._lock:
            self._purge_key(key)
            return key in self._values

    def sadd(self, key: str, *values: str) -> None:
        with self._lock:
            self._sets.setdefault(key, set()).update(values)

    def smembers(self, key: str) -> set[str]:
        with self._lock:
            return set(self._sets.get(key, set()))

    def srem(self, key: str, *values: str) -> None:
        with self._lock:
            members = self._sets.get(key)
            if not members:
                return
            for value in values:
                members.discard(value)
            if not members:
                self._sets.pop(key, None)


class RedisCacheBackend(_CacheBackend):
    name = "redis"

    def __init__(self, redis_url: str) -> None:
        if redis is None:
            raise RuntimeError("redis package is not installed")
        self._client = redis.Redis.from_url(redis_url, decode_responses=True)

    def get(self, key: str) -> str | None:
        return self._client.get(key)

    def set(self, key: str, value: str, ttl_seconds: float) -> None:
        self._client.set(key, value, ex=max(1, int(ttl_seconds)))

    def delete(self, *keys: str) -> None:
        if keys:
            self._client.delete(*keys)

    def exists(self, key: str) -> bool:
        return bool(self._client.exists(key))

    def sadd(self, key: str, *values: str) -> None:
        if values:
            self._client.sadd(key, *values)

    def smembers(self, key: str) -> set[str]:
        return {str(value) for value in self._client.smembers(key)}

    def srem(self, key: str, *values: str) -> None:
        if values:
            self._client.srem(key, *values)

    def ping(self) -> bool:
        return bool(self._client.ping())


@dataclass(frozen=True)
class CacheConfig:
    backend: str = "memory"
    url: str | None = None
    enabled: bool = True
    prefix: str = "funding-bot"
    donor_ttl_seconds: float = 300.0
    connector_ttl_seconds: float = 60.0
    deduped_profile_ttl_seconds: float = 600.0

    @classmethod
    def from_env(cls) -> "CacheConfig":
        backend = os.environ.get("FUNDING_BOT_CACHE_BACKEND", "memory").strip().lower() or "memory"
        return cls(
            backend=backend,
            url=os.environ.get("FUNDING_BOT_CACHE_URL"),
            enabled=_read_bool_env("FUNDING_BOT_CACHE_ENABLED", True),
            prefix=os.environ.get("FUNDING_BOT_CACHE_PREFIX", "funding-bot").strip()
            or "funding-bot",
            donor_ttl_seconds=_read_float_env("FUNDING_BOT_DONOR_CACHE_TTL_SECONDS", 300.0),
            connector_ttl_seconds=_read_float_env("FUNDING_BOT_CONNECTOR_CACHE_TTL_SECONDS", 60.0),
            deduped_profile_ttl_seconds=_read_float_env(
                "FUNDING_BOT_DEDUPED_PROFILE_CACHE_TTL_SECONDS", 600.0
            ),
        )


class CacheManager:
    def __init__(
        self, config: CacheConfig | None = None, *, backend: _CacheBackend | None = None
    ) -> None:
        self.config = config or CacheConfig.from_env()
        self._backend = backend or self._build_backend(self.config)
        self._lock = threading.Lock()
        self._last_error: str | None = None
        self._metrics: dict[tuple[str, str], dict[str, Any]] = {}
        self._namespace_ttls = {
            **_DEFAULT_NAMESPACE_TTLS,
            "donor-records": self.config.donor_ttl_seconds,
            "connector-data": self.config.connector_ttl_seconds,
            "deduped-profiles": self.config.deduped_profile_ttl_seconds,
        }

    @staticmethod
    def _build_backend(config: CacheConfig) -> _CacheBackend:
        if not config.enabled:
            return MemoryCacheBackend()
        if config.backend == "redis" and config.url:
            try:
                backend = RedisCacheBackend(config.url)
                backend.ping()
                return backend
            except Exception:
                return MemoryCacheBackend()
        return MemoryCacheBackend()

    @property
    def backend_name(self) -> str:
        return self._backend.name if self.config.enabled else "disabled"

    def make_region(
        self, namespace: str, *, scope: str = "default", ttl_seconds: float | None = None
    ) -> "CacheRegion":
        return CacheRegion(self, namespace=namespace, scope=scope, ttl_seconds=ttl_seconds)

    def _metrics_bucket(self, namespace: str, scope: str) -> dict[str, Any]:
        key = (namespace, scope)
        with self._lock:
            bucket = self._metrics.setdefault(
                key,
                {
                    "namespace": namespace,
                    "scope": scope,
                    "hits": 0,
                    "misses": 0,
                    "sets": 0,
                    "invalidations": 0,
                },
            )
        return bucket

    def _record(self, namespace: str, scope: str, metric: str) -> None:
        with self._lock:
            bucket = self._metrics.setdefault(
                (namespace, scope),
                {
                    "namespace": namespace,
                    "scope": scope,
                    "hits": 0,
                    "misses": 0,
                    "sets": 0,
                    "invalidations": 0,
                },
            )
            bucket[metric] += 1

    def _serialized_key(self, key: Any) -> str:
        if isinstance(key, str):
            raw = key
        else:
            raw = json.dumps(key, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _scope_key(self, namespace: str, scope: str, key: Any) -> str:
        return f"{self.config.prefix}:{namespace}:{scope}:{self._serialized_key(key)}"

    def _scope_index(self, namespace: str, scope: str) -> str:
        return f"{self.config.prefix}:index:{namespace}:{scope}"

    def _tag_index(self, namespace: str, scope: str, tag: str) -> str:
        digest = hashlib.sha256(tag.encode("utf-8")).hexdigest()
        return f"{self.config.prefix}:tag:{namespace}:{scope}:{digest}"

    def _ttl_for(self, namespace: str, ttl_seconds: float | None) -> float:
        return float(ttl_seconds or self._namespace_ttls.get(namespace, 300.0))

    def get(self, namespace: str, key: Any, *, scope: str = "default") -> tuple[bool, Any]:
        if not self.config.enabled:
            self._record(namespace, scope, "misses")
            return False, None
        full_key = self._scope_key(namespace, scope, key)
        try:
            payload = self._backend.get(full_key)
        except Exception as exc:
            self._last_error = str(exc)
            self._record(namespace, scope, "misses")
            return False, None
        if payload is None:
            self._record(namespace, scope, "misses")
            return False, None
        self._record(namespace, scope, "hits")
        return True, json.loads(payload)

    def set(
        self,
        namespace: str,
        key: Any,
        value: Any,
        *,
        scope: str = "default",
        ttl_seconds: float | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        if not self.config.enabled:
            return
        full_key = self._scope_key(namespace, scope, key)
        ttl = self._ttl_for(namespace, ttl_seconds)
        payload = json.dumps(value, sort_keys=True, default=str)
        try:
            self._backend.set(full_key, payload, ttl)
            self._backend.sadd(self._scope_index(namespace, scope), full_key)
            for tag in tags or ():
                self._backend.sadd(self._tag_index(namespace, scope, tag), full_key)
            self._record(namespace, scope, "sets")
        except Exception as exc:
            self._last_error = str(exc)

    def invalidate(self, namespace: str, key: Any, *, scope: str = "default") -> None:
        if not self.config.enabled:
            return
        full_key = self._scope_key(namespace, scope, key)
        try:
            self._backend.delete(full_key)
            self._backend.srem(self._scope_index(namespace, scope), full_key)
            self._record(namespace, scope, "invalidations")
        except Exception as exc:
            self._last_error = str(exc)

    def invalidate_tags(
        self, namespace: str, tags: list[str] | tuple[str, ...], *, scope: str = "default"
    ) -> None:
        if not self.config.enabled:
            return
        members: set[str] = set()
        tag_indexes = [self._tag_index(namespace, scope, tag) for tag in tags]
        try:
            for tag_index in tag_indexes:
                members.update(self._backend.smembers(tag_index))
            if members:
                self._backend.delete(*sorted(members))
                self._backend.srem(self._scope_index(namespace, scope), *sorted(members))
            self._backend.delete(*tag_indexes)
            self._record(namespace, scope, "invalidations")
        except Exception as exc:
            self._last_error = str(exc)

    def clear(self, namespace: str, *, scope: str = "default") -> None:
        if not self.config.enabled:
            return
        index_key = self._scope_index(namespace, scope)
        try:
            members = self._backend.smembers(index_key)
            if members:
                self._backend.delete(*sorted(members))
            self._backend.delete(index_key)
            self._record(namespace, scope, "invalidations")
        except Exception as exc:
            self._last_error = str(exc)

    def _prune_scope(self, namespace: str, scope: str) -> int:
        index_key = self._scope_index(namespace, scope)
        members = self._backend.smembers(index_key)
        stale = [member for member in members if not self._backend.exists(member)]
        if stale:
            self._backend.srem(index_key, *stale)
        return len(members) - len(stale)

    def stats(
        self, namespace: str, *, scope: str = "default", ttl_seconds: float | None = None
    ) -> dict[str, Any]:
        bucket = self._metrics_bucket(namespace, scope)
        size = 0
        if self.config.enabled:
            try:
                size = self._prune_scope(namespace, scope)
            except Exception as exc:
                self._last_error = str(exc)
        return {
            **bucket,
            "backend": self.backend_name,
            "size": size,
            "ttl_seconds": self._ttl_for(namespace, ttl_seconds),
        }

    def all_stats(self) -> list[dict[str, Any]]:
        with self._lock:
            pairs = list(self._metrics)
        if not pairs:
            pairs = [(namespace, "default") for namespace in self._namespace_ttls]
        return [self.stats(namespace, scope=scope) for namespace, scope in pairs]

    def health_snapshot(self) -> dict[str, Any]:
        reachable = True
        error = self._last_error
        if self.config.enabled and self._backend.name == "redis":
            try:
                reachable = bool(self._backend.ping())
                error = None
            except Exception as exc:
                reachable = False
                error = str(exc)
        return {
            "enabled": self.config.enabled,
            "backend": self.backend_name,
            "reachable": reachable,
            "prefix": self.config.prefix,
            "error": error,
        }


class CacheRegion:
    def __init__(
        self,
        manager: CacheManager,
        *,
        namespace: str,
        scope: str,
        ttl_seconds: float | None = None,
    ) -> None:
        self.manager = manager
        self.namespace = namespace
        self.scope = scope
        self.ttl_seconds = ttl_seconds

    def get(self, key: Any) -> tuple[bool, Any]:
        return self.manager.get(self.namespace, key, scope=self.scope)

    def set(self, key: Any, value: Any, *, tags: list[str] | tuple[str, ...] | None = None) -> None:
        self.manager.set(
            self.namespace,
            key,
            value,
            scope=self.scope,
            ttl_seconds=self.ttl_seconds,
            tags=tags,
        )

    def invalidate(self, key: Any) -> None:
        self.manager.invalidate(self.namespace, key, scope=self.scope)

    def invalidate_tags(self, *tags: str) -> None:
        self.manager.invalidate_tags(self.namespace, list(tags), scope=self.scope)

    def clear(self) -> None:
        self.manager.clear(self.namespace, scope=self.scope)

    def stats(self) -> dict[str, Any]:
        return self.manager.stats(self.namespace, scope=self.scope, ttl_seconds=self.ttl_seconds)
