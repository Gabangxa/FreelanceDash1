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
- December 07, 2025. Added project completion feature and deadline alert system
  - Projects can now be marked as "completed" or reopened with a single click
  - Configurable deadline alerts (7 days, 3 days, 1 day, or custom interval)
  - Color-coded urgency indicators on dashboard (red/orange/yellow)
  - Deadline alert settings accessible from user dropdown menu
- July 03, 2025. Initial setup

## User Preferences

Preferred communication style: Simple, everyday language.