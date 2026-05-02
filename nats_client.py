"""
Thread-safe NATS client for the Flask web tier.

Why this module exists
----------------------
Flask is sync, ``nats-py`` is asyncio-only. The pragmatic bridge is one
asyncio event loop running in a daemon thread, owned by this module. Sync
request handlers call :func:`publish` / :func:`kv` and we submit the work
onto that loop via ``asyncio.run_coroutine_threadsafe``.

When ``NATS_URL`` is unset the entire module is a no-op stub: ``init()``
returns immediately, ``publish()`` swallows the call, and ``state()``
reports ``"disabled"``. That keeps existing dev / test environments
unaffected -- they don't need a NATS server, don't need ``nats-py``
installed, and pay zero cost on every request.

When ``NATS_URL`` *is* set we connect once at app startup. A wedged or
unreachable NATS server must never break a webhook ingest, an invoice
create, or a notification delivery -- :func:`publish` catches and logs.
:func:`init` deliberately does **not** raise on connect failure; the app
has to keep serving traffic even if the bus is down. The connection
state is reported via :func:`state` so operators can see the truth in
the admin panel.

Public API (all thread-safe, all safe to call from any gunicorn worker)
-----------------------------------------------------------------------
* :func:`init` -- start the loop + connect (idempotent)
* :func:`shutdown` -- drain + disconnect (used by tests / graceful exit)
* :func:`publish` -- fire-and-log; returns True on enqueue, False on no-op
* :func:`kv` -- get a synchronous wrapper around a JetStream KV bucket
* :func:`state` -- snapshot dict for the admin status panel
* :func:`record_publish_success` -- used internally / by tests
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level state. Guarded by ``_lock`` for the boot-time mutations
# (``init`` / ``shutdown``); the actual NATS calls run on the loop thread
# and don't need extra locking.
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_loop_ready = threading.Event()
_nc: Any = None  # nats.aio.client.Client
_js: Any = None  # JetStreamContext
_state: str = "disabled"  # disabled | connecting | connected | error
_last_error: Optional[str] = None
_last_event_at: Optional[datetime] = None
_url: Optional[str] = None

# How long sync callers wait for an async publish to land before they give
# up and log. Kept short so a wedged NATS connection can't pile up
# request-handling threads. The publish itself is fire-and-log, so a
# timeout here doesn't lose the event from the caller's POV -- the caller
# already considers it best-effort.
_PUBLISH_TIMEOUT_SECONDS = 2.0
_CONNECT_TIMEOUT_SECONDS = 5.0


def _truthy(env_value: Optional[str]) -> bool:
    return (env_value or "").strip().lower() not in ("", "0", "false", "no")


def is_enabled() -> bool:
    """Return True iff ``NATS_URL`` is set in the environment.

    This is the single source of truth for "should NATS be live in this
    process?" -- callers should never check the env var directly so the
    no-op semantics stay consistent.
    """
    return bool(os.environ.get("NATS_URL"))


# ---------------------------------------------------------------------------
# Background loop thread
# ---------------------------------------------------------------------------
def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Daemon-thread target that owns the asyncio event loop."""
    asyncio.set_event_loop(loop)
    _loop_ready.set()
    try:
        loop.run_forever()
    finally:
        try:
            loop.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            logger.exception("nats event loop close raised")


def _ensure_loop() -> Optional[asyncio.AbstractEventLoop]:
    """Start the daemon loop thread if not already running. Returns the
    loop, or ``None`` if NATS is disabled."""
    global _loop, _loop_thread
    if not is_enabled():
        return None
    if _loop is not None and _loop_thread is not None and _loop_thread.is_alive():
        return _loop
    _loop_ready.clear()
    _loop = asyncio.new_event_loop()
    _loop_thread = threading.Thread(
        target=_run_loop,
        args=(_loop,),
        name="nats-client-loop",
        daemon=True,
    )
    _loop_thread.start()
    # Block briefly so a caller that does init() then immediately publish()
    # doesn't race the loop startup. The loop itself is trivial to start;
    # this should fire essentially instantly.
    if not _loop_ready.wait(timeout=2.0):
        logger.error("nats event loop failed to become ready within 2s")
    return _loop


