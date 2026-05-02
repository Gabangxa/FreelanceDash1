"""
NATS JetStream subscriber runner.

This is the entry point for the long-lived consumer process that runs
on a Reserved VM (NOT on Replit Autoscale -- see ``docs/nats.md`` for
why). It does:

  1. Boots the Flask app context so subscribers can use SQLAlchemy /
     mail / etc. exactly like a request handler.
  2. Connects to NATS using the same env vars as the web tier
     (``NATS_URL``, ``NATS_CREDS_PATH``).
  3. Ensures the ``APP_EVENTS`` JetStream stream exists.
  4. Iterates ``subscribers.REGISTRY`` and registers a JetStream
     consumer for each.
  5. Sleeps on ``SIGTERM`` / ``SIGINT``, drains, exits cleanly.

Run with::

    python worker.py

Or under a process supervisor (systemd, Replit Reserved VM run command,
etc.). The worker is single-process by design; for horizontal scaling,
run multiple instances and set ``Subscriber.queue_group`` so JetStream
load-balances between them.

Failure modes
-------------
* NATS unreachable at startup → log and exit non-zero. The supervisor
  should restart with backoff.
* Stream missing and can't be created → log and exit non-zero. Check
  the JetStream account quota / permissions.
* Subscriber raises while handling a message → ``nak()``, JetStream
  redelivers up to ``max_deliver`` times. After that the message is
  dropped (or routed to a dead-letter consumer if you configure one).
* Worker process dies mid-message → JetStream redelivers after
  ``ack_wait_seconds``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

# Load .env so the worker has the same env as the web tier when run
# locally. Production deployments set env vars via the platform.
from dotenv import load_dotenv
env_path = Path(".") / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("worker")


async def _ensure_app_events_stream(js: Any) -> None:
    """Create / update the ``APP_EVENTS`` stream so subscribers have
    something to bind to. Idempotent: if the stream already exists with
    a matching config, ``add_stream`` is a no-op."""
    from nats.js.api import RetentionPolicy, StorageType, StreamConfig

    cfg = StreamConfig(
        name="APP_EVENTS",
        subjects=["app.>"],
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        max_age=7 * 24 * 60 * 60,  # 7 days; plenty for catch-up after an outage
        max_msgs=10_000_000,
        max_bytes=1024 * 1024 * 1024,  # 1 GiB
        num_replicas=1,
    )
    try:
        await js.add_stream(config=cfg)
        logger.info("APP_EVENTS stream ensured (subjects=app.>)")
    except Exception:
        # Likely already exists with a different config. Try update.
        try:
            await js.update_stream(config=cfg)
            logger.info("APP_EVENTS stream updated to current config")
        except Exception:
            logger.exception(
                "Could not ensure APP_EVENTS stream. Subscribers will fail to "
                "bind. Check JetStream account quota / permissions."
            )
            raise


def _dispatch_sync(flask_app: Any, subscriber: Any, envelope: dict) -> None:
    """Sync wrapper run inside the executor. Brings up the Flask app
    context so subscribers can use ``db.session`` etc."""
    with flask_app.app_context():
        subscriber.handle(envelope)


async def _on_message(msg: Any, subscriber: Any, flask_app: Any) -> None:
    """Per-message dispatcher. Called from the asyncio loop; offloads
    the actual handler to a thread so blocking I/O doesn't stall the
    event loop."""
    # 1) Decode the envelope. Malformed JSON is a publisher bug;
    #    retrying won't fix it, so term() the message immediately so it
    #    doesn't loop forever in the consumer.
    try:
        envelope = json.loads(msg.data.decode("utf-8"))
        if not isinstance(envelope, dict):
            raise ValueError("envelope is not a JSON object")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        logger.error(
            "subscriber=%s discarding malformed envelope: %s",
            type(subscriber).__name__, exc,
        )
        try:
            await msg.term()
        except Exception:  # noqa: BLE001 - best-effort
            logger.exception("term() failed for malformed message")
        return

    # 2) Run the handler. nak on exception so JetStream retries.
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None, _dispatch_sync, flask_app, subscriber, envelope
        )
    except Exception:
        logger.exception(
            "subscriber=%s handler raised; nak-ing for redelivery (envelope id=%s)",
            type(subscriber).__name__, envelope.get("id"),
        )
        try:
            await msg.nak()
        except Exception:  # noqa: BLE001
            logger.exception("nak() failed")
        return

    # 3) Success → ack.
    try:
        await msg.ack()
    except Exception:  # noqa: BLE001
        logger.exception(
            "ack() failed for subscriber=%s envelope id=%s",
            type(subscriber).__name__, envelope.get("id"),
        )


async def _run() -> int:
    nats_url = os.environ.get("NATS_URL")
    if not nats_url:
        logger.error(
            "NATS_URL is not set. The worker has nothing to connect to. "
            "Set NATS_URL (and NATS_CREDS_PATH if using Synadia) and retry."
        )
        return 2

    # Bring up the Flask app context lazily so subscribers can use the
    # ORM. Importing app already runs the startup block (db.create_all
    # etc.); the worker shares the same DATABASE_URL.
    from app import app as flask_app

    # Don't double-init the web-tier nats_client in this process; the
    # worker has its own connection because it needs JetStream pull
    # consumers, not just a publish channel.
    import nats
    from subscribers import REGISTRY

    connect_kwargs: dict[str, Any] = {
        "servers": [nats_url],
        "name": os.environ.get("NATS_CLIENT_NAME", "freelancer-suite-worker"),
        "max_reconnect_attempts": -1,
        "reconnect_time_wait": 2,
    }
    creds_path = os.environ.get("NATS_CREDS_PATH")
    if creds_path:
        connect_kwargs["user_credentials"] = creds_path

    logger.info("Connecting to NATS at %s …", nats_url)
    try:
        nc = await nats.connect(**connect_kwargs)
    except Exception:
        logger.exception("NATS connect failed; exiting so the supervisor can restart us")
        return 3
    logger.info("Connected. Setting up JetStream …")

    js = nc.jetstream()
    try:
        await _ensure_app_events_stream(js)
    except Exception:
        await nc.drain()
        return 4

    # Register every subscriber in the REGISTRY as a JetStream push
    # consumer. We use durable names so a worker restart resumes from
    # where it left off rather than re-reading the whole stream. The
    # explicit ConsumerConfig is critical -- without it, js.subscribe
    # uses JetStream defaults (max_deliver=-1 = infinite redelivery,
    # ack_wait=30s) instead of the per-subscriber values declared on
    # the class. Infinite redelivery on a poison message would starve
    # the consumer.
    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    subscriptions = []
    for sub_cls in REGISTRY:
        sub = sub_cls()

        async def _cb(msg, _sub=sub):
            await _on_message(msg, _sub, flask_app)

        consumer_config = ConsumerConfig(
            durable_name=sub.durable_name,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=sub.ack_wait_seconds,
            max_deliver=sub.max_deliver,
            deliver_policy=DeliverPolicy.ALL,
        )
        try:
            psub = await js.subscribe(
                sub.subject,
                durable=sub.durable_name,
                queue=sub.queue_group,
                cb=_cb,
                manual_ack=True,
                config=consumer_config,
            )
            subscriptions.append(psub)
            logger.info(
                "Bound subscriber=%s subject=%s durable=%s "
                "(ack_wait=%ds, max_deliver=%d)",
                type(sub).__name__, sub.subject, sub.durable_name,
                sub.ack_wait_seconds, sub.max_deliver,
            )
        except Exception:
            logger.exception(
                "Failed to bind subscriber=%s subject=%s; the worker will "
                "continue with the rest, but this subject is unconsumed",
                type(sub).__name__, sub.subject,
            )

    if not subscriptions:
        logger.error("No subscriptions established; exiting")
        await nc.drain()
        return 5

    # Wait for SIGTERM / SIGINT.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    logger.info(
        "Worker ready. %d subscriber(s) running. Send SIGTERM to drain & exit.",
        len(subscriptions),
    )
    await stop.wait()

    logger.info("Shutdown signal received; draining …")
    try:
        await nc.drain()
    except Exception:
        logger.exception("drain() raised during shutdown")
    logger.info("Worker stopped cleanly.")
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
