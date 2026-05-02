# NATS integration

## What this is

Phase 0 of the NATS rollout. We use NATS for two things today:

1. **`events.publish`** — a thin pub/sub helper the app uses to emit
   `webhook.received` and `notification.created` events. Phase 0 has
   no subscribers; the publisher is wired in so future services can
   subscribe without re-plumbing the call sites.
2. **`JetStreamKVStorage`** — a third backend for the existing
   `WebhookStorageBackend` abstract base. Replaces the Postgres
   tables (`webhook_rate_limit_event`, `webhook_failed_attempt`,
   `webhook_cache_entry`) when `NATS_URL` is set.

If `NATS_URL` is unset the entire feature is a no-op: publishers
silently swallow, and storage falls back to Redis (if `REDIS_URL` is
set) or the existing DB tables.

## Hosting

We default to **Synadia Cloud free tier**. Replit autoscale can't host a
NATS server itself (instances spin up and down on web traffic, NATS
needs to be persistently reachable). Synadia gives us TLS + auth out of
the box and is free for the volumes we need today.

Migration to a self-hosted server later is a one-line `NATS_URL`
change — all NATS clients speak the same wire protocol, so the app
doesn't care who's running the server.

## Environment variables

| Var | Required | Description |
|---|---|---|
| `NATS_URL` | yes (to enable) | Server URL, e.g. `tls://connect.ngs.global:4222`. Unset → feature is a no-op. |
| `NATS_CREDS_PATH` | only for Synadia / NGS | Filesystem path to a `.creds` file. Unset → assume no auth (fine for local `nats-server`). |
| `NATS_CLIENT_NAME` | no | Client identifier shown in `nats-server` logs. Defaults to `freelancer-suite-web`. |

`NATS_URL` and `NATS_CREDS_PATH` are read once at app startup. To
disable NATS in a hurry, unset `NATS_URL` and restart — the app falls
back to the existing DB-backed storage and publishers go quiet.

## Subject naming

All app events publish under the `app.` prefix:

```
app.<entity>.<verb>
```

Phase 0 publishes:

* `app.webhook.received` — every inbound webhook (after security checks)
* `app.notification.created` — every notification row written to the DB

Envelope shape (versioned, frozen — bump `ENVELOPE_VERSION` in
`events.py` if you change it):

```json
{
  "v": 1,
  "id": "uuid4",
  "type": "webhook.received",
  "user_id": 42,
  "timestamp": "2026-05-02T12:34:56.789Z",
  "payload": {...}
}
```

**Don't put PII on the bus.** IDs, timestamps, event types only. If a
subscriber needs the full row, it loads it from Postgres by ID. This
keeps the bus auditable and lets us avoid encrypting payloads in
Phase 0.

### Invariant: every `Notification` row publishes `notification.created`

The only places that construct `Notification()` rows in this codebase
are `webhooks/services.py::_create_user_notification` (line ~244) and
`webhooks/services.py::_create_system_notification` (line ~300). Both
publish `notification.created` after the row commits. **If you add a
new code path that writes a `Notification` row, you must publish the
event from that site too** -- there is no central hook today, and a
missing publish silently breaks any future subscriber that assumes
DB-row-and-bus-event are 1:1.

When the planned notification-delivery worker ships in Phase 1 we
should refactor creation behind a single helper (e.g. a SQLAlchemy
`after_insert` listener on `Notification`, or a `notifications.create()`
factory function) so this invariant becomes mechanical instead of
documentational. Tracked separately.

## Running NATS locally for dev

```bash
# Install the binary
brew install nats-server   # macOS
# or download from https://github.com/nats-io/nats-server/releases

# Run with JetStream enabled (required for KV)
nats-server -js -m 8222

# Point the app at it
export NATS_URL=nats://127.0.0.1:4222
```

The `-m 8222` flag exposes a local HTTP monitoring endpoint at
`http://localhost:8222/varz` — useful for sanity-checking that the app
actually connected.

## Rollback

```bash
unset NATS_URL
# restart the app
```

That's it. Storage falls back to `REDIS_URL` if set, otherwise the DB
tables. Publishers go quiet. No code changes, no migration to revert.

## Reconnect behaviour (operator note)

`nats-py`'s `max_reconnect_attempts=-1` / `reconnect_time_wait=2`
options only kick in **after** a successful initial connection. They
handle dropped connections, not a failed boot.

