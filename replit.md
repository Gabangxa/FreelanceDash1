# Freelancer Suite - Replit Configuration

## Overview

Freelancer Suite is a comprehensive SaaS platform built with Flask that provides end-to-end project management and business solutions for freelancers. The application offers client management, project tracking, time management, invoicing, and subscription services through a clean, responsive web interface.

## System Architecture

### Backend Architecture
- **Framework**: Flask with SQLAlchemy ORM
- **Database**: SQLite (development) with support for PostgreSQL (production)
- **Authentication**: Flask-Login with session management
- **Email Services**: Flask-Mail with SMTP configuration
- **Error Handling**: Centralized error handling with logging and user-friendly messages
- **Performance Monitoring**: Custom performance monitoring with slow query detection

### Frontend Architecture
- **Templates**: Jinja2 templating engine
- **CSS Framework**: Bootstrap 5 with custom styling
- **JavaScript**: Vanilla JavaScript with Bootstrap components
- **Asset Management**: Custom asset bundling and minification system
- **Responsive Design**: Mobile-first approach with dark/light mode support

### Security Features
- Password hashing with Werkzeug
- CSRF protection via Flask-WTF
- Input validation and sanitization
- Secure session management
- Password reset functionality with time-limited tokens
- Content Security Policy (CSP) with per-request nonces for both `script-src`
  and `style-src` (no `'unsafe-inline'`). All inline `<script>` and `<style>`
  blocks must carry `nonce="{{ csp_nonce }}"`, and inline `style=""`
  attributes are not used in templates — utility classes in
  `static/css/style.css` and the `safe_color` Jinja filter (for hex-color
  values injected into nonced `<style>` blocks) cover the equivalent needs.

## Key Components

### Core Modules
1. **User Management** (`auth/`): Registration, login, password reset
2. **Client Management** (`clients/`): Client profiles, contact information, project relationships
3. **Project Management** (`projects/`): Project creation, task management, time tracking
4. **Invoice System** (`invoices/`): Professional invoice generation with PDF support
5. **Settings** (`settings/`): User preferences, company information, customization
6. **Subscription System** (`polar/`): Polar.sh integration for payment processing

### Database Models
- **User**: Core user authentication and profile data
- **Client**: Client information with one-to-many project relationships
- **Project**: Project details linked to clients with task hierarchies
- **Task**: Individual work items within projects
- **TimeEntry**: Time tracking records for tasks and projects
- **Invoice/InvoiceItem**: Billing system with line items and multiple currencies
- **Subscription**: Polar.sh subscription management
- **UserSettings**: Customizable user preferences and company branding

### API Structure
- RESTful API endpoints under `/api/v1/`
- Standardized JSON response format
- Authentication-protected endpoints
- Performance monitoring and request logging

## Data Flow

### User Workflow
1. User registration/authentication through Flask-Login
2. Client creation with optional immediate project assignment
3. Project creation with hierarchical task management
4. Time tracking against specific tasks/projects
5. Invoice generation with automated calculations
6. PDF export and client communication

### Database Relationships
- Users → Clients (one-to-many)
- Clients → Projects (one-to-many)
- Projects → Tasks (one-to-many)
- Tasks → TimeEntries (one-to-many)
- Clients → Invoices (one-to-many)
- Users → Subscriptions (one-to-one)

## External Dependencies

### Required Services
- **SMTP Server**: Email delivery for password resets and notifications
- **Polar.sh API**: Subscription management and payment processing
- **File Storage**: Local storage for user uploads (logos, attachments)

### Third-Party Integrations
- **ReportLab**: PDF generation for invoices
- **Pillow**: Image processing for user uploads
- **Bootstrap**: Frontend framework via CDN
- **Font Awesome**: Icon library via CDN

### Environment Variables
- `FLASK_SECRET_KEY`: Application secret key. **Required in production** — the
  app refuses to start without it (no per-process generated fallback, which
  would silently invalidate sessions/CSRF on every gunicorn worker reload).