# ---------------------------------------------------------------------------
# Connect / disconnect
# ---------------------------------------------------------------------------
async def _connect(url: str, creds_path: Optional[str]) -> tuple[Any, Any]:
    """Open a NATS connection and JetStream context. Imported lazily so
    the module is importable even when ``nats-py`` isn't installed (no-op
    mode)."""
    import nats  # noqa: WPS433 - lazy import is intentional
    from nats.errors import NoServersError  # noqa: F401 - imported for type clarity

    connect_kwargs: dict[str, Any] = {
        "servers": [url],
        "name": os.environ.get("NATS_CLIENT_NAME", "freelancer-suite-web"),
        "connect_timeout": _CONNECT_TIMEOUT_SECONDS,
        "max_reconnect_attempts": -1,  # reconnect forever in the background
        "reconnect_time_wait": 2,
    }
    if creds_path:
        connect_kwargs["user_credentials"] = creds_path

    nc = await nats.connect(**connect_kwargs)
    js = nc.jetstream()
    return nc, js


def init() -> None:
    """Connect to NATS using ``NATS_URL`` / ``NATS_CREDS_PATH`` from the
    environment. Idempotent. Safe to call from app startup even if NATS
    is disabled (no-op).

    Connection failure is logged but **does not raise** -- the web tier
    must keep serving traffic with publishers degraded to no-op until the
    connection recovers. This matches the contract documented at the top
    of this module: publish failures are non-fatal.
    """
    global _nc, _js, _state, _last_error, _url

    if not is_enabled():
        _state = "disabled"
        return

    with _lock:
        if _state == "connected" and _nc is not None:
            return  # already connected

        loop = _ensure_loop()
        if loop is None:  # pragma: no cover - is_enabled() already guarded
            return

        _state = "connecting"
        _last_error = None
        _url = os.environ["NATS_URL"]
        creds_path = os.environ.get("NATS_CREDS_PATH") or None

        try:
            future = asyncio.run_coroutine_threadsafe(
                _connect(_url, creds_path), loop
            )
            nc, js = future.result(timeout=_CONNECT_TIMEOUT_SECONDS + 2)
            _nc, _js = nc, js
            _state = "connected"
            logger.info("NATS connected: %s", _url)
        except Exception as exc:  # noqa: BLE001 - non-fatal by contract
            _state = "error"
            _last_error = str(exc)
            logger.exception(
                "NATS connection failed (publishers will no-op until "
                "the connection recovers): %s", exc
            )


def shutdown(timeout: float = 5.0) -> None:
    """Drain + close the connection and stop the loop thread. Used by
    tests and graceful shutdown. Safe to call when nothing is running."""
    global _nc, _js, _state, _loop, _loop_thread

    with _lock:
        nc = _nc
        loop = _loop

        if nc is not None and loop is not None and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(nc.drain(), loop)
                future.result(timeout=timeout)
            except Exception:  # noqa: BLE001 - best-effort
                logger.exception("NATS drain raised during shutdown")

        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)

        if _loop_thread is not None:
            _loop_thread.join(timeout=timeout)

        _nc = None
        _js = None
        _loop = None
        _loop_thread = None
        _state = "disabled" if not is_enabled() else "disconnected"


def reset_for_tests() -> None:
    """Hard reset of all module state. Used by tests so one test's
    connection / mocks don't bleed into the next."""
    global _nc, _js, _state, _last_error, _last_event_at, _url
    global _loop, _loop_thread
    with _lock:
        if _loop is not None and _loop.is_running():
            _loop.call_soon_threadsafe(_loop.stop)
        if _loop_thread is not None and _loop_thread.is_alive():
            _loop_thread.join(timeout=2.0)
        _nc = None
        _js = None
        _loop = None
        _loop_thread = None
        _state = "disabled" if not is_enabled() else "disconnected"
        _last_error = None
        _last_event_at = None
        _url = None
    _loop_ready.clear()


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------
def record_publish_success() -> None:
    """Bump the last-published-event timestamp surfaced in the admin
    status panel. Public so unit tests can simulate a successful publish
    without standing up a real NATS server."""
    global _last_event_at
    _last_event_at = datetime.utcnow()


async def _publish_async(subject: str, payload: bytes) -> None:
    if _nc is None:
        raise RuntimeError("NATS client not connected")
    await _nc.publish(subject, payload)


