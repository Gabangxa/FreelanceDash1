# SoloDolo
SoloDolo is a comprehensive SaaS platform for freelancers, offering end-to-end project management, client tracking, time management, invoicing, and subscription services.

## Run & Operate
- **Run**: `flask run`
- **Build**: _Populate as you build_
- **Typecheck**: _Populate as you build_
- **Codegen**: _Populate as you build_
- **DB Push**: `flask db upgrade` is the **only** path for schema changes. `app.py` no longer runs `db.create_all()` or inline `ALTER TABLE` blocks at boot — it only logs a stderr WARNING if Alembic `current` != `head`.

**Required Environment Variables**:
- `FLASK_SECRET_KEY`: **Required in production**; app refuses to start without it.
- `DATABASE_URL`: Database connection string.
- `MAIL_SERVER`, `MAIL_USERNAME`, `MAIL_PASSWORD`: Email configuration.
- `POLAR_API_KEY`: Polar.sh API key.
- `POLAR_WEBHOOK_SECRET`: Standard-webhooks signing secret.
- `POLAR_PROFESSIONAL_PRODUCT_ID`: Polar product ID for the Professional tier.
- `POLAR_PROFESSIONAL_MONTHLY_PRICE_ID`, `POLAR_PROFESSIONAL_YEARLY_PRICE_ID`: Polar `product_price_id` values.
- `FLASK_ENV`: `development` | `test` | (anything else = production).
- `PRODUCTION`: Optional explicit production flag (`true`/`1`/`yes`).
- `NATS_URL`: NATS server connection string (optional, enables NATS).
- `NATS_CREDS_PATH`: Path to NATS credentials file (optional).
- `NATS_CLIENT_NAME`: NATS client name (optional).
- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`: Google OAuth credentials (optional, enables Google sign-in).

## Stack
- **Framework**: Flask, SQLAlchemy ORM
- **Runtime**: Python (version not specified, assume latest stable for Flask)
- **Database**: SQLite (dev), PostgreSQL (prod)
- **ORM**: SQLAlchemy
- **Validation**: Flask-WTF for forms, Pydantic for API schemas (inferred, not explicit)
- **Build Tool**: Custom asset bundling and minification system, Flask-Migrate (Alembic) for DB migrations
- **Frontend**: Jinja2, Bootstrap 5, Vanilla JavaScript, ReportLab (PDF generation), Pillow (image processing)

## Where things live
- `auth/`: User authentication, registration, password reset.
- `clients/`: Client management.
- `projects/`: Project and task management, time tracking.
- `invoices/`: Invoice generation, PDF export.
- `settings/`: User and company settings, customization.
- `polar/`: Polar.sh subscription integration.
- `subscribers/`: NATS worker and notification delivery.
- `static/css/style.css`: Main CSS, including landing page styles scoped under `.lp-root`.
- `migrations/versions/`: Database migration scripts (Alembic).
- `docs/nats.md`: NATS setup and operational documentation.

## Architecture decisions
- **Decimal for Money**: All financial calculations use `Numeric` (Decimal) types end-to-end to prevent floating-point inaccuracies.
- **Tenant Scoping Defense-in-Depth**: Every cross-table lookup includes `user_id` for enhanced data isolation, even if the parent row is already scoped.
- **NATS for Async Notifications**: Utilizes NATS JetStream for asynchronous notification delivery, offloading from the web request path and providing retry semantics.
- **Strict CSP Implementation**: Employs per-request nonces for `script-src` and `style-src` to enhance security and prevent XSS, avoiding `unsafe-inline`.
- **Centralized Duration Conversion**: All time-related math (minutes ↔ hours ↔ timedelta) is consolidated into `utils/duration.py` to ensure consistency and prevent conversion bugs.
- **Image Upload Hardening**: Implemented robust checks against decompression bombs, size limits, and format whitelisting for user-uploaded images.
- **Alembic-Only Schema Management**: Migrations in `migrations/versions/` are the single source of truth. The inline-`ALTER TABLE`-at-boot escape hatch in `app.py` has been removed (consolidated into `0007_consolidate_startup_alters`), and `0000_baseline` materializes the whole schema from `db.metadata` so a brand-new database can bootstrap from `flask db upgrade` alone. Every migration is idempotent (existence-guarded), so fresh installs and existing DBs converge to head safely. A stderr WARNING fires at boot if `current` ≠ `head`.

## Product
- **User Management**: Registration, login, password reset, OAuth (Google).
- **Client & Project Management**: Create and manage clients, projects, tasks, and time entries.
- **Invoice System**: Generate professional invoices with PDF export, customizable branding (logo, signature, fonts, colors).
- **Subscription Management**: Integration with Polar.sh for managing user subscriptions (Free, Professional tiers).
- **Notification System**: Asynchronous notification delivery (via NATS).
- **Reporting**: Time statistics, project completion tracking, deadline alerts.

## User preferences
Preferred communication style: Simple, everyday language.
I prefer to receive detailed explanations about complex technical concepts.

## Gotchas
- **Production Secret Key**: The application will refuse to start in production without `FLASK_SECRET_KEY` set.
- **Image Upload Limits**: User-uploaded images are subject to `MAX_CONTENT_LENGTH=4MB`, 8000x8000 pixel dimensions, and `PNG/JPEG/GIF` format whitelist.
- **NATS Health**: If NATS JetStream is configured but unhealthy, the application falls back to inline notification delivery.
- **Alembic Migrations**: Every schema change MUST go through Alembic — there is no `db.create_all()` or inline `ALTER TABLE` fallback at boot anymore. Run `flask db upgrade` after pulling new migrations; if you skip it, the app still boots but logs a high-visibility WARNING to stderr.
- **Fresh DB Bootstrap**: A brand-new empty database is provisioned by a single command: `flask db upgrade`. The `0000_baseline` migration creates every table from `db.metadata` (`checkfirst=True`, so it's a no-op on existing DBs), then 0001–0007 layer on top — all idempotent. No manual `db.create_all() + flask db stamp` dance.
- **Minifier Compatibility**: The custom CSS minifier might mangle CSS with `:where()` selectors if not carefully managed.

## Pointers
- **Flask Documentation**: [https://flask.palletsprojects.com/](https://flask.palletsprojects.com/)
- **SQLAlchemy Documentation**: [https://docs.sqlalchemy.org/](https://docs.sqlalchemy.org/)
- **Bootstrap 5 Documentation**: [https://getbootstrap.com/docs/5.3/](https://getbootstrap.com/docs/5.3/)
- **NATS Documentation**: `docs/nats.md`
- **Polar.sh API Documentation**: [https://docs.polar.sh/api/](https://docs.polar.sh/api/)
- **ReportLab User Guide**: [https://www.reportlab.com/docs/reportlab-userguide.pdf](https://www.reportlab.com/docs/reportlab-userguide.pdf)