- `DATABASE_URL`: Database connection string.
- `MAIL_SERVER`, `MAIL_USERNAME`, `MAIL_PASSWORD`: Email configuration.
- `POLAR_API_KEY`: Subscription service API key.
- `FLASK_ENV`: `development` | `test` | (anything else = production).
- `PRODUCTION`: Optional explicit production flag (`true`/`1`/`yes`).
  When either `PRODUCTION=true` or `FLASK_ENV` is unset/non-development,
  the app enables HTTPS-only Secure cookies and the strict secret-key check.

### Database Migrations (Alembic / Flask-Migrate)
The app now uses Flask-Migrate for schema changes. New tables continue to be
created idempotently by `db.create_all()` at boot, but column changes must
go through Alembic so production deployments don't silently drop data.
- Run `flask db upgrade` after pulling new migrations.
- Create a new migration with `flask db migrate -m "<description>"`.
- Migrations live in `migrations/versions/`.

## Deployment Strategy

### Production Configuration
- Debug mode disabled in production
- Secure session handling with proxy support
- Rotating file logs with size limits
- Database connection pooling
- Asset minification and caching

### Development Setup
- SQLite database for local development
- Debug mode enabled with detailed error pages
- Console logging for development feedback
- Hot reload for template and static file changes

### Error Handling
- Centralized error logging with rotation
- User-friendly error pages (400, 403, 404, 500)
- Database error handling with rollback
- Performance monitoring with slow query detection

## Changelog

- May 02, 2026. NATS Phase 0 — message-bus foundation (no subscribers yet):
  - **`nats_client.py`.** Owns one asyncio event loop in a daemon thread
    that bridges sync Flask handlers to async `nats-py`. Module is a
    pure no-op when `NATS_URL` is unset (existing dev / test
    environments are unaffected, no `nats-py` server required). When
    `NATS_URL` *is* set, `init()` connects once at app startup and
    `publish(subject, payload)` is fire-and-log: failures are caught
    and a wedged NATS server can never break webhook ingest, invoice
    creation, or notification delivery. Exposes a `_SyncKV` wrapper
    for JetStream KV buckets so the storage layer can use the bus
    without touching asyncio.
  - **`events.py`.** Standard envelope (`v`, `id`, `type`, `user_id`,
    `timestamp`, `payload`) under the `app.<entity>.<verb>` subject
    convention. Two publishers wired up: `webhook.received` (in
    `webhooks/routes.py` after the event commits) and
    `notification.created` (in `webhooks/services.py` for both the
    user-targeted and system-wide creation paths). No PII on the bus —
    IDs and metadata only.
  - **`JetStreamKVStorage` (third storage backend).** Sits behind the
    existing `WebhookStorageBackend` ABC alongside Redis and the DB
    fallback. Three buckets (`webhook_rate_limit`,
    `webhook_failed_attempt`, `webhook_cache`) with per-entry expiry
    encoded inline (KV doesn't have sorted sets, so we serialise a
    JSON list of timestamps and use `last_revision` CAS with a
    bounded retry budget for atomic-ish increments). `get_storage()`
    selection is now `NATS_URL > REDIS_URL > DB`, with the same
    fail-fast policy as Redis: a configured-but-unreachable backend
    aborts boot rather than silently degrading.
  - **Operator visibility.** Admin → Webhooks page now surfaces NATS
    state (connected / connecting / error / disabled), the configured
    server URL, and the timestamp of the last successfully-published
    event. The status panel never crashes the events list.
  - **Tests.** 12 new unit tests pinning the no-op stub semantics and
    envelope shape (`tests/test_nats_client.py`). Storage contract
    extracted into `tests/storage_contract.py` so the same behaviour
    suite can run against any backend; live JetStream contract tests
    in `tests/test_storage_contract_nats.py` skip cleanly unless
    `NATS_TEST_URL` is set. Existing 174 tests stay green.
  - **Docs.** `docs/nats.md` covers Synadia hosting choice, env vars
    (`NATS_URL`, `NATS_CREDS_PATH`, `NATS_CLIENT_NAME`), subject
    naming, the envelope contract, local-dev `nats-server -js`
    commands, and the one-line rollback (`unset NATS_URL`).