If NATS is unreachable at app startup, `nats_client.init()` logs the
failure, leaves the module in the `error` state (visible on the admin
panel), and `events.publish` quietly returns `False` for the lifetime
of that worker process. **A process restart is required** to retry the
initial connect.

Practically that means: if you flip on `NATS_URL` and the server is
down, fix the server, then `gunicorn`-reload (or kill the workers and
let the supervisor respawn them). Don't expect the app to silently
self-heal -- the admin panel is the source of truth for the current
state.

Storage backend reads/writes against an unreachable NATS server fail
fast and abort boot (same fail-fast policy as Redis); only the
fire-and-log publisher is best-effort.

## Phase 1: subscriber on a Reserved VM

Phase 1 ships the first long-lived NATS consumer: the
`NotificationDeliverySubscriber` in `subscribers/notifications.py`. It
takes notification delivery off the request path so a slow SMTP server
can't slow down a webhook ingest.

### Architecture

```
┌─────────────────────────┐                ┌─────────────────────────┐
│  Web tier (Autoscale)   │                │  Worker (Reserved VM)   │
│  gunicorn workers       │                │  python worker.py       │
│  - publish events       │   JetStream    │  - subscribe(app.*)     │
│  - skip inline delivery │ ─────────────► │  - deliver_notification │
│    when flag is on      │                │  - ack / nak            │
└─────────────────────────┘                └─────────────────────────┘
              │                                       ▲
              ▼                                       │
        ┌──────────────────────────────────────────────┐
        │  NATS server (Synadia Cloud free tier)       │
        │  Stream: APP_EVENTS  (subjects: app.>)       │
        │  Durable: notification-delivery              │
        └──────────────────────────────────────────────┘
```

**Why two processes.** The web tier runs on Autoscale, which scales to
zero between requests — that kills any long-lived TCP connection. The
subscriber needs to be running 24/7 to consume messages, so it lives on
a Reserved VM (always-on, flat monthly rate). See the discussion in
the project changelog for cost trade-offs.

**Persistence.** Phase 1 upgrades `nats_client.publish` from core NATS
to JetStream-persisted publish. Messages survive subscriber downtime
and get at-least-once redelivery. The web tier auto-creates the
`APP_EVENTS` stream at startup; the worker also ensures it. If
JetStream is unavailable for any reason, publish falls back to core
NATS so the web tier keeps working.

### Provisioning the Reserved VM

In the Replit workspace:

1. Open the deployments panel and create a new **Reserved VM**
   deployment (not Autoscale). Choose the smallest size — the worker
   is mostly idle.
2. Set the **run command** to `python worker.py`.
3. Set these **secrets** on the VM (must match the web tier exactly):
   * `NATS_URL` — the same server the web tier connects to
   * `NATS_CREDS_PATH` — for Synadia, point to the uploaded `.creds` file
   * `DATABASE_URL` — same Postgres URL as the web tier
   * `FLASK_SECRET_KEY` — same value as the web tier (the worker
     imports the Flask app to get an app context for SQLAlchemy)
4. **Don't set** `NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS` yet. Leave it
   unset on both web and worker for the soak period.

### Safety interlocks built into the code

Two failure modes during cutover are caught automatically; one is not
and needs operator monitoring:

| Failure mode                          | What happens                                                                                                       |
|---------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| Web flag on, stream couldn't be created at startup | Web tier sees `_jetstream_publish_enabled=False` and falls back to inline delivery. **No notifications lost.** |
| Web flag on, JS publish fails mid-runtime (single message) | `nats_client.publish` re-raises (does NOT silently core-publish), `events.publish` returns False, caller in `webhooks/services.py` sees `published_ok=False` and inline-delivers THIS notification. Subsequent publishes also bypass JS until the next restart. **No notifications lost.** |
| Web flag on, worker process not running | **Not caught in code.** Web publishes to JetStream, the message persists, but nothing consumes it. Notifications are delayed (not lost — they deliver when the worker comes back). Set up an alert on the JetStream consumer's `num_pending` to catch this within minutes. |
| Worker flag on, web flag off          | Worker delivers from the bus AND web delivers inline → **double delivery**. The published `notification.created` event drives the worker; the inline call drives the web tier. Avoid by always flipping web first, then worker, or by using deploy automation that flips both atomically. |
| System broadcast (`_create_system_notification`) during JS outage | Per-message `published_ok` is **not** captured in the broadcast loop, so a broadcast during a runtime JS outage may miss the worker for those specific users. Accepted trade-off for now — system broadcasts are operationally rarer than webhook-driven notifications. Tracked separately. |

