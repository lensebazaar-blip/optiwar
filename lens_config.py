"""The compiled configuration a lens page and its checkout read.

Reading order, every request, no process-global state:

    Redis  ``ow:lens:cfg:<product_id>:<rule_version>``
      ↓ miss
    ``contact_lens_configs`` row for that exact version
      ↓ miss (a lens loaded by the importer before rules existed, or a config
              somebody deleted)
    the variant rows themselves, as ``lens_order`` has always read them

Whatever the source, the result is the same object: a ``lens_order.Matrix``
(or ``Rules``) that ``options()`` and ``find()`` are asked. The version is the
product row's ``rule_version``, read on the same cursor as the product, so a
publication that lands between two requests is seen whole by the second and
not at all by the first — and never half by either. Cache keys carry the
version, which is why nothing is ever invalidated: an old key simply stops
being asked for and expires.

Redis is an accelerator and not a dependency. A refused connection, a timeout
or a corrupt value is logged once per process and the database answers; the
checkout's truth was never in the cache.
"""
import json
import logging
import os
import threading

try:
    from . import lens_order, lens_rules
except ImportError:  # run as a plain module (tests, deploy tool, scripts)
    import lens_order
    import lens_rules

log = logging.getLogger(__name__)

KEY = "ow:lens:cfg:%s:%s"
TTL_SECONDS = int(os.environ.get("LENS_CONFIG_TTL_SECONDS", "86400"))
REDIS_URL = os.environ.get("LENS_CONFIG_REDIS_URL",
                           "redis://127.0.0.1:6379/2")

CONFIG_SQL = ("SELECT config_json FROM contact_lens_configs "
              "WHERE product_id = %s AND rule_version = %s")

SOURCE_CACHE = "cache"
SOURCE_DB = "db"
SOURCE_ROWS = "rows"


class _Cache(object):
    """One Redis client per process, created on first use and shared by every
    thread and request after that. The client is a connection pool and is
    thread-safe; the only mutable state here is the creation, which is locked,
    and the "warned" flag, which only ever goes False -> True."""

    def __init__(self, url=None, factory=None):
        self._url = url or REDIS_URL
        self._factory = factory
        self._client = None
        self._lock = threading.Lock()
        self._warned = False
        self.disabled = os.environ.get("LENS_CONFIG_CACHE", "1") == "0"

    def client(self):
        if self.disabled:
            return None
        if self._client is None:
            with self._lock:
                if self._client is None:
                    try:
                        if self._factory:
                            self._client = self._factory()
                        else:
                            import redis
                            self._client = redis.Redis.from_url(
                                self._url, socket_connect_timeout=0.2,
                                socket_timeout=0.2)
                    except Exception as exc:  # noqa: BLE001
                        self._warn("client", exc)
                        self.disabled = True
                        return None
        return self._client

    def get(self, key):
        client = self.client()
        if client is None:
            return None
        try:
            raw = client.get(key)
        except Exception as exc:  # noqa: BLE001
            self._warn("get", exc)
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            self._warn("decode", "corrupt value at %s" % key)
            return None

    def set(self, key, value):
        client = self.client()
        if client is None:
            return
        try:
            client.set(key, json.dumps(value, separators=(",", ":")),
                       ex=TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            self._warn("set", exc)

    def _warn(self, where, exc):
        if not self._warned:
            self._warned = True
            log.warning("lens config cache unavailable (%s): %s; serving from "
                        "the database", where, exc)


_default_cache = _Cache()


def config(cursor, product_id, rule_version, cache=None):
    """The compiled config for exactly this version, or ``None``."""
    cache = _default_cache if cache is None else cache
    version = _int(rule_version)
    if not version:
        return None, None
    key = KEY % (product_id, version)
    found = cache.get(key)
    if found and found.get("checksum") == lens_rules.checksum(found):
        return found, SOURCE_CACHE
    cursor.execute(CONFIG_SQL, (product_id, version))
    row = cursor.fetchone()
    if not row:
        return None, None
    raw = row["config_json"] if isinstance(row, dict) else row[0]
    try:
        found = json.loads(raw)
    except ValueError:
        log.error("contact_lens_configs %s/%s is not JSON", product_id,
                  version)
        return None, None
    if found.get("checksum") != lens_rules.checksum(found):
        log.error("contact_lens_configs %s/%s fails its checksum", product_id,
                  version)
        return None, None
    cache.set(key, found)
    return found, SOURCE_DB


def shape(cursor, lens, cache=None):
    """What this lens states as orderable, wherever it is fastest to read.

    Returns the ``Matrix``/``Rules`` the rest of ``lens_order`` works on;
    ``shape_with_source`` says where it came from, for tests and the report.
    """
    return shape_with_source(cursor, lens, cache)[0]


def shape_with_source(cursor, lens, cache=None):
    if (lens.get("param_mode") or "").strip().upper() == "RULES":
        return (lens_order.selectable(
            lens_order.param_rules(cursor, lens["product_id"]),
            lens.get("lens_type")), SOURCE_ROWS)
    compiled, source = config(cursor, lens["product_id"],
                              lens.get("rule_version"), cache)
    if compiled:
        return lens_order.Matrix(lens_rules.rows_from_config(compiled)), source
    return lens_order.Matrix(lens_order.variants(cursor, lens["product_id"])), \
        SOURCE_ROWS


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
