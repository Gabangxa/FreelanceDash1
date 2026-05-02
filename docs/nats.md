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

## Out of scope (Phase 1 territory)

* Long-lived subscriber processes (need a Reserved VM, not autoscale)
* Replacing the email-sending daemon with a NATS worker
* PDF generation queue
* Migrating Polar webhook handling to publish events
* Inter-service auth (NKey / JWT) — added when the first subscriber ships

## Critical constraints

* **No subscribers in Phase 0.** The web tier publishes only. Anything
  needing a long-lived consumer waits for Phase 1.
* **Publish failures are non-fatal.** A wedged NATS connection must
  never break invoice creation or webhook ingest. The async loop
  catches and logs; the request handler sees no exception.
* **Idempotent subjects from day one.** Subject names are the public
  contract for future services — pick them carefully.