- May 02, 2026. Code-review fixes — money precision, IDOR hardening,
  narrowed exception handling:
  - **Money to Decimal end-to-end.** `Invoice.amount`,
    `InvoiceItem.{quantity, rate, amount}` and `Subscription.amount`
    converted from `Float` to `Numeric` (precision 12 / scale 2 for
    money, scale 4 for quantity so fractional hours like 1.25h
    round-trip exactly). `invoices/forms.py` switched from
    `FloatField` to `DecimalField`, and `invoices/routes.py` now uses
    `Decimal` arithmetic throughout the totaling loop with a
    `_to_money()` helper that quantizes to 2dp using `ROUND_HALF_UP`.
    Removes the binary-rounding drift that made `0.10 + 0.20` render
    as `0.30000000000000004` in invoice totals.
    `settings/routes.py:export_data_json` got a `default=` JSON
    encoder so Decimal columns serialize cleanly.
  - **Migration `0006_money_to_numeric`.** Idempotent (inspects each
    column type before altering, skips when already `Numeric`),
    SQLite-safe (`batch_alter_table`), reversible (`downgrade` flips
    columns back to `Float`). Tested round-trip on SQLite:
    NUMERIC → FLOAT → NUMERIC.
  - **Belt-and-suspenders tenant scoping.** Every cross-table lookup
    now carries `user_id=current_user.id` even when the parent row
    was already scoped, so a future refactor can't silently leak
    another tenant's data. Touched routes:
    `clients/routes.py:view_client` + `delete_client` (project lookups),
    `invoices/routes.py:create_invoice` (project lookup + dropdown
    population) and `get_projects` (JSON helper),
    `projects/routes.py:view_task` + `edit_task` (TimeEntry queries
    via Project join) and `get_project_tasks` (Task query via Project
    join).
  - **IDOR regression tests.** New `tests/test_tenant_isolation.py`
    creates two tenants per test (uuid-suffixed to dodge UNIQUE
    collisions) and asserts that user A asking for any of user B's
    row ids — client, project, task, invoice, invoice PDF, JSON
    project-list helper, JSON task-list helper — gets a 404 / refusal,
    never the data. 12 new tests, full suite now 174 passing.
  - **Narrower exception handling.** `admin/routes.py` table-row-count
    loop's bare `except:` → `except SQLAlchemyError:` with
    `logger.exception`. Four unannotated `except Exception:` blocks
    in `webhooks/storage.py` (rate-limit increment, cache write,
    counter clear, expired-row sweep) tightened to
    `except SQLAlchemyError:` so KeyboardInterrupt / MemoryError /
    programming errors surface instead of getting swallowed and
    re-raised as opaque DB failures.