### Cutover sequence (zero-downtime, reversible)

1. **Day 0 — soak.** Worker is running, flag is unset. Watch the
   worker logs:
   ```
   notification.delivery: flag off, ack-and-skipping notification_id=42
   ```
   Every webhook-triggered notification should produce one of these
   lines on the worker. If you don't see them, the bus isn't wired up
   correctly — fix that before flipping the flag.

2. **Day N — flip.** Set `NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS=true`
   in **both** the Autoscale deployment AND the Reserved VM. Restart
   both. From this point:
   * Web tier writes the row + publishes the event + returns.
   * Worker consumes the event + delivers + acks.
   * No double-deliveries (web tier no longer calls
     `deliver_notification` inline).

3. **Rollback at any time.** Unset the flag on both sides and restart.
   Inline delivery resumes immediately. The subscriber goes back to
   ack-and-discard. No data loss — every notification row is in
   Postgres regardless of the delivery path.

### Retry semantics (what acks vs. what naks)

The subscriber distinguishes permanent vs. transient failures so
JetStream retries the right things and doesn't loop forever on the
wrong things:

| `deliver_notification` returns                        | Subscriber action     | Why                                                            |
|-------------------------------------------------------|-----------------------|----------------------------------------------------------------|
| `{"email": {"status": "success"}, ...}` (no errors)   | ack                   | Delivered.                                                     |
| `{"error": "Notification not found"}`                 | ack (warn-log)        | Row deleted between publish and consume; retry won't help.     |
| `{"error": "User not found"}`                         | ack (warn-log)        | User deleted; retry won't help.                                |
| `{"error": "<anything else>"}`                        | **nak → retry**       | Unknown failure mode; safer to retry than silently swallow.    |
| `{"email": {"status": "error", ...}}`                 | **nak → retry**       | SMTP / mail-queue blip caught by the service; transient.       |
| `{"email": {"status": "failed"}}`                     | **nak → retry**       | Sender returned False; transient.                              |
| Handler raises `Exception`                            | **nak → retry**       | DB outage, OOM, etc.                                           |
| Envelope has no `payload.notification_id`             | ack (error-log)       | Publisher bug; retrying won't fix bad data.                    |
| Envelope is malformed JSON                            | term (error-log)      | Same — never retry, JetStream drops immediately.               |

After `max_deliver=5` failed attempts JetStream stops redelivering. To
catch poison messages, watch the consumer's "max-deliver-reached"
counter (`nats consumer info APP_EVENTS notification-delivery`).

### Idempotency contract

JetStream is at-least-once. The same `notification.created` envelope
can land twice if:

* The worker crashes between the SMTP send and the ack.
* `ack_wait_seconds` (60s) elapses before the worker acks.
* The operator restarts the worker mid-message.
* Any nak above causes a redelivery.

`NotificationDeliveryService.deliver_notification` is the choke point.
It must remain idempotent — re-running it for the same
`notification_id` should not double-send the email. The current
implementation relies on the email queue's per-recipient dedupe window;
if that ever changes, add an explicit dedupe check (e.g. an
`EmailDeliveryLog` lookup) at the top of the subscriber's `handle()`.

### Adding a new subscriber

1. Create a new module under `subscribers/` with a class that extends
   `Subscriber` (see `subscribers/notifications.py` for the template).
2. Append it to `REGISTRY` in `subscribers/__init__.py`.
3. Restart the worker. The new subscriber is bound at startup; no
   web-tier change needed.

If two workers should share the load on the same subject, set
`Subscriber.queue_group = "some-name"` and run multiple worker
instances. JetStream load-balances messages between them.

## Still out of scope (later phases)

* PDF generation queue (would replace the inline `generate_pdf` call
  in `invoices/routes.py`)
* Migrating Polar webhook handling to publish events
* Inter-service auth beyond the Synadia `.creds` file (NKey / JWT
  per-subscriber when we have more than one trust boundary)
* A web UI for the worker's consumer state (today: `nats consumer info
  APP_EVENTS notification-delivery` from a shell on the VM)

## Critical constraints

* **No subscribers in Phase 0.** The web tier publishes only. Anything
  needing a long-lived consumer waits for Phase 1.
* **Publish failures are non-fatal.** A wedged NATS connection must
  never break invoice creation or webhook ingest. The async loop
  catches and logs; the request handler sees no exception.
* **Idempotent subjects from day one.** Subject names are the public
  contract for future services — pick them carefully.