def publish(subject: str, payload: bytes) -> bool:
    """Publish ``payload`` to ``subject``. Thread-safe. Returns ``True``
    on success, ``False`` if NATS is disabled / not connected / failed.

    Never raises -- this is fire-and-log. Callers must not let publish
    failures break user-visible request handling.
    """
    if not is_enabled():
        return False
    if _state != "connected" or _nc is None:
        # Common case: NATS_URL is set but we haven't (re-)connected yet,
        # e.g. during startup or after a network blip. Quietly no-op so
        # the request still succeeds.
        return False

    loop = _loop
    if loop is None or not loop.is_running():  # pragma: no cover - defensive
        logger.warning("nats publish dropped: loop not running (subject=%s)", subject)
        return False

    try:
        future = asyncio.run_coroutine_threadsafe(
            _publish_async(subject, payload), loop
        )
        future.result(timeout=_PUBLISH_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - non-fatal by contract
        logger.warning(
            "nats publish failed (subject=%s): %s", subject, exc
        )
        return False

    record_publish_success()
    return True


# ---------------------------------------------------------------------------
# JetStream KV accessor (used by JetStreamKVStorage in webhooks/storage.py)
# ---------------------------------------------------------------------------
class _SyncKV:
    """Thin synchronous wrapper around an async JetStream KV bucket.

    Each method submits a coroutine onto the background loop and blocks
    until it completes. Callers in sync request handlers can use this
    exactly like a normal dict-ish object.
    """

    def __init__(self, bucket: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._bucket = bucket
        self._loop = loop

    def _run(self, coro: Any, timeout: float = _PUBLISH_TIMEOUT_SECONDS) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def get(self, key: str) -> Optional[Any]:
        """Return the KV entry (with .value, .revision) or ``None`` if
        missing. Mirrors the underlying ``KeyValue.get`` semantics."""
        from nats.js.errors import KeyNotFoundError

        async def _get() -> Any:
            try:
                return await self._bucket.get(key)
            except KeyNotFoundError:
                return None

        return self._run(_get())

    def put(self, key: str, value: bytes) -> int:
        return int(self._run(self._bucket.put(key, value)))

    def update(self, key: str, value: bytes, last: int) -> int:
        return int(self._run(self._bucket.update(key, value, last=last)))

    def delete(self, key: str) -> None:
        self._run(self._bucket.delete(key))

    def keys(self) -> list[str]:
        async def _keys() -> list[str]:
            try:
                return await self._bucket.keys()
            except Exception:  # noqa: BLE001 - empty bucket can raise NoKeysError
                return []

        return list(self._run(_keys()))

    def purge(self) -> None:
        async def _purge() -> None:
            for k in await self._bucket.keys():
                try:
                    await self._bucket.delete(k)
                except Exception:  # noqa: BLE001
                    logger.exception("kv delete failed during purge: %s", k)

        try:
            self._run(_purge())
        except Exception:  # noqa: BLE001
            logger.exception("kv purge raised")


def kv(bucket: str, ttl_seconds: Optional[int] = None) -> Optional[_SyncKV]:
    """Return a synchronous wrapper around the named JetStream KV bucket.

    The bucket is created if it doesn't exist. Returns ``None`` when NATS
    is disabled or not connected -- callers should treat that as "fall
    back to the next storage backend".
    """
    if not is_enabled() or _state != "connected" or _js is None or _loop is None:
        return None

    from nats.js.api import KeyValueConfig

    async def _open() -> Any:
        try:
            return await _js.key_value(bucket)
        except Exception:  # noqa: BLE001 - bucket likely doesn't exist yet
            cfg = KeyValueConfig(bucket=bucket, ttl=ttl_seconds)
            return await _js.create_key_value(config=cfg)

    try:
        future = asyncio.run_coroutine_threadsafe(_open(), _loop)
        bucket_obj = future.result(timeout=_CONNECT_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.exception("nats kv open failed (bucket=%s): %s", bucket, exc)
        return None

    return _SyncKV(bucket_obj, _loop)


# ---------------------------------------------------------------------------
# Status (used by the admin panel)
# ---------------------------------------------------------------------------
def state() -> dict:
    """Return a snapshot of the connection state for the admin panel.

    Always returns a dict; never raises. The shape is stable so the
    template can render it without conditional branching on missing
    keys.
    """
    return {
        "enabled": is_enabled(),
        "state": _state,
        "url": _url,
        "last_error": _last_error,
        "last_event_at": (
            _last_event_at.isoformat() + "Z" if _last_event_at else None
        ),
    }