- May 02, 2026. Landing page redesign — Jony-Ive-inspired minimalist
  ("Freelance.") brand:
  - Replaced `templates/index.html` (was 965-line Bootstrap landing) with
    a clean Apple-style page following the uploaded `freelance-dash` UI
    design and using its copy verbatim ("Focus on the work. We'll handle
    the rest.", "A frictionless environment for independent
    professionals…", "Exceptional by design.", three pillars Intuitive
    view / Zero friction / Absolute privacy, "Start building" CTA, "Open
    App" nav, "Designed with intent." footer).
  - All styles live under a `.lp-root` scope in `static/css/style.css`
    (~340 new lines) so they cannot leak into the authenticated app.
    Inter Variable is loaded from `cdn.jsdelivr.net` via `@fontsource-
    variable/inter` (CSP-allowed origin).
  - The landing escapes the base.html `.container` with a full-bleed
    `position:relative; left:50%; margin-left:-50vw` wrapper so the
    section bands (off-white hero / white features / white footer) span
    the full viewport, with `overflow-x:hidden` to suppress sidescroll.
  - Auth-aware CTAs preserved: unauth → register / login, auth →
    dashboard. Brand label kept as "Freelance." per the uploaded design;
    rest of the app continues to brand as Freelancer Suite / WorkVista.
  - Two CSS gotchas solved during implementation, documented inline:
    1. `.lp-root a { color: inherit }` (specificity 0,1,1) was beating
       `.lp-cta-primary` (0,1,0) and rendering the "Start building"
       button text invisible — fixed by removing the color half of the
       reset; every link class now sets its own color explicitly.
    2. The project's regex CSS minifier in `asset_bundler.py` strips
       whitespace around `:` and would mangle `.lp-root :where(a)` into
       `.lp-root:where(a)` (descendant → element-with-pseudo). Avoided
       `:where()` for landing styles for that reason.
  - Post-review hardening (architect feedback):
    - **Real CSS isolation**: every landing selector is now prefixed with
      `.lp-root ` (59 rules), so the section truly cannot leak into the
      authenticated app even if class names collide later.
    - **Scrollbar-safe full-bleed**: replaced `width:100vw + left:50% +
      margin-left:-50vw` with `margin-inline: calc(50% - 50vw)` (no
      `width`); the element auto-fills the viewport without forcing an
      extra scrollbar-width column of horizontal overflow.
    - **WCAG AA contrast**: split the muted token in two —
      `--lp-text-muted` (`#6e6e73`, 4.66:1 on white) for body text
      (feature descriptions, footer); `--lp-text-muted-soft` (`#86868b`,
      Apple's gray, AA only for large text) for the hero h1 second line
      and the 24px subtitle.
    - **Removed dead nav links**: `Stories` (`#testimonials`) and
      `Pricing` (`#pricing`) had no destination sections — kept only
      `Features` to avoid misleading clicks. Slight deviation from the
      uploaded design's nav copy, in service of UX integrity.


- May 02, 2026. Centralized duration conversions (`utils/duration.py`):
  - All minutes ↔ hours / minutes ↔ timedelta math now routes through one
    module. The same conversion was previously scattered across
    `admin/routes.py`, `projects/routes.py` (~10 spots, including a
    nested `def format_duration`), and inline `{% set hours = ... // 60 %}`
    in two templates — exactly the duplication that let the C2 `/3600`
    bug under-report admin hours by 60× for as long as it did.
  - Public API: `minutes_to_hours`, `hours_to_minutes`, `timedelta_to_minutes`
    (raises on negative deltas so corrupt time entries fail loud),
    `split_minutes`, `format_duration`. All tolerate `None` and treat
    negative values as zero.
  - `format_duration` and `minutes_to_hours` are registered as Jinja
    filters in `app.py`, so templates can write
    `{{ entry.duration|format_duration }}` instead of duplicating the
    division.
  - `templates/projects/time_statistics.html` and `task_detail.html`
    migrated to the filter; `projects/routes.py` and `admin/routes.py`
    migrated to the helpers.
  - 35 new tests in `tests/test_duration.py` cover every helper
    (including None / negative / garbage input), the round-trip
    `hours → minutes → hours` invariant, and verify the Jinja filters
    are wired up. Suite is now 71 passing.
- May 02, 2026. Feature gating split into `has_feature` + `get_feature_limit`:
  - New `polar/features.py` is the single source of truth for the feature
    schema (`FEATURE_SCHEMA` + per-tier overrides). `Subscription.get_features`
    and the new `User._resolve_features` both delegate to it, so the
    duplicated free-tier defaults that used to live in both `models.py` and
    `polar/models.py` are gone.
  - `User.has_feature(name) -> bool` for boolean flags, and
    `User.get_feature_limit(name) -> Optional[int]` for numeric caps where
    `None` means *unlimited*. This replaces the legacy `0 == unlimited`
    sentinel — which was indistinguishable from a real cap of zero
    (e.g. `team_members=0` on free tier).
  - `clients/routes.py` and `projects/routes.py` migrated to
    `get_feature_limit(...)`, treating `None` as "no cap" and skipping the
    DB count entirely. Defensive `int(...)` wrappers removed. Fixed a latent
    bug in `projects/routes.py` where `count >= bool/int` could compare
    oddly when the old method returned a bool.
  - `User.has_subscription_feature` kept as a deprecation shim
    (`DeprecationWarning`, bug-compatible: returns `0` for unlimited
    limits and `0` for unknown `*_limit` names).
  - 12 new tests in `tests/test_feature_gating.py` cover free/pro/business
    tiers, `None` translation, contract-violation guards, schema-key
    agreement, the shim's deprecation warning, and unknown-limit
    legacy preservation. Suite is now 35 passing.
- May 02, 2026. Hardening pass from code review (C1–C4, I8–I10):
  - **WebhookEvent.metadata → event_metadata** (C1): the previous attribute
    name collided with SQLAlchemy's reserved `MetaData` registry on
    `DeclarativeBase`, so security/audit metadata never persisted. Added
    Alembic migration `0001_add_event_metadata` and a model round-trip test.
  - **Admin hours math** (C2): `TimeEntry.duration` is in minutes, not
    seconds — replaced `/3600` with `/60.0` in `admin/routes.py` (was
    under-reporting totals 60×). Added unit test.
  - **Open-redirect filter** (C3): added `utils/security.is_safe_url` that
    rejects `javascript:`, `data:`, `vbscript:`, protocol-relative `//host`
    and backslash-host payloads. `auth/login` uses it for `?next=`. 16-case
    test covers safe + dangerous payloads.
  - **403 error logging** (I8): fixed `current_user` lookup that was always
    logging "Unknown" because it was reading from `current_app` instead of
    `flask_login`.
  - **Production hardening** (I9/I10): app refuses to start without
    `FLASK_SECRET_KEY` in production; cookie `Secure` flag is now tied to
    explicit `IS_PRODUCTION` rather than `app.debug`.
  - **Email reliability** (C4): `send_email_async` now wraps every send in
    `app.app_context()`, retries 3× with exponential backoff, and persists
    every attempt to a new `EmailDeliveryLog` table (recipient, subject,
    status, attempts, last_error, sent_at). Foundation for the queue-based
    delivery system planned next.
  - **Tests**: added pytest harness (`tests/conftest.py` w/ in-memory
    SQLite). 21 tests passing.
- May 02, 2026. Task #17: "Continue with Google" sign-in (additive OAuth).
  - Added `oauth_provider` + `oauth_provider_id` columns on `User` with a
    composite `UNIQUE (oauth_provider, oauth_provider_id)` so two app
    accounts can never both claim the same Google identity. Migration
    `0005_add_user_oauth_columns` (idempotent, reversible).
  - New `google_auth.py` blueprint adapted from Replit's
    `flask_google_oauth` integration but with: Google `sub` (not email)
    as the stable identifier; three-step lookup (provider+id → email
    link → create with collision-safe generated username); session-
    bound CSRF state; `is_safe_url`-validated `next` redirect carried
    across the round-trip via session; 409 refusal to relink an account
    already bound to a different Google identity.
  - Blueprint is registered conditionally on
    `GOOGLE_OAUTH_CLIENT_ID/SECRET` so the app boots in environments
    without OAuth credentials. Templates check the
    `google_oauth_enabled` context flag to show/hide the
    "Continue with Google" button on `/auth/login` and `/auth/register`.
  - New read-only `/settings/sign-in-methods` page surfaces which auth
    methods (password, magic link, Google) are linked to the account.
    Linked from the user dropdown.
  - 19 new tests in `tests/test_google_oauth.py`; full suite is now
    157 passing.
- December 07, 2025. Added project completion feature and deadline alert system
  - Projects can now be marked as "completed" or reopened with a single click
  - Configurable deadline alerts (7 days, 3 days, 1 day, or custom interval)
  - Color-coded urgency indicators on dashboard (red/orange/yellow)
  - Deadline alert settings accessible from user dropdown menu
- July 03, 2025. Initial setup

## User Preferences

Preferred communication style: Simple, everyday language.